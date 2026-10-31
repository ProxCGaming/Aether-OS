"""Local text-to-speech (Epic 8, E8-5.1a).

Kokoro-82M is the local mouth from the voice research: Apache-2.0,
faster than realtime on CPU, and the settled default voice is
``af_heart`` (grill Q29). Synthesis is sentence-streamed by the
pipeline — each sentence goes to the engine as the talker's text
arrives, so first audio does not wait for the full reply.

The kokoro package is an optional dependency: probed with
``find_spec``, loaded lazily in ``open()`` — never at import or
construction time. Output chunks are self-contained WAV bytes so
the desktop player can decode each chunk independently.
"""
import asyncio
import importlib.util
import io
import logging
import struct
import wave
from collections.abc import AsyncIterator
from typing import Protocol

from aether_engine.voice import VoiceUnavailableError

logger = logging.getLogger("aether_engine.voice.tts")

DEFAULT_VOICE = "af_heart"
KOKORO_SAMPLE_RATE = 24000


class TtsEngine(Protocol):
    """Sentence-at-a-time speech synthesis."""

    def unavailable_reason(self) -> str | None:
        """Why this engine cannot run here, or None when it can."""
        ...

    async def open(self) -> None:
        """Load the model. Called only when a voice session starts."""
        ...

    async def close(self) -> None:
        """Release the model. Idempotent."""
        ...

    def synthesize(self, text: str, *, voice: str) -> AsyncIterator[bytes]:
        """Yield WAV audio chunks for *text* spoken in *voice*."""
        ...


def float_pcm_to_wav(samples, sample_rate: int = KOKORO_SAMPLE_RATE) -> bytes:
    """Encode float samples in [-1, 1] as a PCM16 WAV chunk.

    Duck-typed on purpose: Kokoro yields numpy arrays, but this
    conversion only needs iteration, so numpy is never imported
    here.
    """
    frames = bytearray()
    for value in samples:
        clamped = max(-1.0, min(1.0, float(value)))
        frames += struct.pack("<h", int(clamped * 32767))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(bytes(frames))
    return buffer.getvalue()


class KokoroTts:
    """Kokoro TTS runner; fails closed when unavailable."""

    def __init__(self, lang_code: str = "a"):
        self._lang_code = lang_code
        self._pipeline = None

    def unavailable_reason(self) -> str | None:
        if importlib.util.find_spec("kokoro") is None:
            return "kokoro is not installed (local TTS)"
        return None

    async def open(self) -> None:
        reason = self.unavailable_reason()
        if reason is not None:
            raise VoiceUnavailableError(reason)
        self._pipeline = await asyncio.to_thread(self._load_pipeline)

    def _load_pipeline(self):
        from kokoro import KPipeline

        return KPipeline(lang_code=self._lang_code)

    async def close(self) -> None:
        self._pipeline = None

    async def synthesize(self, text: str, *, voice: str = DEFAULT_VOICE) -> AsyncIterator[bytes]:
        if self._pipeline is None:
            raise VoiceUnavailableError("Kokoro TTS used before open()")
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _produce() -> None:
            try:
                for _gs, _ps, audio in self._pipeline(text, voice=voice):
                    chunk = float_pcm_to_wav(audio)
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except Exception as e:  # noqa: BLE001 — surfaced to the consumer below, not swallowed
                loop.call_soon_threadsafe(queue.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        producer = asyncio.ensure_future(asyncio.to_thread(_produce))
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            producer.cancel()
