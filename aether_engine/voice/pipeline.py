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
* Degradation (S15): a pipeline built by
  ``create_voice_pipeline`` carries a ladder plan
  (``voice/degradation.py``) decided before anything opens —
  from the stages' own availability, the S10 capability
  matrix's local-voice verdict, and this machine's measured
  STT latency. Stages with a rung no longer refuse the
  session: a missing TTS starts the session text-only, a
  missing Silero runtime falls back to energy endpointing,
  and mid-session the delivery half executes the tracker's
  decisions through this class's seams (STT engine swap,
  text-only flip, fatal end). A pipeline constructed directly
  (tests, embeddings) has no ladder and keeps the binary
  contract above, exactly.

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
import asyncio
import logging
from collections.abc import AsyncIterator, Callable

from aether_common.contracts import EventType
from aether_engine.voice import VoiceTalkerError, VoiceUnavailableError
from aether_engine.voice.degradation import (
    PROFILE_DEGRADED,
    PROFILE_FULL,
    SessionDegradation,
    plan_voice_session,
    record_voice_stage_sample,
    stt_fallback_chain,
    stt_p95_by_model,
)
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
from aether_engine.voice.vad import (
    Endpointer,
    EndpointerConfig,
    EnergyVad,
    SileroVad,
    VadEngine,
)

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
        degradation: SessionDegradation | None = None,
        stt_factory: Callable[[str], SttEngine] | None = None,
    ):
        self.session_id = session_id
        # S15: the session's degradation tracker (None = no
        # ladder; the binary availability contract applies) and
        # the factory the STT swap/load-fallback seams build
        # replacement engines with (None when the STT stage was
        # injected — an injected engine cannot be rebuilt, so
        # swaps honestly report False).
        self._degradation = degradation
        self._stt_factory = stt_factory
        self._fatal_reason: str | None = None
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
            degradation=degradation,
            swap_stt=self.swap_stt_engine,
            on_fatal=self._end_session_fatal,
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

    def _speech_output_enabled(self) -> bool:
        """Whether this session speaks its replies. False only
        under a ladder plan whose TTS rung is off (the stage
        could not run at plan time): the TTS stage is then
        neither checked nor opened, and the delivery half never
        calls it."""
        return self._degradation is None or self._degradation.tts_enabled

    def unavailable_reasons(self) -> dict[str, str]:
        """Stage name -> reason, for every stage that cannot run."""
        reasons: dict[str, str] = {}
        for name, stage in (
            ("vad", self._vad),
            ("stt", self._stt),
            ("tts", self._tts),
            ("transport", self._transport),
        ):
            if name == "tts" and not self._speech_output_enabled():
                continue
            reason = stage.unavailable_reason()
            if reason is not None:
                reasons[name] = reason
        return reasons

    @property
    def voice_profile(self) -> str:
        """The session's current degradation profile (S15): the
        plan's profile at start, updated live as transitions
        land (text_only after a TTS failure, unavailable at a
        fatal end). "full" for a pipeline built without a
        ladder."""
        if self._degradation is None:
            return PROFILE_FULL
        return self._degradation.profile

    @property
    def start_notices(self) -> tuple[str, ...]:
        """The user-facing notices the start response carries
        (S15): every deviation from the full cascade the ladder
        planned, plus a load-failure substitution added by
        start() itself. Empty for a full-cascade session."""
        if self._degradation is None:
            return ()
        return self._degradation.notices

    @property
    def fatal_reason(self) -> str | None:
        """Why the ladder ended this session mid-flight (S15),
        or None while it lives."""
        return self._fatal_reason

    # -- lifecycle -----------------------------------------------------

    async def start(self) -> dict | None:
        """Open the transport and stages; returns webrtc_config.

        Raises VoiceUnavailableError — before opening anything —
        when any stage reports itself unavailable (under a
        ladder plan, the TTS stage is exempt when the plan's
        text-only rung is active: it is never opened at all).
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
            await self._vad.open()
            opened.append(self._vad)
            await self._open_stt()
            opened.append(self._stt)
            if self._speech_output_enabled():
                await self._tts.open()
                opened.append(self._tts)
        except Exception:
            for stage in reversed(opened):
                try:
                    await stage.close()
                except Exception:  # best-effort rollback of a failed start
                    logger.debug("voice stage close during rollback failed", exc_info=True)
            raise
        self._started = True
        return self._webrtc_config

    async def _open_stt(self) -> None:
        """Open the STT stage, with the ladder's load-failure
        retry (S15): a configured model that cannot even LOAD
        (not downloaded, corrupt cache) retries down the
        smaller-model chain before the start is allowed to
        fail — each candidate is built fresh by the factory and
        opened, the first that loads is adopted (the delivery
        half is repointed at it) and the substitution joins the
        start notices. With no ladder or no factory wired, or
        when every candidate fails, the original error
        propagates and start()'s rollback runs as before."""
        try:
            await self._stt.open()
            return
        except Exception:
            if self._degradation is None or self._stt_factory is None:
                raise
            configured = self._degradation.stt_model
            adopted: SttEngine | None = None
            adopted_model: str | None = None
            for candidate in stt_fallback_chain(configured):
                try:
                    engine = self._stt_factory(candidate)
                    await engine.open()
                except Exception:  # this candidate cannot load either; try smaller
                    logger.debug(
                        "voice STT load fallback: %r failed to load",
                        candidate, exc_info=True)
                    continue
                adopted, adopted_model = engine, candidate
                break
            if adopted is None or adopted_model is None:
                raise
            old = self._stt
            self._stt = adopted
            self._delivery.replace_stt(adopted)
            self._degradation.stt_model = adopted_model
            if self._degradation.profile == PROFILE_FULL:
                self._degradation.profile = PROFILE_DEGRADED
            self._degradation.add_start_notice(
                f"The '{configured}' speech model could not be "
                f"loaded, so this session is using "
                f"'{adopted_model}' instead — it may be a little "
                "less accurate.")
            try:
                await old.close()
            except Exception:  # the old engine never opened; close is best-effort
                logger.debug("voice STT load fallback: old engine close failed",
                             exc_info=True)

    # -- degradation execution seams (S15) ------------------------------

    async def swap_stt_engine(self, model: str) -> bool:
        """Replace the STT engine with a fresh one for *model*
        (the delivery half's step-down/failure-swap execution).
        The new engine is opened BEFORE the swap lands, so a
        load failure changes nothing and returns False; on
        success the delivery half is repointed at the new
        engine and the old one is closed. False whenever no
        factory is wired (an injected STT cannot be rebuilt)."""
        if self._stt_factory is None:
            return False
        try:
            engine = self._stt_factory(model)
            await engine.open()
        except Exception:  # a swap candidate that cannot load is a decision input
            logger.warning(
                "voice STT swap: model %r failed to load", model,
                exc_info=True)
            return False
        old = self._stt
        self._stt = engine
        self._delivery.replace_stt(engine)
        try:
            await old.close()
        except Exception:  # teardown of the replaced engine is best-effort
            logger.debug("voice STT swap: old engine close failed", exc_info=True)
        return True

    async def _end_session_fatal(self, reason: str) -> None:
        """The delivery half's fatal hook (S15): the STT ladder
        decided the session cannot honestly continue. The
        status push carrying the reason has already been
        emitted by the delivery half; the stop itself is
        scheduled rather than run inline, because stop() tears
        down the transport the frame loop — the delivery
        half's caller — is running under."""
        self._fatal_reason = reason
        asyncio.ensure_future(self.stop())

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


def _matrix_voice_verdict(report: dict | None) -> dict | None:
    """The S10 matrix's ``local_voice`` verdict for the planner:
    the injected report when given, else the capability
    module's current report (cached/persisted per its own
    TTL). Reading the matrix must never break a session start
    — any failure reads as "no verdict", and planning proceeds
    on stage reasons alone."""
    try:
        if report is None:
            from aether_engine.machine_capability import (
                machine_capability_report,
            )

            report = machine_capability_report()
    except Exception:  # the matrix is advisory input, never a start dependency
        logger.debug("voice planning: capability report unreadable",
                     exc_info=True)
        return None
    features = report.get("features") if isinstance(report, dict) else None
    for feature in features or []:
        if isinstance(feature, dict) and feature.get("id") == "local_voice":
            return feature
    return None


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
    capability_report: dict | None = None,
    latency_store=None,
) -> tuple[VoicePipeline | None, str | None]:
    """Build a pipeline from the production stages (or injected
    ones), planned through the S15 degradation ladder. Returns
    ``(pipeline, None)`` when the ladder finds a runnable
    profile — full, degraded, or text-only — or
    ``(None, "voice unavailable: ...")`` when no rung exists —
    nothing is opened either way; opening happens in
    ``pipeline.start()``.

    ``stt_model`` / ``tts_voice`` are the speech config (model hub,
    H3), read by the caller at session start: the STT engine is
    built with the configured model here, and a config change
    applies to the NEXT session (grill P3(a)). The only
    mid-session model changes are the ladder's own step-downs,
    executed through the pipeline's swap seam with a factory
    that rebuilds this same stage — never from config. An
    injected ``stt`` wins over ``stt_model`` (and, being
    un-rebuildable, disables the swap seams). The voice, by
    contrast, reaches the TTS stage as a per-sentence parameter
    from ``tts_voice``.

    ``capability_report`` / ``latency_store`` are the planner's
    S10-matrix and measured-latency inputs, injectable for
    tests; by default the live report and the global store
    are read.
    """
    vad_stage = vad if vad is not None else SileroVad()
    stt_stage = stt if stt is not None else (
        FasterWhisperStt(model=stt_model) if stt_model
        else FasterWhisperStt())
    tts_stage = tts if tts is not None else KokoroTts()
    transport_stage = (
        transport if transport is not None else WebRtcVoiceTransport())
    configured_model = (
        stt_model or getattr(stt_stage, "model_name", None) or "small")
    plan = plan_voice_session(
        stage_reasons={
            "vad": vad_stage.unavailable_reason(),
            "stt": stt_stage.unavailable_reason(),
            "tts": tts_stage.unavailable_reason(),
            "transport": transport_stage.unavailable_reason(),
        },
        configured_stt_model=configured_model,
        matrix_voice=_matrix_voice_verdict(capability_report),
        stt_p95_by_model=stt_p95_by_model(
            [configured_model], store=latency_store),
        vad_replaceable=vad is None,
    )
    if plan.unavailable_reason is not None:
        return None, plan.unavailable_reason
    if plan.vad_fallback and vad is None:
        # The factory's own Silero stage cannot run: the
        # ladder's energy endpointing takes its place. (An
        # injected VAD is never swapped — see the planner's
        # vad_replaceable rule, which refuses instead.)
        vad_stage = EnergyVad()
    degradation = SessionDegradation(
        plan,
        recorder=lambda stage, model, duration_ms: (
            record_voice_stage_sample(
                stage, model, duration_ms, store=latency_store)),
    )
    pipeline = VoicePipeline(
        session_id=session_id,
        vad=vad_stage,
        stt=stt_stage,
        tts=tts_stage,
        transport=transport_stage,
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
        degradation=degradation,
        stt_factory=(
            (lambda model: FasterWhisperStt(model=model))
            if stt is None else None),
    )
    return pipeline, None
