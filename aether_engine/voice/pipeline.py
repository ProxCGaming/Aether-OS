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
"""
import asyncio
import base64
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from aether_common.contracts import Event, EventType
from aether_engine.voice import VoiceTalkerError, VoiceUnavailableError
from aether_engine.voice.rtc import WebRtcVoiceTransport
from aether_engine.voice.stt import FasterWhisperStt, SttEngine
from aether_engine.voice.tts import DEFAULT_VOICE, KokoroTts, TtsEngine
from aether_engine.voice.vad import Endpointer, EndpointerConfig, SileroVad, VadEngine

logger = logging.getLogger("aether_engine.voice.pipeline")

Talker = Callable[[str], AsyncIterator[str]]
EmitFn = Callable[[Event], Awaitable[None]]
TurnCallback = Callable[[str], Awaitable[None]]
EscalationClassifier = Callable[[str], Awaitable[bool]]

#: The spoken acknowledgement for an escalated turn (E8-5.1b): it
#: masks the supervisor's latency — the job is already running by
#: the time this is heard — and it is the turn's committed reply.
DEFAULT_ESCALATION_ACK = (
    "On it — I'll run that in the background "
    "and let you know when it's done."
)

_SENTENCE_BOUNDARY = re.compile(r"[.!?…]+[\"'”’)\]]*\s+")
_WORD = re.compile(r"\S+")


def _split_sentences(buffer: str) -> tuple[list[str], str]:
    """Split off sentences whose boundary is proven (terminator +
    following whitespace); the tail stays buffered for more deltas."""
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_BOUNDARY.finditer(buffer):
        sentences.append(buffer[start:match.end()])
        start = match.end()
    return sentences, buffer[start:]


def _heard_prefix(sentence: str, emitted: int, total: int) -> str:
    """The heard part of a sentence whose audio was interrupted:
    the chunk-proportional prefix of its words (E8-5.1b word
    granularity for the only-actually-heard commit rule)."""
    if total <= 0 or emitted <= 0:
        return ""
    if emitted >= total:
        return sentence
    words = list(_WORD.finditer(sentence))
    if not words:
        return sentence
    heard_words = (len(words) * emitted) // total
    if heard_words <= 0:
        return ""
    if heard_words >= len(words):
        return sentence
    end = words[heard_words - 1].end()
    if end < len(sentence) and sentence[end] == " ":
        end += 1
    return sentence[:end]


def _word_deltas(text: str) -> list[str]:
    """Split text into word deltas, each carrying its trailing
    whitespace, so the deltas rejoin into the exact original."""
    deltas: list[str] = []
    pos = 0
    for match in _WORD.finditer(text):
        end = match.end()
        while end < len(text) and text[end] == " ":
            end += 1
        deltas.append(text[pos:end])
        pos = end
    if pos < len(text):
        deltas.append(text[pos:])
    return deltas


async def _static_deltas(text: str) -> AsyncIterator[str]:
    """Word-sized deltas for text that already exists whole (an
    escalation acknowledgement, an announcement): the transcript
    still renders it word by word, as Q28 requires."""
    for delta in _word_deltas(text):
        yield delta


class VoiceTransport(Protocol):
    """Audio frame source + session config (SmallWebRTC seam)."""

    def unavailable_reason(self) -> str | None:
        """Why this transport cannot run here, or None when it can."""
        ...

    async def open(self) -> dict | None:
        """Open the peer connection; returns the webrtc_config the
        desktop side needs, or None."""
        ...

    def frames(self) -> AsyncIterator[bytes]:
        """Raw PCM16 frames from the user's microphone."""
        ...

    async def close(self) -> None:
        """Tear the connection down. Idempotent; ends ``frames()``."""
        ...


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
        self._vad = vad
        self._stt = stt
        self._tts = tts
        self._transport = transport
        self._talker = talker
        self._emit = emit
        self._on_user_final = on_user_final
        self._on_assistant_final = on_assistant_final
        self._on_turn_error = on_turn_error
        if endpointer is not None:
            self._endpointer = endpointer
        elif endpointer_config is not None:
            self._endpointer = Endpointer(endpointer_config)
        else:
            self._endpointer = Endpointer()
        self._tts_voice = tts_voice
        self._sample_rate = sample_rate
        self._partial_min_audio_ms = partial_min_audio_ms
        # E8-5.1b escalation seams: both must be wired for a turn to
        # escalate; either missing keeps every turn with the talker
        # (the 5.1a behaviour).
        self._escalation_classifier = escalation_classifier
        self._on_escalation = on_escalation
        self._escalation_ack = escalation_ack

        self._started = False
        self._stopping = False
        self._webrtc_config: dict | None = None
        self._utterance = bytearray()
        self._last_partial_text = ""
        self._since_partial_ms = 0
        self._pending_partial_done = False
        self._turn_active = False
        self._turn_task: asyncio.Task | None = None
        self._announcing = False
        # N10: the identity of the announcement currently speaking
        # (None for reply turns), so a barge-in landing mid-
        # announcement can tag its event with it.
        self._announcing_id: str | None = None
        self._interrupt_requested = False
        # Heard-words tracking (E8-5.1b, N09): _heard_entries holds
        # one record per sentence whose audio the current output
        # activity has emitted — its text, its first chunk_seq, its
        # chunk total and how many chunks were emitted — in
        # emission order; _heard_text_now() derives the heard text
        # from these records (playback reports when the renderer
        # sends them, emission counts when it never does).
        self._heard_entries: list[dict] = []
        # N09 playback accounting: session-scoped, so both survive
        # _reset_heard() — chunk seqs are never reused, and a late
        # report for a previous activity's chunks can only name
        # seqs below the next activity's first chunk. _played_seq
        # is the highest chunk_seq the renderer confirmed FINISHED
        # playing; _progress_seen flips on the first well-formed
        # report (including the -1 "reporting live" one).
        self._next_chunk_seq = 0
        self._played_seq = -1
        self._progress_seen = False
        # The heard text published at a barge-in cut, frozen so the
        # history commit — which lands when the output loop notices
        # the interrupt — is exactly the published text.
        self._frozen_heard: str | None = None
        # Barge-in detection state (E8-5.1b): a separate endpointer
        # watches frames while output plays, so the main
        # endpointer's utterance state is never corrupted.
        # S4-M16: the barge-in endpointer shares the config (so it
        # carries a Smart Turn policy object too) but never observes
        # a transcript, so it degrades to silence-only endpointing —
        # barge-in timing is deliberately unchanged by Smart Turn.
        self._barge_endpointer = Endpointer(self._endpointer.config)
        self._barge_frames: list[bytes] = []
        self._barge_speech_ms = 0

    @property
    def endpointer(self) -> Endpointer:
        """The turn-taking endpointer driving this session."""
        return self._endpointer

    @property
    def _output_active(self) -> bool:
        """A reply turn or an announcement is currently speaking."""
        return self._turn_active or self._announcing

    def _reset_heard(self) -> None:
        self._heard_entries = []
        self._frozen_heard = None

    def note_playback_progress(self, played_seq: int) -> None:
        """Record a renderer playback report (N09): the highest
        chunk_seq the desktop has FINISHED playing, or -1 for
        "reporting live, nothing finished yet". Fire-and-forget —
        the websocket layer calls this per inbound
        VOICE_PLAYBACK_PROGRESS event; malformed values are
        ignored, never fatal, and reports only ever move the
        position forward. A report arriving after a cut was
        published for the current activity does not move that
        activity's accounting (the cut is final); it still arms
        playback mode for what follows."""
        try:
            seq = int(played_seq)
        except (TypeError, ValueError):
            return
        self._progress_seen = True
        if self._frozen_heard is not None:
            return
        self._played_seq = max(self._played_seq, seq)

    def _played_count(self, entry: dict) -> int:
        """How many of one emitted sentence's chunks the renderer
        has confirmed played. A sentence's chunk seqs are
        contiguous, assigned in emission order."""
        played = self._played_seq - entry["first_seq"] + 1
        return max(0, min(entry["total"], played))

    def _heard_text_now(self) -> str:
        """Everything the listener has actually heard of the
        current output activity.

        Once the renderer has reported any playback progress this
        session, only PLAYED chunks count (N09): fully-played
        sentences in full, the first partially played one as its
        chunk-proportional word prefix, and nothing from later
        sentences however much of them was emitted. When no
        progress has ever been reported (a client without playback
        reporting), the same walk runs on EMITTED counts — the
        pre-N09 estimate the older suites pin."""
        if self._frozen_heard is not None:
            return self._frozen_heard
        parts: list[str] = []
        for entry in self._heard_entries:
            total = entry["total"]
            if self._progress_seen:
                available = self._played_count(entry)
            else:
                available = min(entry["emitted"], total)
            if available >= total:
                parts.append(entry["sentence"])
                continue
            if available > 0:
                parts.append(_heard_prefix(
                    entry["sentence"], available, total))
            break
        return "".join(parts)

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
        self._interrupt_requested = True
        self._barge_frames = []
        self._barge_speech_ms = 0
        self._barge_endpointer.reset()
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

    # -- barge-in -------------------------------------------------------

    async def _await_output_settle(self) -> None:
        """Bounded wait for the in-flight output activity (a reply
        turn or an announcement) to wind down once the interrupt
        flag is set."""
        for _ in range(2000):
            if not self._output_active:
                break
            await asyncio.sleep(0.002)

    async def interrupt_output(self) -> dict:
        """Stop the in-flight output (a reply turn or an
        announcement); report what was actually heard.

        Returns the VOICE_BARGE_IN payload fields:
        ``{"stopped_at_char", "committed_text"}``. The committed
        text — only the words actually heard, at word granularity —
        is also what ``on_assistant_final`` commits for the turn:
        the snapshot published here is frozen, and the commit reads
        exactly it.

        Timing (N09): when the renderer reports playback progress,
        the accounting is independent of the output loop, so the
        cut is published IMMEDIATELY — the pre-N09 code first polled
        up to ~4 s for the output task to settle, while the desktop
        kept playing the unheard tail. The settle wait still runs
        afterwards in that mode, but only as sequencing for the
        caller (the barge-in path replays the interrupting frames
        once the old output has wound down). With emission-based
        accounting (no progress ever reported) the heard text is
        only final once the output loop has noticed the interrupt,
        so the settle still precedes the snapshot, as it did
        pre-N09.

        When the interrupted output is an announcement, the event
        also carries its ``announcement_id`` (N10): the heard prefix
        of an announcement is NOT a new message — the announced
        task's own message already stands in chat — and the tag is
        how the desktop knows not to commit it as one.
        """
        announcement_id = self._announcing_id if self._announcing else None
        if self._output_active:
            self._interrupt_requested = True
            if not self._progress_seen:
                await self._await_output_settle()
            heard = self._heard_text_now()
            self._frozen_heard = heard
            payload = {
                "session_id": self.session_id,
                "stopped_at_char": len(heard),
                "committed_text": heard,
            }
            if announcement_id is not None:
                payload["announcement_id"] = announcement_id
            await self._emit(Event(
                type=EventType.VOICE_BARGE_IN,
                payload=payload,
            ))
            if self._progress_seen:
                await self._await_output_settle()
        heard = self._heard_text_now()
        return {
            "stopped_at_char": len(heard),
            "committed_text": heard,
        }

    # -- main loop -------------------------------------------------------

    async def run(self) -> None:
        """Consume frames until the transport ends or stop() lands."""
        if not self._started:
            raise VoiceUnavailableError("voice unavailable: session not started")
        try:
            async for data in self._transport.frames():
                if self._stopping:
                    break
                await self._consume_frame(data)
        finally:
            # Turns run as their own tasks (the frame loop must stay
            # free for barge-in detection); never orphan one.
            if self._turn_task is not None and not self._turn_task.done():
                self._turn_task.cancel()
            self._turn_active = False

    async def _consume_frame(self, data: bytes) -> None:
        if self._output_active:
            await self._consume_frame_during_output(data)
            return
        if self._barge_frames:
            # The reply ended while the user was mid-interruption:
            # their captured speech starts the next utterance.
            frames = self._barge_frames
            self._barge_frames = []
            self._barge_speech_ms = 0
            self._barge_endpointer.reset()
            for captured in frames:
                if self._stopping:
                    return
                await self._consume_frame(captured)
            if self._stopping:
                return
            if self._output_active:
                # The replay itself started the next turn.
                await self._consume_frame_during_output(data)
                return
        await self._consume_main_frame(data)

    async def _consume_frame_during_output(self, data: bytes) -> None:
        """Full-duplex watch (E8-5.1b): while output plays, frames
        feed the barge-in endpointer instead of the main path.
        Sustained speech is a barge-in — playback stops at once
        and the captured frames replay into the main path as the
        next utterance. Anything shorter than the min-speech
        guard (a cough, a backchannel) is discarded."""
        probability = await self._vad.probability(data, self._sample_rate)
        frame_ms = round(len(data) / 2 / self._sample_rate * 1000)
        endpointer = self._barge_endpointer
        event = endpointer.feed(probability, frame_ms)
        if event == "speech_started":
            self._barge_frames = [data]
            self._barge_speech_ms = frame_ms
            return
        if event == "utterance_discarded":
            self._barge_frames = []
            self._barge_speech_ms = 0
            return
        if event == "utterance_end":
            # The user spoke a whole short utterance over the reply.
            self._barge_frames.append(data)
            await self._trigger_barge_in()
            return
        if not endpointer.in_speech:
            return
        self._barge_frames.append(data)
        if probability >= endpointer.config.speech_threshold:
            self._barge_speech_ms += frame_ms
        if self._barge_speech_ms >= endpointer.config.min_speech_ms:
            await self._trigger_barge_in()

    async def _trigger_barge_in(self) -> None:
        frames = self._barge_frames
        self._barge_frames = []
        self._barge_speech_ms = 0
        self._barge_endpointer.reset()
        await self.interrupt_output()
        if self._stopping:
            return
        for captured in frames:
            if self._stopping:
                return
            await self._consume_frame(captured)

    async def _consume_main_frame(self, data: bytes) -> None:
        probability = await self._vad.probability(data, self._sample_rate)
        frame_ms = round(len(data) / 2 / self._sample_rate * 1000)
        endpointer = self._endpointer
        event = endpointer.feed(probability, frame_ms)
        if event == "speech_started":
            self._utterance = bytearray(data)
            self._last_partial_text = ""
            self._since_partial_ms = 0
            self._pending_partial_done = False
            return
        if event == "utterance_discarded":
            self._utterance = bytearray()
            return
        if event == "utterance_end":
            self._utterance += data
            await self._finish_utterance()
            return
        if not endpointer.in_speech:
            return
        self._utterance += data
        if probability >= endpointer.config.speech_threshold:
            self._since_partial_ms += frame_ms
            if self._since_partial_ms >= self._partial_min_audio_ms:
                self._since_partial_ms = 0
                await self._emit_partial()
        elif not self._pending_partial_done:
            # First trailing-silence frame: one more partial with the
            # full utterance context before the endpoint lands.
            self._pending_partial_done = True
            await self._emit_partial()

    async def _emit_partial(self) -> None:
        if not self._utterance:
            return
        try:
            text = await self._stt.transcribe(
                bytes(self._utterance), sample_rate=self._sample_rate)
        except Exception:  # a partial is provisional: never kill the loop
            logger.debug("voice partial transcription failed", exc_info=True)
            return
        text = (text or "").strip()
        if text and text != self._last_partial_text:
            self._last_partial_text = text
            # S4-M16 (Smart Turn): feed the partial into the main
            # endpointer so its turn-completion policy can bend the
            # trailing-silence window for this utterance. The
            # barge-in endpointer is intentionally NOT fed — it
            # never observes transcripts and stays silence-only.
            self._endpointer.observe_transcript(text)
            await self._emit(Event(
                type=EventType.VOICE_PARTIAL_TRANSCRIPT,
                payload={
                    "session_id": self.session_id,
                    "speaker": "user",
                    "text": text,
                    "is_final": False,
                },
            ))

    async def _finish_utterance(self) -> None:
        audio = bytes(self._utterance)
        self._utterance = bytearray()
        self._last_partial_text = ""
        try:
            text = await self._stt.transcribe(audio, sample_rate=self._sample_rate)
        except Exception as e:  # noqa: BLE001 — a dead STT stage drops the turn, not the session
            logger.warning(f"voice transcription failed: {e}")
            return
        text = (text or "").strip()
        if not text:
            return
        await self._emit(Event(
            type=EventType.VOICE_PARTIAL_TRANSCRIPT,
            payload={
                "session_id": self.session_id,
                "speaker": "user",
                "text": text,
                "is_final": True,
            },
        ))
        if self._on_user_final is not None:
            await self._on_user_final(text)
        # Route the turn (E8-5.1b). Turn state initialises HERE,
        # synchronously, before the turn task is scheduled: the
        # frame loop keeps running (full-duplex), and a barge-in
        # detected before the task's first slice must not be lost
        # to a reset inside the task.
        self._turn_active = True
        self._interrupt_requested = False
        self._reset_heard()
        if await self._should_escalate(text):
            self._turn_task = asyncio.ensure_future(
                self._run_escalation_turn(text))
        else:
            self._turn_task = asyncio.ensure_future(self._run_turn(text))

    async def _should_escalate(self, text: str) -> bool:
        """Ask the wired classifier whether this turn is work.
        Both seams must be present, and a classifier failure
        degrades to the talker — the turn is never lost to a
        routing error."""
        if self._escalation_classifier is None or self._on_escalation is None:
            return False
        try:
            return bool(await self._escalation_classifier(text))
        except Exception:  # degrade to chat, never drop the turn
            logger.warning(
                "voice escalation classification failed; "
                "keeping the turn with the talker", exc_info=True)
            return False

    async def _run_turn(self, text: str) -> None:
        """One talker turn. Turn state was initialised by
        ``_finish_utterance`` before this task was scheduled."""
        try:
            await self._deliver_reply(self._talker(text))
        except Exception as e:  # noqa: BLE001 — the talker failing must not kill the session
            logger.warning(f"voice turn failed: {e}")
            if self._on_turn_error is not None:
                await self._on_turn_error(str(e))
        finally:
            self._turn_active = False

    async def _run_escalation_turn(self, text: str) -> None:
        """Escalation turn (E8-5.1b): the job launches FIRST —
        the background task lane is a separate execution and never
        waits on this lane — then the spoken acknowledgement masks
        its latency as this turn's reply. If the launch itself
        fails, the talker answers instead: the user is never left
        with silence where an answer was possible."""
        try:
            launched = False
            try:
                await self._on_escalation(text)
                launched = True
            except Exception:  # fall back, never strand the turn
                logger.warning(
                    "voice escalation launch failed; "
                    "falling back to the talker", exc_info=True)
            if launched:
                await self._deliver_reply(_static_deltas(self._escalation_ack))
            else:
                await self._deliver_reply(self._talker(text))
        except Exception as e:  # noqa: BLE001 — a failed turn must not kill the session
            logger.warning(f"voice escalation turn failed: {e}")
            if self._on_turn_error is not None:
                await self._on_turn_error(str(e))
        finally:
            self._turn_active = False

    async def speak_announcement(
        self, text: str, *, announcement_id: str | None = None,
        kind: str | None = None, message_id: str | None = None,
    ) -> bool:
        """Speak out-of-turn text — a finished background task's
        summary (E8-5.1b). Waits for a quiet moment (no reply
        playing, the user not mid-utterance), then speaks with the
        same word-by-word streaming and barge-in rules as a reply.
        Never commits to history: the task machinery already
        persisted the summary as the task's own message. Returns
        False when the session is not live — the summary still
        stands in chat either way.

        N10: ``announcement_id``/``kind`` tag every emitted chunk
        as this announcement (never as a talker reply), and
        ``message_id`` — the announced message's own id in the
        session store, when there is one — rides along, so the
        desktop can match the spoken text to the message already
        standing in chat instead of rendering it a second time."""
        text = (text or "").strip()
        if not text or not self._started or self._stopping:
            return False
        while not self._stopping and (
            self._output_active
            or self._endpointer.in_speech
            or self._barge_endpointer.in_speech
        ):
            await asyncio.sleep(0.005)
        if self._stopping or not self._started:
            return False
        self._announcing = True
        self._announcing_id = announcement_id
        self._interrupt_requested = False
        self._reset_heard()
        try:
            await self._deliver_reply(
                _static_deltas(text), commit=False,
                announcement_id=announcement_id, announcement_kind=kind,
                message_id=message_id)
        except Exception:  # a failed announcement must not kill the session
            logger.warning("voice announcement failed", exc_info=True)
        finally:
            self._announcing = False
            self._announcing_id = None
        return True

    async def _deliver_reply(
        self, deltas: AsyncIterator[str], *, commit: bool = True,
        announcement_id: str | None = None,
        announcement_kind: str | None = None,
        message_id: str | None = None,
    ) -> None:
        """Stream text deltas word by word and speak each completed
        sentence. On interruption the committed text is the heard
        text (word granularity); otherwise it is the full reply.
        ``commit=False`` (announcements) streams and speaks without
        touching history; the announcement identity, when given,
        tags every chunk this delivery emits."""
        parts: list[str] = []
        sentence_buffer = ""
        interrupted = False
        async for delta in deltas:
            if self._interrupt_requested or self._stopping:
                interrupted = True
                break
            if not delta:
                continue
            parts.append(delta)
            await self._emit_chunk(
                text_delta=delta, audio_chunk=None,
                announcement_id=announcement_id,
                announcement_kind=announcement_kind, message_id=message_id)
            sentence_buffer += delta
            sentences, sentence_buffer = _split_sentences(sentence_buffer)
            for sentence in sentences:
                await self._speak(
                    sentence, announcement_id=announcement_id,
                    announcement_kind=announcement_kind,
                    message_id=message_id)
                if self._interrupt_requested or self._stopping:
                    interrupted = True
                    break
            if interrupted:
                break
        if not interrupted and sentence_buffer.strip():
            await self._speak(
                sentence_buffer, announcement_id=announcement_id,
                announcement_kind=announcement_kind, message_id=message_id)
            if self._interrupt_requested or self._stopping:
                interrupted = True
        if not commit:
            return
        committed = self._heard_text_now() if interrupted else "".join(parts)
        if committed.strip() and self._on_assistant_final is not None:
            await self._on_assistant_final(committed.strip())

    async def _speak(
        self, sentence: str, *, announcement_id: str | None = None,
        announcement_kind: str | None = None,
        message_id: str | None = None,
    ) -> None:
        """Synthesize one sentence and push its audio chunks.

        The sentence's audio is fully synthesized BEFORE emission
        so the chunk total is known. Emission registers the
        sentence in the heard accounting together with its chunks'
        seq range (N09); how much of it counts as heard is decided
        at read time — played chunks when the renderer reports
        playback progress, emitted chunks when it never does — so
        an interrupt landing mid-sentence needs no fold-in here.
        A TTS failure degrades the turn to text-only, never fatal.
        A silent synthesis (zero chunks) registers nothing: no
        audio means nothing was heard, so the sentence can never
        be credited on the interrupted path (it still stands in
        the full commit of an uninterrupted reply, which
        ``_deliver_reply`` builds from the reply text itself).
        """
        chunks: list[bytes] = []
        try:
            async for audio in self._tts.synthesize(sentence, voice=self._tts_voice):
                if self._interrupt_requested or self._stopping:
                    return  # nothing of this sentence was emitted
                chunks.append(audio)
        except Exception:  # the mouth failing must not kill the conversation
            logger.warning("voice TTS failed; continuing text-only", exc_info=True)
            return
        if not chunks:
            return
        entry = {
            "sentence": sentence,
            "first_seq": self._next_chunk_seq,
            "total": len(chunks),
            "emitted": 0,
        }
        self._next_chunk_seq += len(chunks)
        self._heard_entries.append(entry)
        seq = entry["first_seq"]
        for audio in chunks:
            if self._interrupt_requested or self._stopping:
                return
            await self._emit_chunk(
                text_delta="",
                audio_chunk=base64.b64encode(audio).decode("ascii"),
                announcement_id=announcement_id,
                announcement_kind=announcement_kind, message_id=message_id,
                chunk_seq=seq,
            )
            entry["emitted"] += 1
            seq += 1

    async def _emit_chunk(
        self, *, text_delta: str, audio_chunk: str | None,
        announcement_id: str | None = None,
        announcement_kind: str | None = None,
        message_id: str | None = None,
        chunk_seq: int | None = None,
    ) -> None:
        payload: dict = {
            "session_id": self.session_id,
            "speaker": "assistant",
            "text_delta": text_delta,
            "audio_chunk": audio_chunk,
            "timestamp": time.time(),
        }
        if chunk_seq is not None:
            # N09: the audio chunk's session-monotonic identity.
            # The renderer reports finished seqs back as playback
            # progress — the input the heard accounting reads.
            payload["chunk_seq"] = chunk_seq
        if announcement_id is not None:
            # N10: this chunk is an announcement, not a reply turn.
            # The identity is stable per announced event, so the
            # desktop can route it away from the committable reply
            # stream; message_id, when present, is the announced
            # message's own id in the session store.
            payload["announcement_id"] = announcement_id
            if announcement_kind is not None:
                payload["announcement_kind"] = announcement_kind
            if message_id is not None:
                payload["message_id"] = message_id
        await self._emit(Event(
            type=EventType.VOICE_OUTPUT_CHUNK,
            payload=payload,
        ))


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def fast_chat_talker(provider, *, session_id: str | None) -> Talker:
    """Adapt ``run_fast_chat`` to the pipeline's talker contract:
    an async generator of reply text deltas for one user turn.

    History is loaded per turn from the session store, so voice
    turns and typed turns share one conversation.
    """

    async def talk(text: str) -> AsyncIterator[str]:
        from aether_engine.langgraph.fast_chat import (
            load_session_history,
            run_fast_chat,
        )

        history = load_session_history(session_id, text)
        async for event in run_fast_chat(
            text, provider, history, session_id=session_id):
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
