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
import re
import struct
import threading
import wave
from collections.abc import AsyncIterator
from typing import Protocol

from aether_engine.voice import VoiceUnavailableError

logger = logging.getLogger("aether_engine.voice.tts")

DEFAULT_VOICE = "af_heart"
KOKORO_SAMPLE_RATE = 24000

# The synthesis producer runs on a worker thread (Kokoro is
# blocking) and hands chunks over through this queue (E7-13).
# The queue is bounded: on a barge-in the consumer is gone, and
# an unbounded queue let the orphaned thread synthesise the
# whole interrupted sentence into memory nobody drained. Eight
# chunks is well ahead of playback and far short of a hoard.
_TTS_QUEUE_MAXSIZE = 8

# How long the producer thread waits on a full queue before
# re-checking whether its consumer has gone.
_TTS_PUT_POLL_S = 0.05

# Kokoro voice ids are ``<family>_<name>``: a two-letter
# language/gender prefix (a=American, b=British, e=Spanish,
# f=French, h=Hindi, i=Italian, j=Japanese, p=Portuguese,
# z=Mandarin; f=female, m=male) and a lowercase name. The pattern
# is the vocabulary the speech config field validates against
# (model hub, H3): a value outside it names no voice Kokoro can
# load, so it is refused at write time instead of failing at
# synthesis time.
KOKORO_VOICE_ID_RE = re.compile(
    r"^(?:af|am|bf|bm|ef|em|ff|fm|hf|hm|if|im|jf|jm|pf|pm|zf|zm)"
    r"_[a-z]+$")


def is_valid_kokoro_voice(value: object) -> bool:
    """Whether *value* has the shape of a Kokoro voice id."""
    return (
        isinstance(value, str)
        and KOKORO_VOICE_ID_RE.match(value.strip()) is not None)


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
        queue: asyncio.Queue = asyncio.Queue(maxsize=_TTS_QUEUE_MAXSIZE)
        # E7-13: cancelling the to_thread future cannot stop a
        # running thread, so the producer's lifetime is governed
        # by this flag instead: the generator's finally sets it,
        # and a producer blocked on the bounded queue (or between
        # chunks) notices and stops synthesising.
        stop = threading.Event()

        def _deliver(item) -> bool:
            """Queue one item from the producer thread. Blocks
            while the bounded queue is full; returns False —
            dropping the item — once the consumer has gone or
            the loop has closed, so delivery never raises into
            the thread (the old call_soon_threadsafe shape could
            raise RuntimeError on a closed loop)."""
            if stop.is_set():
                return False
            try:
                put = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
            except RuntimeError:  # the loop is already closed
                return False
            while not stop.is_set():
                try:
                    put.result(timeout=_TTS_PUT_POLL_S)
                    return True
                except TimeoutError:
                    continue
            put.cancel()
            return False

        def _produce() -> None:
            try:
                for _gs, _ps, audio in self._pipeline(text, voice=voice):
                    if not _deliver(float_pcm_to_wav(audio)):
                        return
            except Exception as e:  # noqa: BLE001 — surfaced to the consumer below, not swallowed
                _deliver(e)
            finally:
                _deliver(None)

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
            stop.set()
            producer.cancel()
