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
import re
import wave
from typing import Protocol

from aether_engine.voice import VoiceUnavailableError

logger = logging.getLogger("aether_engine.voice.stt")

# The bare model names faster-whisper itself accepts: each maps to
# the ``Systran/faster-whisper-<name>`` Hugging Face repo in
# ``faster_whisper.download_model``. This is the closed vocabulary
# the speech config fields validate against (model hub, H3) —
# faster-whisper treats ANY other string as a repo id or path, so
# without this check a settings typo becomes a surprise multi-GB
# download at first voice use.
WHISPER_MODEL_SIZES = frozenset({
    "tiny", "tiny.en",
    "base", "base.en",
    "small", "small.en",
    "medium", "medium.en",
    "large-v1", "large-v2", "large-v3", "large",
    "large-v3-turbo", "turbo",
    "distil-small.en", "distil-medium.en",
    "distil-large-v2", "distil-large-v3", "distil-large-v3.5",
})

# An HF repo id is exactly ``owner/name`` — no whitespace, no
# extra path segments.
_HF_REPO_ID_RE = re.compile(r"^[^/\s]+/[^/\s]+$")


def is_supported_stt_model(value: object) -> bool:
    """Whether *value* may be stored as a speech STT model (H3).

    Two legal shapes, checked offline:

    - a bare faster-whisper size name (``WHISPER_MODEL_SIZES``);
    - an HF repo id whose leaf marks it as a faster-whisper build
      — decided by the model hub's runnability labeller (H1) from
      the id alone, so this predicate and the hub can never
      disagree about what runs in-app.

    Anything else (unknown bare names, non-faster-whisper repos,
    non-strings) is unsupported.
    """
    if not isinstance(value, str):
        return False
    name = value.strip()
    if not name:
        return False
    if "/" not in name:
        return name in WHISPER_MODEL_SIZES
    if not _HF_REPO_ID_RE.match(name):
        return False
    from aether_engine.models.model_hub import label_runnability

    label, _reason = label_runnability("speech-to-text", [], name)
    return label == "in-app"


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
