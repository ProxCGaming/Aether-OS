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

* Smart Turn endpointing (S4-M16) — the semantic half of that seam,
  now built. A ``TurnCompletionPolicy`` judges the latest partial
  transcript (fed in via ``Endpointer.observe_transcript()``) and
  the Endpointer makes the required trailing silence dynamic:
  COMPLETE turns release earlier than ``silence_duration_ms``
  (never below ``EARLY_RELEASE_FLOOR_MS`` of real pause), INCOMPLETE
  turns are held longer (never past ``MAX_HOLD_MS``), and with no
  transcript / no policy / sensitivity 0 the timing is exactly the
  legacy silence-only behaviour. The default policy is
  ``HeuristicTurnCompletionPolicy``: deterministic, local, no
  model, no cloud (grill Q27 local-first cascade).

  Model-availability evaluation (S4-M16, 2 Nov 2026): a real
  Smart Turn-class ONNX model is NOT honestly available via the
  installed voice extra. After ``uv sync --extra voice``,
  ``smart_turn`` and ``pipecat``/``pipecat_ai`` are not importable
  and no Smart Turn weights are bundled or declared in
  pyproject/uv.lock (onnxruntime/torch/transformers arrive only as
  transitive deps of the other stages). Serving the real model
  would need a new dependency plus a Hugging Face weights download
  in the runtime path, which the local-first decision forbids by
  default — so the heuristic ships behind the protocol seam, and a
  model-backed policy can replace it later without touching the
  Endpointer or the pipeline.

* ``SileroVad`` — the probability source. Silero VAD (ONNX,
  ~2 MB, <1 ms per 32 ms frame) is the cascade default from the
  voice research. The model runtime (onnxruntime + numpy) is an
  optional dependency: it is probed with ``find_spec`` and loaded
  lazily in ``open()``, never at import or construction time.
"""
import asyncio
import importlib.util
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from aether_engine.voice import VoiceUnavailableError

logger = logging.getLogger("aether_engine.voice.vad")

# Silero VAD's native frame: 512 samples at 16 kHz (32 ms).
SILERO_FRAME_SAMPLES = 512

# Smart Turn timing bounds (S4-M16). A COMPLETE verdict may shorten
# the required trailing silence, but never below this floor: a turn
# decision must still be grounded in some real pause. An INCOMPLETE
# verdict may extend the required silence, but never past this
# absolute cap: a misjudged fragment cannot hold a turn forever,
# and a voice turn held past ~1.2 s of silence stops feeling
# responsive regardless of what the transcript suggests.
EARLY_RELEASE_FLOOR_MS = 120
MAX_HOLD_MS = 1_200


def _clamp_sensitivity(value: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.5
    return min(1.0, max(0.0, value))


@dataclass
class EndpointerConfig:
    speech_threshold: float = 0.5
    silence_duration_ms: int = 700
    min_speech_ms: int = 240
    max_utterance_ms: int = 30_000
    # Smart Turn sensitivity (S4-M16, E8-5.2 pattern): how strongly
    # the turn-completion policy bends the trailing-silence window
    # [0.0, 1.0]. 0.0 disables Smart Turn entirely (pure
    # silence-only endpointing); higher values release COMPLETE
    # turns earlier (towards EARLY_RELEASE_FLOOR_MS) and hold
    # INCOMPLETE turns longer (towards MAX_HOLD_MS).
    smart_turn_sensitivity: float = 0.5

    def __post_init__(self) -> None:
        self.smart_turn_sensitivity = _clamp_sensitivity(
            self.smart_turn_sensitivity)


# ---------------------------------------------------------------------------
# Turn-completion policy (Smart Turn seam, S4-M16)
# ---------------------------------------------------------------------------


class TurnVerdict(Enum):
    """A policy's judgement of the current partial transcript."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TurnContext:
    """What a policy judges: the latest partial transcript plus the
    basic prosody context the Endpointer already tracks (how much
    speech the utterance holds so far, and how much trailing
    silence has accrued at judgement time)."""

    transcript: str
    speech_ms: int = 0
    silence_ms: int = 0


class TurnCompletionPolicy(Protocol):
    """Judges whether the speaker's turn is semantically complete.

    Implementations must be cheap and side-effect free: the
    Endpointer may call ``judge()`` on every trailing-silence frame.
    A model-backed Smart Turn policy satisfies this same protocol.
    """

    def judge(self, context: TurnContext) -> TurnVerdict:
        """COMPLETE / INCOMPLETE / UNKNOWN for ``context``."""
        ...


_WORD_RE = re.compile(r"[a-z']+")

# Words that cannot end a complete turn: the clause is syntactically
# still open (a conjunction/preposition/article/auxiliary expects
# more words after it).
_TRAILING_INCOMPLETE_WORDS = frozenset({
    # conjunctions + complementizers / subordinators
    "and", "but", "or", "nor", "yet", "so", "because", "although",
    "though", "since", "unless", "if", "when", "whenever", "while",
    "that", "which", "who", "whom", "whose",
    # prepositions
    "about", "above", "after", "against", "at", "before", "behind",
    "below", "beneath", "between", "by", "during", "for", "from",
    "in", "into", "of", "off", "on", "onto", "over", "through",
    "to", "towards", "under", "until", "upon", "via", "with",
    "within", "without",
    # articles / determiners that require a following noun
    "the", "a", "an", "my", "your", "his", "her", "its", "our",
    "their", "some", "any", "each", "every",
    # auxiliaries / modals that require a following verb
    "am", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "can", "could", "shall", "should", "may", "might", "must",
})

# Filler words / discourse phrases that, at the end of a partial,
# signal the speaker is holding the floor rather than yielding it.
_FILLER_WORDS = frozenset({"um", "uh", "erm", "hmm", "mm", "mhm", "like"})
_FILLER_ENDINGS = ("you know", "i mean", "you know what")

# Multi-word incomplete tails (checked on the punctuation-stripped,
# lowercased text) — explicit trailing-off shapes like the canonical
# "I was thinking about" mid-thought pause.
_INCOMPLETE_ENDINGS = (
    "i was thinking about", "i am thinking about",
    "i was going to", "i am going to", "i'm going to",
    "let me think about", "depends on",
)

# Short answers that are complete turns on their own.
_COMPLETE_SHORT_WORDS = frozenset({
    "yes", "no", "okay", "ok", "sure", "thanks", "hello", "hi",
    "stop", "go", "done", "right", "correct", "fine", "please",
})
_COMPLETE_SHORT_PHRASES = frozenset({"thank you", "yes please", "no thanks"})

_TRAILING_PRONOUNS = frozenset({"i", "you", "he", "she", "it", "we", "they", "me", "him", "them", "us"})


class HeuristicTurnCompletionPolicy:
    """Deterministic local Smart Turn heuristic (S4-M16).

    No model download, no cloud, no I/O — text shape only:

    * trailing-off markers ("…", "...", "—") -> INCOMPLETE;
    * an incomplete tail phrase, or a final word that cannot close
      a clause (conjunction / preposition / article / auxiliary /
      complementizer…) -> INCOMPLETE;
    * filler endings ("um", "you know", "i mean") -> INCOMPLETE;
    * terminal punctuation (. ! ?) on a closable clause -> COMPLETE;
    * known short answers ("yes", "thank you") -> COMPLETE;
    * very short fragments (<= 2 words, no other signal) ->
      INCOMPLETE — a fragment is not yet a turn;
    * anything longer ending on a content word -> COMPLETE;
    * otherwise UNKNOWN (the Endpointer falls back to silence-only).
    """

    def judge(self, context: TurnContext) -> TurnVerdict:
        text = (context.transcript or "").strip()
        if not text:
            return TurnVerdict.UNKNOWN
        # Trailing-off markers: the speaker visibly did not finish.
        if text.endswith(("…", "...", "—", "--")) or "…" in text[-2:]:
            return TurnVerdict.INCOMPLETE
        words = _WORD_RE.findall(text.lower())
        if not words:
            return TurnVerdict.UNKNOWN
        plain = " ".join(words)
        if any(plain.endswith(ending) for ending in _INCOMPLETE_ENDINGS):
            return TurnVerdict.INCOMPLETE
        last = words[-1]
        # A final word that syntactically requires a continuation.
        # "that" is a complementizer ("she said that …") unless a
        # preposition precedes it, where it is that preposition's
        # object ("help me with that") and the clause can close.
        if last in _TRAILING_INCOMPLETE_WORDS and not (
                last == "that" and len(words) >= 2
                and words[-2] in {"about", "at", "by", "for", "from",
                                  "in", "into", "of", "on", "to",
                                  "with"}):
            return TurnVerdict.INCOMPLETE
        if last in _FILLER_WORDS or any(
                plain.endswith(ending) for ending in _FILLER_ENDINGS):
            return TurnVerdict.INCOMPLETE
        # Terminal punctuation on a closable clause is the strongest
        # completeness signal STT partials carry.
        tail = text.rstrip("\"'”’)]} ")
        if tail.endswith((".", "!", "?")):
            return TurnVerdict.COMPLETE
        if plain in _COMPLETE_SHORT_PHRASES or (
                len(words) == 1 and last in _COMPLETE_SHORT_WORDS):
            return TurnVerdict.COMPLETE
        # A trailing pronoun with no punctuation ("and then I …")
        # leaves the clause's verb still to come.
        if last in _TRAILING_PRONOUNS:
            return TurnVerdict.INCOMPLETE
        # Very short fragments are not yet turns.
        if len(words) <= 2:
            return TurnVerdict.INCOMPLETE
        # A longer partial ending on a content word reads as a
        # complete clause/imperative ("turn off the lights").
        return TurnVerdict.COMPLETE


class Endpointer:
    """Probability stream -> utterance boundary events.

    ``feed()`` returns "speech_started", "utterance_end",
    "utterance_discarded" (speech shorter than ``min_speech_ms`` —
    a blip, not a turn), or None.

    Smart Turn (S4-M16): with a turn-completion policy attached,
    ``observe_transcript()`` feeds the latest partial transcript in
    and the trailing-silence requirement becomes dynamic (see
    ``_required_silence_ms()``). Without an observed transcript,
    without a policy, or at sensitivity 0, the requirement is
    exactly ``silence_duration_ms`` — the legacy behaviour, frame
    for frame. The ``max_utterance_ms`` cap (checked on speech
    frames) and the ``min_speech_ms`` discard are untouched by the
    policy either way.
    """

    def __init__(
        self,
        config: EndpointerConfig | None = None,
        policy: TurnCompletionPolicy | None = None,
    ):
        self.config = config or EndpointerConfig()
        if policy is not None:
            self._policy: TurnCompletionPolicy | None = policy
        elif self.config.smart_turn_sensitivity > 0:
            self._policy = HeuristicTurnCompletionPolicy()
        else:
            self._policy = None
        self._observed_transcript = ""
        self.reset()

    def reset(self) -> None:
        self._in_speech = False
        self._speech_ms = 0
        self._silence_ms = 0
        self._observed_transcript = ""

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def policy(self) -> TurnCompletionPolicy | None:
        """The attached turn-completion policy, or None."""
        return self._policy

    def observe_transcript(self, text: str) -> None:
        """Record the latest partial transcript for this utterance.

        Called by the pipeline whenever a new partial lands. An
        empty text records "no transcript", which keeps endpointing
        silence-only. The transcript is cleared on reset(), so a
        verdict never leaks into the next utterance.
        """
        self._observed_transcript = (text or "").strip()

    def _required_silence_ms(self) -> int:
        """Trailing silence required to end the current utterance.

        Legacy value unless a policy has a transcript to judge and
        sensitivity is on: COMPLETE shrinks the window towards
        EARLY_RELEASE_FLOOR_MS, INCOMPLETE stretches it towards
        MAX_HOLD_MS, both scaled by smart_turn_sensitivity (so at
        the default 0.5 the window moves halfway). UNKNOWN, a
        policy error, or no transcript all fall back to the legacy
        value — a policy must never strand or rush a turn by
        failing.
        """
        base = self.config.silence_duration_ms
        sensitivity = self.config.smart_turn_sensitivity
        if sensitivity <= 0 or self._policy is None \
                or not self._observed_transcript:
            return base
        try:
            verdict = self._policy.judge(TurnContext(
                transcript=self._observed_transcript,
                speech_ms=self._speech_ms,
                silence_ms=self._silence_ms,
            ))
        except Exception:  # a broken policy degrades to silence-only
            logger.debug("turn-completion policy failed", exc_info=True)
            return base
        if verdict is TurnVerdict.COMPLETE and base > EARLY_RELEASE_FLOOR_MS:
            return round(base - sensitivity * (base - EARLY_RELEASE_FLOOR_MS))
        if verdict is TurnVerdict.INCOMPLETE and MAX_HOLD_MS > base:
            return round(base + sensitivity * (MAX_HOLD_MS - base))
        return base

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
        if self._silence_ms >= self._required_silence_ms():
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
        self._context = None

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

            loaded = load_silero_vad(onnx=True)
            # silero-vad 6.x returns an OnnxWrapper around the
            # ONNX session, not the session itself. This engine
            # drives the session (and its recurrent state and
            # context, see _infer) directly, so unwrap it; older
            # return shapes pass through unchanged.
            session = getattr(loaded, "session", loaded)
        self._np = np
        # Silero's recurrent state: [2, 1, 128], zeroed per session.
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = None
        return session

    async def close(self) -> None:
        self._session = None
        self._state = None
        self._context = None

    async def probability(self, frame: bytes, sample_rate: int) -> float:
        if self._session is None:
            raise VoiceUnavailableError("Silero VAD used before open()")
        return await asyncio.to_thread(self._infer, frame, sample_rate)

    def _infer(self, frame: bytes, sample_rate: int) -> float:
        np = self._np
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        if len(samples) < SILERO_FRAME_SAMPLES:
            samples = np.pad(samples, (0, SILERO_FRAME_SAMPLES - len(samples)))
        samples = samples[:SILERO_FRAME_SAMPLES]
        # The bundled Silero model (v5, via silero-vad 6.x) is a
        # streaming model: each inference must be prefixed with a
        # context window — the last 64 samples of the previous
        # input at 16 kHz (32 at 8 kHz) — exactly as silero-vad's
        # own OnnxWrapper feeds it. Feeding bare 512-sample frames
        # runs without error but returns ~0 speech probability for
        # real speech, so the context is part of the model's
        # input contract, not an optimisation.
        context_size = 64 if sample_rate == 16000 else 32
        if self._context is None or len(self._context) != context_size:
            self._context = np.zeros(context_size, dtype=np.float32)
        audio = np.concatenate([self._context, samples]).reshape(1, -1)
        out, self._state = self._session.run(
            None,
            {
                "input": audio,
                "state": self._state,
                "sr": np.array(sample_rate, dtype=np.int64),
            },
        )
        self._context = audio[0, -context_size:]
        return float(out[0][0])
