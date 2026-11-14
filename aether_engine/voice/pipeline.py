"""Voice cascade coordinator (Epic 8, E8-5.1a + E8-5.1b).

``VoicePipeline`` runs one voice session:

    transport frames -> VAD -> Endpointer -> STT -> talker -> TTS

* User speech streams out as VOICE_PARTIAL_TRANSCRIPT pushes:
  provisional partials while the utterance grows (the STT buffer
  re-transcribed on a cadence), then one ``is_final`` transcript.
  Only the final text is committed — via ``on_user_final`` — and
  handed to the talker (transcript discipline, grill Q28).
* The talker is fast chat (``fast_chat_talker`` adapts
  ``run_fast_chat``): its reply streams out as VOICE_OUTPUT_CHUNK
  text deltas word by word, and each completed sentence is
  synthesized by the local TTS stage and pushed as base64
  ``audio_chunk`` chunks. The full reply is committed via
  ``on_assistant_final``.
* Lifecycle is the device contract: constructing the pipeline
  opens nothing; ``start()`` refuses — with a
  ``VoiceUnavailableError`` naming every unavailable stage —
  before touching the transport; ``stop()`` closes the transport
  and every stage, idempotently.

The loop is full-duplex (E8-5.1b): frames keep flowing while a
reply plays, into a dedicated barge-in endpointer. Sustained user
speech — past the endpointer's minimum-speech guard, so a cough
or a backchannel doesn't count — is a barge-in: playback stops at
once via ``interrupt_output()``, the captured frames are replayed
into the main path as the next utterance, and history keeps only
the words actually heard. Heard words are tracked at word
granularity (N09): every emitted audio chunk carries a
session-monotonic ``chunk_seq`` and the renderer reports the
highest seq it has FINISHED playing; once a session's renderer
reports playback progress, a sentence counts only through its
played chunks — the first partially played one as its
chunk-proportional word prefix — because the desktop's playback
queue runs behind emission and buffered audio was never heard.
A renderer that never reports progress keeps the older estimate
(the same walk over emitted counts).

Turns route two ways (grill Q27/Q28): the talker answers chat;
a turn the optional escalation classifier calls work launches a
background supervisor task through ``on_escalation`` — a launch,
never a wait: the task lane is a separate async execution and the
talker lane never queues behind it — and a spoken acknowledgement
is the turn's reply. Results come back out of band:
``speak_announcement()`` speaks a finished task's summary at the
next quiet moment, under the same barge-in rules, without
committing it (the task machinery already persisted it as the
task's own message). Announcements carry a stable identity
(N10): every chunk is tagged with the announcement's id and
kind — and, for a result, the persisted message's own id — so
the desktop renders the spoken text as that message, never as
a second assistant turn; approval pauses are announced the
same way, as notices that a decision is waiting in chat.

The transport is the SmallWebRTC seam: audio frames come from a
``VoiceTransport``, never from a device the engine opens itself.
``WebRtcVoiceTransport`` (``aether_engine.voice.rtc``) is the
production transport: a real aiortc peer connection the desktop
negotiates over the engine WebSocket (``VOICE_RTC_OFFER_*`` /
``VOICE_RTC_ICE_*`` signalling); received audio is resampled to
PCM16 at the session sample rate and streamed into the cascade,
and TTS audio returns over ``VOICE_OUTPUT_CHUNK`` events.

Structure (Wave 5): this class is the session facade — it owns
the stages, the lifecycle and the wiring, and nothing else. The
two paths the cascade is made of live in their own modules:
``aether_engine.voice.intake`` (frames -> VAD -> endpointing ->
finished utterances, plus barge-in detection) and
``aether_engine.voice.delivery`` (transcription, turn routing,
reply delivery, announcements, heard accounting). They meet
only at the narrow callables wired in ``__init__`` below, so the
intake path has no code path that waits on a transcription
(E7-07's structural fix) and each half is testable on its own.
"""
import logging
from collections.abc import AsyncIterator

from aether_common.contracts import EventType
from aether_engine.voice import VoiceTalkerError, VoiceUnavailableError
from aether_engine.voice.delivery import (
    EmitFn,
    EscalationClassifier,
    Talker,
    TurnCallback,
    VoiceDelivery,
)
from aether_engine.voice.intake import VoiceIntake, VoiceTransport
from aether_engine.voice.rtc import WebRtcVoiceTransport
from aether_engine.voice.stt import FasterWhisperStt, SttEngine
from aether_engine.voice.tts import DEFAULT_VOICE, KokoroTts, TtsEngine
from aether_engine.voice.vad import Endpointer, EndpointerConfig, SileroVad, VadEngine

logger = logging.getLogger("aether_engine.voice.pipeline")

#: The spoken acknowledgement for an escalated turn (E8-5.1b): it
#: masks the supervisor's latency — the job is already running by
#: the time this is heard — and it is the turn's committed reply.
DEFAULT_ESCALATION_ACK = (
    "On it — I'll run that in the background "
    "and let you know when it's done."
)

#: How long speak_announcement waits for a quiet moment before
#: giving up (E7-06). The wait used to be unbounded: a user who
#: kept talking, or output activity that never settled, held the
#: caller's task forever. A minute is far past any real reply or
#: speech bout; past it the announcement is dropped — its text
#: already stands in chat as the task's own message.
_ANNOUNCEMENT_QUIET_TIMEOUT_S = 60.0


def _unavailable_message(reasons: dict[str, str]) -> str:
    detail = "; ".join(f"{stage}: {reason}" for stage, reason in reasons.items())
    return f"voice unavailable: {detail}"


class VoicePipeline:
    """One voice session's cascade. See the module docstring."""

    def __init__(
        self,
        *,
        session_id: str,
        vad: VadEngine,
        stt: SttEngine,
        tts: TtsEngine,
        transport: VoiceTransport,
        talker: Talker,
        emit: EmitFn,
        on_user_final: TurnCallback | None = None,
        on_assistant_final: TurnCallback | None = None,
        on_turn_error: TurnCallback | None = None,
        endpointer: Endpointer | None = None,
        endpointer_config: EndpointerConfig | None = None,
        tts_voice: str = DEFAULT_VOICE,
        sample_rate: int = 16000,
        partial_min_audio_ms: int = 250,
        escalation_classifier: EscalationClassifier | None = None,
        on_escalation: TurnCallback | None = None,
        escalation_ack: str = DEFAULT_ESCALATION_ACK,
    ):
        self.session_id = session_id
        # S11: the latest EQ affect read for this session,
        # recorded via note_affect_read (the talker's fast-chat
        # run computes one per turn). Session state only — it is
        # never persisted anywhere (eq.py: transient by
        # construction).
        self._latest_affect_read = None
        self._vad = vad
        self._stt = stt
        self._tts = tts
        self._transport = transport
        self._tts_voice = tts_voice
        self._sample_rate = sample_rate
        if endpointer is not None:
            self._endpointer = endpointer
        elif endpointer_config is not None:
            self._endpointer = Endpointer(endpointer_config)
        else:
            self._endpointer = Endpointer()
        # Barge-in detection state (E8-5.1b): a separate endpointer
        # watches frames while output plays, so the main
        # endpointer's utterance state is never corrupted.
        # S4-M16: the barge-in endpointer shares the config (so it
        # carries a Smart Turn policy object too) but never observes
        # a transcript, so it degrades to silence-only endpointing —
        # barge-in timing is deliberately unchanged by Smart Turn.
        self._barge_endpointer = Endpointer(self._endpointer.config)

        self._started = False
        self._stopping = False
        self._webrtc_config: dict | None = None

        # The two halves of the cascade (Wave 5 split). The intake
        # half is constructed first; the delivery half reads its
        # buffer through the intake's bound methods; then the
        # intake is bound to the delivery it schedules into. The
        # endpointers are shared objects: observe_transcript is
        # wired as a call-time lookup on the main endpointer, so
        # in-place wrapping of its method (as the Smart Turn
        # suites do) is seen by the delivery half.
        self._intake = VoiceIntake(
            vad=vad,
            transport=transport,
            endpointer=self._endpointer,
            barge_endpointer=self._barge_endpointer,
            sample_rate=sample_rate,
            partial_min_audio_ms=partial_min_audio_ms,
            is_stopping=lambda: self._stopping,
        )
        self._delivery = VoiceDelivery(
            session_id=session_id,
            stt=stt,
            tts=tts,
            talker=talker,
            emit=emit,
            on_user_final=on_user_final,
            on_assistant_final=on_assistant_final,
            on_turn_error=on_turn_error,
            tts_voice=tts_voice,
            sample_rate=sample_rate,
            escalation_classifier=escalation_classifier,
            on_escalation=on_escalation,
            escalation_ack=escalation_ack,
            utterance_snapshot=self._intake.utterance_snapshot,
            utterance_extends=self._intake.utterance_extends,
            has_utterance=self._intake.has_utterance,
            observe_transcript=(
                lambda text: self._endpointer.observe_transcript(text)),
            user_speaking=lambda: self._intake.user_speaking,
            is_started=lambda: self._started,
            is_stopping=lambda: self._stopping,
        )
        self._intake.bind_delivery(self._delivery)

    @property
    def endpointer(self) -> Endpointer:
        """The turn-taking endpointer driving this session."""
        return self._endpointer

    @property
    def current_affect(self):
        """The latest EQ affect read for this session, or None.

        The read exists so a future TTS with prosody controls
        has something to consume (see note_affect_read); today
        it informs nothing audible.
        """
        return self._latest_affect_read

    def note_affect_read(self, read) -> None:
        """Record the talker's latest EQ affect read (S11).

        PROSODY NO-OP, stated plainly: this records the read and
        applies NOTHING. The current TTS stage synthesises from
        text + voice id only (``TtsEngine.synthesize(text, *,
        voice)``) — it has no rate, pitch or SSML control — so
        there is no honest way to make the voice sound slower,
        calmer or more careful today, and this hook does not
        pretend otherwise. When a TTS with prosody controls
        lands, it consumes ``current_affect`` here.
        """
        self._latest_affect_read = read

    # -- availability --------------------------------------------------

    def unavailable_reasons(self) -> dict[str, str]:
        """Stage name -> reason, for every stage that cannot run."""
        reasons: dict[str, str] = {}
        for name, stage in (
            ("vad", self._vad),
            ("stt", self._stt),
            ("tts", self._tts),
            ("transport", self._transport),
        ):
            reason = stage.unavailable_reason()
            if reason is not None:
                reasons[name] = reason
        return reasons

    # -- lifecycle -----------------------------------------------------

    async def start(self) -> dict | None:
        """Open the transport and stages; returns webrtc_config.

        Raises VoiceUnavailableError — before opening anything —
        when any stage reports itself unavailable.
        """
        reasons = self.unavailable_reasons()
        if reasons:
            raise VoiceUnavailableError(_unavailable_message(reasons))
        if self._started:
            return self._webrtc_config
        opened: list = []
        try:
            self._webrtc_config = await self._transport.open()
            opened.append(self._transport)
            for stage in (self._vad, self._stt, self._tts):
                await stage.open()
                opened.append(stage)
        except Exception:
            for stage in reversed(opened):
                try:
                    await stage.close()
                except Exception:  # best-effort rollback of a failed start
                    logger.debug("voice stage close during rollback failed", exc_info=True)
            raise
        self._started = True
        return self._webrtc_config

    async def stop(self) -> None:
        """Close the transport and every stage. Idempotent."""
        self._stopping = True
        self._delivery.request_interrupt()
        self._intake.reset_barge()
        try:
            await self._transport.close()
        except Exception:  # a broken transport must not strand the stages
            logger.debug("voice transport close failed", exc_info=True)
        for stage in (self._vad, self._stt, self._tts):
            try:
                await stage.close()
            except Exception:  # idempotent teardown: log and continue
                logger.debug("voice stage close failed", exc_info=True)
        self._started = False

    # -- RTC negotiation (F-12a) ---------------------------------------
    # Signalling for the production transport, delegated: the
    # pipeline owns the transport, the WebSocket layer owns the
    # socket, and these methods are the seam between them. A
    # transport that does not negotiate (a scripted test double)
    # fails offer/ICE calls closed with VoiceUnavailableError; the
    # candidate listener is best-effort — such a transport simply
    # never pushes candidates.

    async def handle_rtc_offer(self, offer: dict) -> dict:
        """Answer the renderer's RTC offer via the transport."""
        handler = getattr(self._transport, "handle_offer", None)
        if handler is None:
            raise VoiceUnavailableError(
                "voice unavailable: this session's transport does "
                "not negotiate RTC")
        return await handler(offer)

    async def add_rtc_ice_candidate(self, candidate: dict | None) -> None:
        """Trickle one renderer ICE candidate into the transport
        (None = end-of-candidates)."""
        handler = getattr(self._transport, "add_remote_candidate", None)
        if handler is None:
            raise VoiceUnavailableError(
                "voice unavailable: this session's transport does "
                "not negotiate RTC")
        await handler(candidate)

    def set_rtc_ice_listener(self, listener) -> None:
        """Register the push channel for the transport's own
        gathered ICE candidates (wire-shaped dicts, or None for
        end-of-candidates)."""
        setter = getattr(self._transport, "set_local_candidate_listener", None)
        if setter is not None:
            setter(listener)

    # -- main loop -------------------------------------------------------

    async def run(self) -> None:
        """Consume frames until the transport ends or stop() lands."""
        if not self._started:
            raise VoiceUnavailableError("voice unavailable: session not started")
        try:
            await self._intake.run()
        finally:
            # Turns and partials run as delivery tasks off the
            # frame loop (the loop must stay free for barge-in
            # detection, and the partial task works off the intake
            # buffer); never orphan one when the loop ends.
            self._delivery.cancel_pending()

    # -- the delivery half's public surface ------------------------------
    # Thin delegations: the behaviour — and its documentation —
    # lives on VoiceDelivery; these methods are the session
    # contract the websocket layer and tests call.

    async def interrupt_output(self) -> dict:
        """Stop the in-flight output (a reply turn or an
        announcement); report what was actually heard. See
        ``VoiceDelivery.interrupt_output`` for the contract."""
        return await self._delivery.interrupt_output()

    def note_playback_progress(self, played_seq: int) -> None:
        """Record a renderer playback report (N09). See
        ``VoiceDelivery.note_playback_progress``."""
        self._delivery.note_playback_progress(played_seq)

    @property
    def _played_seq(self) -> int:
        """The delivery half's playback position (N09), exposed
        here because the playback-progress route suites read the
        accounting state off the pipeline object."""
        return self._delivery._played_seq

    @_played_seq.setter
    def _played_seq(self, value: int) -> None:
        self._delivery._played_seq = value

    async def speak_announcement(
        self, text: str, *, announcement_id: str | None = None,
        kind: str | None = None, message_id: str | None = None,
    ) -> bool:
        """Speak out-of-turn text — a finished background task's
        summary (E8-5.1b). See ``VoiceDelivery.speak_announcement``
        for the contract. The quiet-wait bound is this module's
        ``_ANNOUNCEMENT_QUIET_TIMEOUT_S``, read at call time and
        handed down — the value the E7-06 suites patch."""
        return await self._delivery.speak_announcement(
            text,
            announcement_id=announcement_id,
            kind=kind,
            message_id=message_id,
            quiet_timeout_s=_ANNOUNCEMENT_QUIET_TIMEOUT_S,
        )


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def fast_chat_talker(provider, *, session_id: str | None,
                     on_affect_read=None) -> Talker:
    """Adapt ``run_fast_chat`` to the pipeline's talker contract:
    an async generator of reply text deltas for one user turn.

    History is loaded per turn from the session store, so voice
    turns and typed turns share one conversation.

    ``on_affect_read`` (S11) is forwarded to run_fast_chat: each
    turn's EQ read is reported there so the session owner can
    record it on the pipeline (note_affect_read). The EQ
    briefing itself rides inside run_fast_chat's prompt, exactly
    as for typed chat — there is no voice-specific fork.
    """

    async def talk(text: str) -> AsyncIterator[str]:
        from aether_engine.langgraph.fast_chat import (
            load_session_history,
            run_fast_chat,
        )

        history = load_session_history(session_id, text)
        async for event in run_fast_chat(
            text, provider, history, session_id=session_id,
            on_affect_read=on_affect_read):
            if event.type == EventType.TASK_PROGRESS:
                delta = event.payload.get("text_delta")
                if delta:
                    yield delta
            elif event.type == EventType.TASK_FAILED:
                raise VoiceTalkerError(
                    event.payload.get("error") or "fast chat failed")

    return talk


def create_voice_pipeline(
    *,
    session_id: str,
    talker: Talker,
    emit: EmitFn,
    on_user_final: TurnCallback | None = None,
    on_assistant_final: TurnCallback | None = None,
    on_turn_error: TurnCallback | None = None,
    vad: VadEngine | None = None,
    stt: SttEngine | None = None,
    tts: TtsEngine | None = None,
    transport: VoiceTransport | None = None,
    escalation_classifier: EscalationClassifier | None = None,
    on_escalation: TurnCallback | None = None,
    escalation_ack: str = DEFAULT_ESCALATION_ACK,
    endpointer: Endpointer | None = None,
    endpointer_config: EndpointerConfig | None = None,
    stt_model: str | None = None,
    tts_voice: str = DEFAULT_VOICE,
) -> tuple[VoicePipeline | None, str | None]:
    """Build a pipeline from the production stages (or injected
    ones). Returns ``(pipeline, None)`` when every stage can run,
    or ``(None, "voice unavailable: ...")`` — nothing is opened
    either way; opening happens in ``pipeline.start()``.

    ``stt_model`` / ``tts_voice`` are the speech config (model hub,
    H3), read by the caller at session start: the STT engine is
    built with the model here and holds it for the pipeline's
    whole life — there is no swap seam, so a config change applies
    to the NEXT session (grill P3(a)). An injected ``stt`` wins
    over ``stt_model``. The voice, by contrast, reaches the TTS
    stage as a per-sentence parameter from ``tts_voice``.
    """
    pipeline = VoicePipeline(
        session_id=session_id,
        vad=vad if vad is not None else SileroVad(),
        stt=stt if stt is not None else (
            FasterWhisperStt(model=stt_model) if stt_model
            else FasterWhisperStt()),
        tts=tts if tts is not None else KokoroTts(),
        transport=transport if transport is not None else WebRtcVoiceTransport(),
        talker=talker,
        emit=emit,
        on_user_final=on_user_final,
        on_assistant_final=on_assistant_final,
        on_turn_error=on_turn_error,
        endpointer=endpointer,
        endpointer_config=endpointer_config,
        tts_voice=tts_voice,
        escalation_classifier=escalation_classifier,
        on_escalation=on_escalation,
        escalation_ack=escalation_ack,
    )
    reasons = pipeline.unavailable_reasons()
    if reasons:
        return None, _unavailable_message(reasons)
    return pipeline, None
