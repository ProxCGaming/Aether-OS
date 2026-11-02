"""Voice activity detection + endpointing (Epic 8, E8-5.1a).

Two separate concerns live here:

* ``Endpointer`` — the pure turn-taking state machine. It consumes
  per-frame speech probabilities and decides when an utterance
  starts and when it has ended (trailing silence past
  ``silence_duration_ms``, with a minimum-speech guard so coughs
  and clicks are discarded, and a maximum-utterance cap). It has no
  model and no I/O, so endpointing decisions are fully testable
  from scripted probability inputs. A Smart Turn-class semantic
  detector (E8-5.2 tuning) plugs in by replacing the probability
  source's endpointing policy here — the pipeline only depends on
  this class's ``feed()`` contract.

* ``SileroVad`` — the probability source. Silero VAD (ONNX,
  ~2 MB, <1 ms per 32 ms frame) is the cascade default from the
  voice research. The model runtime (onnxruntime + numpy) is an
  optional dependency: it is probed with ``find_spec`` and loaded
  lazily in ``open()``, never at import or construction time.
"""
import asyncio
import importlib.util
import logging
from dataclasses import dataclass
from typing import Protocol

from aether_engine.voice import VoiceUnavailableError

logger = logging.getLogger("aether_engine.voice.vad")

# Silero VAD's native frame: 512 samples at 16 kHz (32 ms).
SILERO_FRAME_SAMPLES = 512


@dataclass
class EndpointerConfig:
    speech_threshold: float = 0.5
    silence_duration_ms: int = 700
    min_speech_ms: int = 240
    max_utterance_ms: int = 30_000


class Endpointer:
    """Probability stream -> utterance boundary events.

    ``feed()`` returns "speech_started", "utterance_end",
    "utterance_discarded" (speech shorter than ``min_speech_ms`` —
    a blip, not a turn), or None.
    """

    def __init__(self, config: EndpointerConfig | None = None):
        self.config = config or EndpointerConfig()
        self.reset()

    def reset(self) -> None:
        self._in_speech = False
        self._speech_ms = 0
        self._silence_ms = 0

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def feed(self, probability: float, frame_ms: int) -> str | None:
        cfg = self.config
        if probability >= cfg.speech_threshold:
            self._silence_ms = 0
            if not self._in_speech:
                self._in_speech = True
                self._speech_ms = frame_ms
                return "speech_started"
            self._speech_ms += frame_ms
            if self._speech_ms >= cfg.max_utterance_ms:
                return self._finish()
            return None
        if not self._in_speech:
            return None
        self._silence_ms += frame_ms
        if self._silence_ms >= cfg.silence_duration_ms:
            return self._finish()
        return None

    def _finish(self) -> str:
        speech_ms = self._speech_ms
        self.reset()
        if speech_ms >= self.config.min_speech_ms:
            return "utterance_end"
        return "utterance_discarded"


class VadEngine(Protocol):
    """Per-frame speech probability source."""

    def unavailable_reason(self) -> str | None:
        """Why this engine cannot run here, or None when it can."""
        ...

    async def open(self) -> None:
        """Load the model. Called only when a voice session starts."""
        ...

    async def close(self) -> None:
        """Release the model. Idempotent."""
        ...

    async def probability(self, frame: bytes, sample_rate: int) -> float:
        """Speech probability in [0, 1] for one PCM16 frame."""
        ...


class SileroVad:
    """Silero VAD over onnxruntime; fails closed when unavailable."""

    def __init__(self, model_path: str | None = None):
        self._model_path = model_path
        self._session = None
        self._state = None

    def unavailable_reason(self) -> str | None:
        if importlib.util.find_spec("onnxruntime") is None:
            return "onnxruntime is not installed (Silero VAD runtime)"
        if importlib.util.find_spec("numpy") is None:
            return "numpy is not installed (Silero VAD runtime)"
        if self._model_path is None:
            try:
                from silero_vad import load_silero_vad  # noqa: F401
            except ImportError:
                return "silero-vad is not installed (Silero VAD model)"
        return None

    async def open(self) -> None:
        reason = self.unavailable_reason()
        if reason is not None:
            raise VoiceUnavailableError(reason)
        self._session = await asyncio.to_thread(self._load_model)

    def _load_model(self):
        import numpy as np
        import onnxruntime as ort

        if self._model_path is not None:
            session = ort.InferenceSession(
                self._model_path, providers=["CPUExecutionProvider"])
        else:
            from silero_vad import load_silero_vad

            session = load_silero_vad(onnx=True)
        self._np = np
        # Silero's recurrent state: [2, 1, 128], zeroed per session.
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        return session

    async def close(self) -> None:
        self._session = None
        self._state = None

    async def probability(self, frame: bytes, sample_rate: int) -> float:
        if self._session is None:
            raise VoiceUnavailableError("Silero VAD used before open()")
        return await asyncio.to_thread(self._infer, frame, sample_rate)

    def _infer(self, frame: bytes, sample_rate: int) -> float:
        np = self._np
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        if len(samples) < SILERO_FRAME_SAMPLES:
            samples = np.pad(samples, (0, SILERO_FRAME_SAMPLES - len(samples)))
        audio = samples[:SILERO_FRAME_SAMPLES].reshape(1, -1)
        out, self._state = self._session.run(
            None,
            {
                "input": audio,
                "state": self._state,
                "sr": np.array(sample_rate, dtype=np.int64),
            },
        )
        return float(out[0][0])
