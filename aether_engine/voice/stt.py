"""Local speech-to-text (Epic 8, E8-5.1a).

The STT stage transcribes one accumulated utterance buffer at a
time. Streaming partials are produced the way faster-whisper is
used in production cascades: the pipeline re-transcribes the
growing utterance buffer on a cadence and emits each improved
text as a provisional partial; the endpointer's final call is the
committed transcript. (Moonshine, the other researched local
option, is a native-streaming model; it can replace this runner
behind the same ``SttEngine`` protocol without the pipeline
changing.)

faster-whisper is an optional dependency: probed with
``find_spec``, loaded lazily in ``open()`` — never at import or
construction time. The same runner later feeds E8-4.4's video
transcript stage.
"""
import asyncio
import importlib.util
import io
import logging
import wave
from typing import Protocol

from aether_engine.voice import VoiceUnavailableError

logger = logging.getLogger("aether_engine.voice.stt")


class SttEngine(Protocol):
    """Utterance-buffer transcription."""

    def unavailable_reason(self) -> str | None:
        """Why this engine cannot run here, or None when it can."""
        ...

    async def open(self) -> None:
        """Load the model. Called only when a voice session starts."""
        ...

    async def close(self) -> None:
        """Release the model. Idempotent."""
        ...

    async def transcribe(self, audio: bytes, *, sample_rate: int) -> str:
        """Transcribe raw PCM16 mono audio to text ("" when silent)."""
        ...


def pcm16_to_wav(audio: bytes, sample_rate: int) -> bytes:
    """Wrap raw PCM16 mono samples in a WAV container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(audio)
    return buffer.getvalue()


class FasterWhisperStt:
    """faster-whisper runner; fails closed when unavailable."""

    def __init__(self, model: str = "small", language: str | None = None):
        self._model_name = model
        self._language = language
        self._model = None

    def unavailable_reason(self) -> str | None:
        if importlib.util.find_spec("faster_whisper") is None:
            return "faster-whisper is not installed (local STT)"
        return None

    async def open(self) -> None:
        reason = self.unavailable_reason()
        if reason is not None:
            raise VoiceUnavailableError(reason)
        self._model = await asyncio.to_thread(self._load_model)

    def _load_model(self):
        from faster_whisper import WhisperModel

        return WhisperModel(self._model_name, device="auto", compute_type="auto")

    async def close(self) -> None:
        self._model = None

    async def transcribe(self, audio: bytes, *, sample_rate: int) -> str:
        if self._model is None:
            raise VoiceUnavailableError("faster-whisper STT used before open()")
        return await asyncio.to_thread(self._transcribe_sync, audio, sample_rate)

    def _transcribe_sync(self, audio: bytes, sample_rate: int) -> str:
        wav = io.BytesIO(pcm16_to_wav(audio, sample_rate))
        segments, _info = self._model.transcribe(
            wav, language=self._language, beam_size=1, vad_filter=False)
        return "".join(segment.text for segment in segments).strip()
