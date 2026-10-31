"""Voice cascade coordinator (Epic 8, E8-5.1a).

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

The loop is half-duplex in this slice: frames arriving while a
reply is being spoken are not consumed (the frame source simply
isn't pulled), because interruption *detection* is E8-5.1b. The
barge-in support the pipeline itself owes lives in
``interrupt_output()``: it stops the in-flight reply cooperatively
and reports the committed text — only the words actually spoken —
which is what history keeps (``on_assistant_final`` receives
exactly that), plus the VOICE_BARGE_IN push payload.

The transport is the SmallWebRTC seam: audio frames come from a
``VoiceTransport``, never from a device the engine opens itself.
``WebRtcVoiceTransport`` is the production transport; in this
build the engine has no RTC negotiation endpoint (and aiortc is
an optional dependency), so it reports itself unavailable and
session start fails closed honestly until that lands.
"""
import asyncio
import base64
import importlib.util
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from aether_common.contracts import Event, EventType
from aether_engine.voice import VoiceTalkerError, VoiceUnavailableError
from aether_engine.voice.stt import FasterWhisperStt, SttEngine
from aether_engine.voice.tts import DEFAULT_VOICE, KokoroTts, TtsEngine
from aether_engine.voice.vad import Endpointer, SileroVad, VadEngine

logger = logging.getLogger("aether_engine.voice.pipeline")

Talker = Callable[[str], AsyncIterator[str]]
EmitFn = Callable[[Event], Awaitable[None]]
TurnCallback = Callable[[str], Awaitable[None]]

_SENTENCE_BOUNDARY = re.compile(r"[.!?…]+[\"'”’)\]]*\s+")


def _split_sentences(buffer: str) -> tuple[list[str], str]:
    """Split off sentences whose boundary is proven (terminator +
    following whitespace); the tail stays buffered for more deltas."""
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_BOUNDARY.finditer(buffer):
        sentences.append(buffer[start:match.end()])
        start = match.end()
    return sentences, buffer[start:]


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


class WebRtcVoiceTransport:
    """SmallWebRTC peer transport between Electron and the engine.

    Gated in this build: aiortc is an optional dependency and the
    engine exposes no RTC negotiation endpoint yet, so the
    transport honestly reports unavailable and voice sessions fail
    closed at start instead of pretending to listen.
    """

    def unavailable_reason(self) -> str | None:
        if importlib.util.find_spec("aiortc") is None:
            return "aiortc is not installed (SmallWebRTC transport)"
        return "SmallWebRTC negotiation endpoint is not available in this build"

    async def open(self) -> dict | None:
        reason = self.unavailable_reason()
        raise VoiceUnavailableError(reason or "SmallWebRTC transport unavailable")

    async def frames(self) -> AsyncIterator[bytes]:
        reason = self.unavailable_reason()
        raise VoiceUnavailableError(reason or "SmallWebRTC transport unavailable")
        yield b""  # pragma: no cover — makes this an async generator

    async def close(self) -> None:
        return None


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
        tts_voice: str = DEFAULT_VOICE,
        sample_rate: int = 16000,
        partial_min_audio_ms: int = 250,
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
        self._endpointer = endpointer or Endpointer()
        self._tts_voice = tts_voice
        self._sample_rate = sample_rate
        self._partial_min_audio_ms = partial_min_audio_ms

        self._started = False
        self._stopping = False
        self._webrtc_config: dict | None = None
        self._utterance = bytearray()
        self._last_partial_text = ""
        self._since_partial_ms = 0
        self._pending_partial_done = False
        self._turn_active = False
        self._interrupt_requested = False
        self._spoken_text = ""

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

    # -- barge-in seam (E8-5.1b builds detection on this) ---------------

    async def interrupt_output(self) -> dict:
        """Stop the in-flight reply; report what was actually heard.

        Returns the VOICE_BARGE_IN payload fields:
        ``{"stopped_at_char", "committed_text"}``. The committed
        text — only the words whose audio was fully emitted — is
        also what ``on_assistant_final`` commits for the turn.
        """
        if self._turn_active:
            self._interrupt_requested = True
            for _ in range(2000):
                if not self._turn_active:
                    break
                await asyncio.sleep(0.002)
            await self._emit(Event(
                type=EventType.VOICE_BARGE_IN,
                payload={
                    "session_id": self.session_id,
                    "stopped_at_char": len(self._spoken_text),
                    "committed_text": self._spoken_text,
                },
            ))
        return {
            "stopped_at_char": len(self._spoken_text),
            "committed_text": self._spoken_text,
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
            self._turn_active = False

    async def _consume_frame(self, data: bytes) -> None:
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
        await self._run_turn(text)

    async def _run_turn(self, text: str) -> None:
        self._turn_active = True
        self._interrupt_requested = False
        self._spoken_text = ""
        parts: list[str] = []
        sentence_buffer = ""
        interrupted = False
        try:
            async for delta in self._talker(text):
                if self._interrupt_requested or self._stopping:
                    interrupted = True
                    break
                if not delta:
                    continue
                parts.append(delta)
                await self._emit_chunk(text_delta=delta, audio_chunk=None)
                sentence_buffer += delta
                sentences, sentence_buffer = _split_sentences(sentence_buffer)
                for sentence in sentences:
                    await self._speak(sentence)
                    if self._interrupt_requested or self._stopping:
                        interrupted = True
                        break
                if interrupted:
                    break
            if not interrupted and sentence_buffer.strip():
                await self._speak(sentence_buffer)
                if self._interrupt_requested or self._stopping:
                    interrupted = True
        except Exception as e:  # noqa: BLE001 — the talker failing must not kill the session
            logger.warning(f"voice turn failed: {e}")
            if self._on_turn_error is not None:
                await self._on_turn_error(str(e))
            self._turn_active = False
            return
        committed = self._spoken_text if interrupted else "".join(parts)
        if committed.strip() and self._on_assistant_final is not None:
            await self._on_assistant_final(committed.strip())
        self._turn_active = False

    async def _speak(self, sentence: str) -> None:
        """Synthesize one sentence and push its audio chunks.

        The sentence joins the spoken text only once its audio is
        fully emitted — that is the barge-in committed-text rule.
        A TTS failure degrades the turn to text-only, never fatal.
        """
        try:
            async for audio in self._tts.synthesize(sentence, voice=self._tts_voice):
                if self._interrupt_requested or self._stopping:
                    return
                await self._emit_chunk(
                    text_delta="",
                    audio_chunk=base64.b64encode(audio).decode("ascii"),
                )
        except Exception:  # the mouth failing must not kill the conversation
            logger.warning("voice TTS failed; continuing text-only", exc_info=True)
            return
        self._spoken_text += sentence

    async def _emit_chunk(self, *, text_delta: str, audio_chunk: str | None) -> None:
        await self._emit(Event(
            type=EventType.VOICE_OUTPUT_CHUNK,
            payload={
                "session_id": self.session_id,
                "speaker": "assistant",
                "text_delta": text_delta,
                "audio_chunk": audio_chunk,
                "timestamp": time.time(),
            },
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
) -> tuple[VoicePipeline | None, str | None]:
    """Build a pipeline from the production stages (or injected
    ones). Returns ``(pipeline, None)`` when every stage can run,
    or ``(None, "voice unavailable: ...")`` — nothing is opened
    either way; opening happens in ``pipeline.start()``.
    """
    pipeline = VoicePipeline(
        session_id=session_id,
        vad=vad if vad is not None else SileroVad(),
        stt=stt if stt is not None else FasterWhisperStt(),
        tts=tts if tts is not None else KokoroTts(),
        transport=transport if transport is not None else WebRtcVoiceTransport(),
        talker=talker,
        emit=emit,
        on_user_final=on_user_final,
        on_assistant_final=on_assistant_final,
        on_turn_error=on_turn_error,
    )
    reasons = pipeline.unavailable_reasons()
    if reasons:
        return None, _unavailable_message(reasons)
    return pipeline, None
