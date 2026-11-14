"""Voice cascading degradation + latency step-down (master build S15).

Until this slice the voice cascade was binary: every stage
reported ``unavailable_reason()`` and ``VoicePipeline.start()``
refused the WHOLE session when any one of them could not run —
no reduced mode, no smaller-model retry — and a stage that ran
slowly just made every turn slow (audit R14 / voice G4+G5). The
one measured data point in the project: CPU-only whisper
``small`` transcription took ~5,556 ms per turn on the build VM
against the bench's 200 ms stage budget
(``benchmarks/voice_latency_bench.py``), with the session none
the wiser.

This module is the ladder that replaces the binary refusal. It
has three parts:

* :func:`plan_voice_session` — a PURE function (no I/O, no
  clock) deciding the session's starting profile from the stage
  availability reasons, the S10 capability matrix's
  ``local_voice`` verdict, and the per-model STT latency this
  machine has actually measured (the store below). The rungs:

    full        — Silero VAD + configured STT model + TTS.
    degraded    — the session runs with a named reduction: the
                  energy-based VAD fallback (the Silero runtime
                  is missing but frames still need endpointing),
                  or a measured-slow STT model the session will
                  step down from if it stays slow.
    text_only   — TTS cannot run (or fails mid-session): the
                  user still talks, replies arrive as text.
    unavailable — a stage with no rung is missing (transport,
                  STT), or the S10 matrix judges local voice
                  unavailable on this machine. The refusal names
                  every missing stage, exactly as the binary
                  contract did.

  There is deliberately NO cloud-speech rung: no cloud STT/TTS
  engine or config exists anywhere in the codebase (audit §4),
  and a ladder rung may not invent a permission the user never
  granted. If a cloud speech engine lands, it joins the ladder
  here, behind its own config.

* :class:`VoiceLatencyStore` — per-stage measured durations,
  persisted in the data home (``~/.aether/voice_latency.db``),
  house style mirroring ``routing/latency.py``: bounded SQLite,
  percentiles over the newest samples, recording that never
  raises. This is the per-machine measurement the S10 matrix's
  voice verdict said S15 would supply ("S15 replaces the voice
  verdict's basis with per-machine measured latency"): the
  matrix file itself stays S10-owned and read-only here; the
  two are read together at planning time.

* :class:`SessionDegradation` — one session's runtime tracker:
  it owns the current profile, the rolling STT latency window,
  the consecutive-failure count and the one-shot latches, and
  turns observations (a final transcription's duration, a stage
  failure) into decisions the delivery half executes (swap the
  STT engine, go text-only, end the session). Decisions are
  data, never side effects, so the logic is testable without a
  pipeline.

Step-down hysteresis: a session only ever steps DOWN (smaller
STT model, TTS off), never back up — no oscillation. The next
session re-plans from the stored measurements, which is the
re-probe.

Threshold honesty: the constants below are HEURISTICS derived
from the single VM measurement plus the bench budget, named
and commented as such; Windows-hardware figures still await
(the same standing caveat as the S10 matrix thresholds).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("aether_engine.voice.degradation")

# ---------------------------------------------------------------------------
# Step-down thresholds (HEURISTICS — see the module docstring).
#
# The bench's STT stage budget is 200 ms (aspirational, for a
# responsive turn) and the one real measurement is ~5,556 ms
# for whisper "small" on the contended, GPU-less build VM.
# 2,000 ms sits between them: ten times the budget (a turn
# already feels broken well before this) and about a third of
# the measured bad case (a machine merely as slow as the VM
# trips it decisively, a merely-mediocre one does not).
# ---------------------------------------------------------------------------
STT_STEP_DOWN_P95_MS = 2000.0

#: Rolling window of final-transcription durations the
#: in-session step-down decision reads.
STT_LATENCY_WINDOW_SAMPLES = 8

#: Samples required before the window may trip a step-down: a
#: single slow first transcription (cold model caches) must
#: not, on its own, downgrade a session.
STT_STEP_DOWN_MIN_SAMPLES = 5

#: Consecutive failed final transcriptions before the ladder
#: acts (swap to a smaller model once, then end the session):
#: one failure is a dropped turn, as it has always been; three
#: in a row is a stage that is down, not unlucky.
STT_CONSECUTIVE_FAILURE_LIMIT = 3

# ---------------------------------------------------------------------------
# The STT model step-down chain. faster-whisper models form a
# size ladder inside each family; stepping down stays inside
# the family (an ".en" model steps to the smaller ".en" model,
# a distil steps to the smaller distil) so a step never changes
# the language behaviour the user configured. HF repo ids and
# unknown names have no chain — no step-down is offered for
# them, honestly, rather than guessing at a stranger's model
# zoo.
# ---------------------------------------------------------------------------
_NEXT_SMALLER_MODEL = {
    "large-v3": "medium",
    "large-v2": "medium",
    "large-v1": "medium",
    "large": "medium",
    "large-v3-turbo": "medium",
    "turbo": "medium",
    "distil-large-v3.5": "distil-medium.en",
    "distil-large-v3": "distil-medium.en",
    "distil-large-v2": "distil-medium.en",
    "medium": "small",
    "medium.en": "small.en",
    "distil-medium.en": "distil-small.en",
    "small": "base",
    "small.en": "base.en",
    "distil-small.en": "base.en",
    "base": "tiny",
    "base.en": "tiny.en",
    "tiny": None,
    "tiny.en": None,
}


def next_smaller_stt_model(model: str | None) -> str | None:
    """The next model down the size ladder, or None when
    *model* is already the smallest (or not on the ladder)."""
    if not isinstance(model, str):
        return None
    return _NEXT_SMALLER_MODEL.get(model.strip())


def stt_fallback_chain(model: str | None) -> list[str]:
    """Every smaller model, in step-down order (possibly
    empty). Used for the load-failure retry at session start:
    a model that cannot even LOAD (not downloaded, corrupt)
    retries down the chain before the session gives up."""
    chain: list[str] = []
    current = next_smaller_stt_model(model)
    while current is not None:
        chain.append(current)
        current = next_smaller_stt_model(current)
    return chain


def _unavailable_message(reasons: Mapping[str, str]) -> str:
    """The refusal text: identical composition to the pipeline's
    long-standing one (every unavailable stage named), so a
    refusal reads the same as it did before the ladder."""
    detail = "; ".join(f"{stage}: {reason}" for stage, reason in reasons.items())
    return f"voice unavailable: {detail}"


# ---------------------------------------------------------------------------
# The session plan (pure).
# ---------------------------------------------------------------------------

PROFILE_FULL = "full"
PROFILE_DEGRADED = "degraded"
PROFILE_TEXT_ONLY = "text_only"
PROFILE_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class VoicePlan:
    """The ladder's decision for one session start.

    ``unavailable_reason`` is set exactly when ``profile`` is
    ``"unavailable"``; ``notices`` are user-facing sentences
    the start response carries and the overlay shows — every
    deviation from the full cascade is announced, never
    silent.
    """

    profile: str
    stt_model: str
    tts_enabled: bool = True
    vad_fallback: bool = False
    notices: tuple[str, ...] = ()
    unavailable_reason: str | None = None


def plan_voice_session(
    *,
    stage_reasons: Mapping[str, str | None],
    configured_stt_model: str,
    matrix_voice: Mapping[str, Any] | None = None,
    stt_p95_by_model: Mapping[str, float | None] | None = None,
    vad_replaceable: bool = True,
) -> VoicePlan:
    """Decide the session's starting profile. PURE: same
    inputs, same plan — the tests drive it with fixtures.

    ``stage_reasons`` maps stage name -> unavailable reason
    (None when the stage can run) for vad / stt / tts /
    transport. ``matrix_voice`` is the S10 matrix's
    ``local_voice`` verdict dict (or None when no report could
    be read — planning then proceeds on stage reasons alone).
    ``stt_p95_by_model`` carries this machine's measured STT
    p95 per model from the latency store.

    ``vad_replaceable`` is False when the caller injected its
    own VAD stage: the factory may only swap in the energy
    fallback for the default Silero stage it built itself, so
    an injected VAD that cannot run keeps the old refusal.
    """
    reasons = {stage: reason for stage, reason in stage_reasons.items()
               if reason}
    # Stages with no rung: without a transport there is no
    # audio channel at all, and without STT the session cannot
    # hear — no reduced mode covers either. A non-replaceable
    # VAD that cannot run joins them (see above).
    fatal = {stage: reasons[stage] for stage in ("transport", "stt")
             if stage in reasons}
    if "vad" in reasons and not vad_replaceable:
        fatal["vad"] = reasons["vad"]
    if fatal:
        # The refusal names EVERY unavailable stage, not just
        # the fatal ones — the pre-ladder contract, kept: the
        # user fixing the fatal stage deserves to know about
        # the others before the next attempt.
        return VoicePlan(
            profile=PROFILE_UNAVAILABLE,
            stt_model=configured_stt_model,
            tts_enabled=False,
            unavailable_reason=_unavailable_message(reasons),
        )
    # The matrix gate: stages individually runnable, but S10
    # judged local voice unavailable on this machine (e.g. too
    # little RAM for the speech models to load sanely). The
    # user gets that reason up front instead of a session that
    # fails at model-load time.
    if matrix_voice is not None and matrix_voice.get("status") == "unavailable":
        reason = str(matrix_voice.get("reason") or "").strip()
        return VoicePlan(
            profile=PROFILE_UNAVAILABLE,
            stt_model=configured_stt_model,
            tts_enabled=False,
            unavailable_reason=(
                f"voice unavailable: {reason}" if reason
                else "voice unavailable: the capability check "
                     "reports local voice unavailable on this machine"),
        )
    notices: list[str] = []
    tts_enabled = "tts" not in reasons
    if not tts_enabled:
        notices.append(
            "Speech output is unavailable "
            f"({reasons['tts']}) — you can still talk; replies "
            "will appear as text only this session.")
    vad_fallback = "vad" in reasons
    if vad_fallback:
        notices.append(
            "Voice activity detection is running in reduced "
            f"mode ({reasons['vad']}) — turn detection uses "
            "audio energy instead of the speech model, so speak "
            "clearly and leave a clear pause when you finish.")
    # Measured-latency warning for the configured model: the
    # session still STARTS on the user's configured model
    # (their Settings choice wins at start); the automatic
    # step-down happens in-session, from live measurements,
    # when the window trips — this notice is the warning the
    # plan asks for, with the measured number in it.
    slow_warning = False
    p95 = (stt_p95_by_model or {}).get(configured_stt_model)
    if p95 is not None and p95 > STT_STEP_DOWN_P95_MS:
        slow_warning = True
        smaller = next_smaller_stt_model(configured_stt_model)
        if smaller is not None:
            notices.append(
                "Speech recognition has measured slow on this "
                f"machine (~{p95 / 1000.0:.1f} s per turn with "
                f"'{configured_stt_model}'). If it stays slow, "
                f"this session will switch to the faster "
                f"'{smaller}' model on its own — or pick that "
                "model now in Settings → Speech.")
        else:
            notices.append(
                "Speech recognition has measured slow on this "
                f"machine (~{p95 / 1000.0:.1f} s per turn with "
                f"'{configured_stt_model}') and no smaller "
                "speech model is available — expect slow turns.")
    if not tts_enabled:
        profile = PROFILE_TEXT_ONLY
    elif vad_fallback or slow_warning:
        profile = PROFILE_DEGRADED
    else:
        profile = PROFILE_FULL
    return VoicePlan(
        profile=profile,
        stt_model=configured_stt_model,
        tts_enabled=tts_enabled,
        vad_fallback=vad_fallback,
        notices=tuple(notices),
    )


# ---------------------------------------------------------------------------
# Measured per-stage latency, persisted (mirrors routing/latency.py).
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH = Path.home() / ".aether" / "voice_latency.db"

#: Samples per (stage, model) that percentile summaries read.
VOICE_LATENCY_WINDOW = 100

#: Rows retained per (stage, model); older rows are pruned on
#: insert so the store stays bounded forever.
MAX_SAMPLES_PER_KEY = 200


def _percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile (the numpy default
    method) — the same computation routing/latency.py uses."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return float(ordered[low] + (ordered[high] - ordered[low]) * fraction)


@dataclass(frozen=True)
class VoiceLatencySummary:
    """Percentile summary over one stage+model's recent
    measured durations. Metrics are None only when the summary
    itself is (no samples) — see :meth:`VoiceLatencyStore.summary`."""

    stage: str
    model: str
    samples: int
    p50_ms: float | None
    p95_ms: float | None
    mean_ms: float | None


class VoiceLatencyStore:
    """SQLite-backed per-stage latency samples (schema v1).

    Keys are (stage, model): for ``stt`` the model is the
    whisper model name — the key the step-down decisions read;
    for ``tts`` it is the voice id; for ``talker`` it is "".
    """

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._initialized = False

    def _get_conn(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        if self._initialized:
            return
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS voice_stage_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    stage TEXT NOT NULL,
                    model TEXT NOT NULL,
                    duration_ms REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_voice_stage_ts
                ON voice_stage_samples(stage, model, timestamp DESC, id DESC)
            """)
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
        self._initialized = True

    def record_sample(self, *, stage: str, model: str,
                      duration_ms: float) -> None:
        """Insert one measured duration and prune the key's
        retained rows to the newest MAX_SAMPLES_PER_KEY."""
        self.init_db()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO voice_stage_samples (
                    timestamp, stage, model, duration_ms
                ) VALUES (?, ?, ?, ?)
                """,
                (time.time(), stage, model, float(duration_ms)),
            )
            conn.execute(
                """
                DELETE FROM voice_stage_samples
                WHERE stage = ? AND model = ? AND id NOT IN (
                    SELECT id FROM voice_stage_samples
                    WHERE stage = ? AND model = ?
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                )
                """,
                (stage, model, stage, model, MAX_SAMPLES_PER_KEY),
            )
            conn.commit()

    def summary(self, stage: str, model: str) -> VoiceLatencySummary | None:
        """Percentiles over the key's newest VOICE_LATENCY_WINDOW
        samples, or None when the key has no samples at all —
        an unmeasured model is unknown, never zero."""
        self.init_db()
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT duration_ms FROM voice_stage_samples
                WHERE stage = ? AND model = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (stage, model, VOICE_LATENCY_WINDOW),
            ).fetchall()
        if not rows:
            return None
        values = [float(r["duration_ms"]) for r in rows]
        return VoiceLatencySummary(
            stage=stage,
            model=model,
            samples=len(values),
            p50_ms=_percentile(values, 50),
            p95_ms=_percentile(values, 95),
            mean_ms=sum(values) / len(values),
        )


GLOBAL_VOICE_LATENCY_STORE: VoiceLatencyStore | None = None


def get_voice_latency_store() -> VoiceLatencyStore:
    global GLOBAL_VOICE_LATENCY_STORE
    if GLOBAL_VOICE_LATENCY_STORE is None:
        GLOBAL_VOICE_LATENCY_STORE = VoiceLatencyStore()
    return GLOBAL_VOICE_LATENCY_STORE


def record_voice_stage_sample(stage: str, model: str, duration_ms: float,
                              *, store: VoiceLatencyStore | None = None) -> bool:
    """Record one stage duration in the global store.
    Observational only — never raises; False when recording
    failed (the turn it describes is unaffected either way)."""
    try:
        (store or get_voice_latency_store()).record_sample(
            stage=stage, model=model, duration_ms=duration_ms)
        return True
    except Exception:  # latency recording is observational only.
        logger.debug("voice latency recording failed", exc_info=True)
        return False


def stt_p95_by_model(models: list[str], *,
                     store: VoiceLatencyStore | None = None,
                     ) -> dict[str, float | None]:
    """Measured STT p95 per model (None = unmeasured), for the
    planner. Never raises: a broken store reads as "nothing
    measured", which plans exactly like a fresh machine."""
    result: dict[str, float | None] = {}
    try:
        resolved = store or get_voice_latency_store()
        for model in models:
            summary = resolved.summary("stt", model)
            result[model] = summary.p95_ms if summary else None
    except Exception:  # planning must never fail on the store
        logger.debug("voice latency read failed", exc_info=True)
        return {model: None for model in models}
    return result


# ---------------------------------------------------------------------------
# One session's runtime degradation tracker.
# ---------------------------------------------------------------------------

StageRecorder = Callable[[str, str, float], bool]


class SessionDegradation:
    """The ladder, live for one session.

    Constructed from the :class:`VoicePlan` the session
    started with; the delivery half reports observations and
    executes the returned decisions:

    * :meth:`note_stt_success` — records the duration (store +
      rolling window); returns ``(notice, new_model)`` when
      the window's p95 stays over the step-down threshold —
      the caller swaps the STT engine and emits the notice.
      The window then clears, so the new model must earn its
      own step-down with a fresh window of slow turns; the
      chain only ever walks DOWN (smaller models), so repeated
      step-downs cannot oscillate. When no smaller model
      exists the decision latches off for the session.
    * :meth:`note_stt_failure` — ``("drop", None)`` below the
      consecutive-failure limit (the historical behaviour);
      ``("swap", model)`` once at the limit when a smaller
      model exists; ``("end", reason)`` when the session
      cannot honestly continue hearing.
    * :meth:`note_tts_failure` — the notice for the one
      transition into text-only, then None forever.

    Nothing here ever steps a session back UP: hysteresis is
    structural, not a timer.
    """

    def __init__(self, plan: VoicePlan, *,
                 recorder: StageRecorder | None = None):
        self._plan = plan
        self.profile = plan.profile
        self.stt_model = plan.stt_model
        self.tts_enabled = plan.tts_enabled
        self._recorder = recorder
        self._stt_window: deque[float] = deque(
            maxlen=STT_LATENCY_WINDOW_SAMPLES)
        self._step_down_exhausted = False
        self._stt_failures = 0
        self._failure_swap_used = False
        self._tts_failure_noticed = False
        self._extra_notices: list[str] = []

    @property
    def plan(self) -> VoicePlan:
        return self._plan

    @property
    def notices(self) -> tuple[str, ...]:
        """Every start notice: the plan's, plus any added by
        the start path itself (the STT load-failure fallback
        lands here, before the start response is composed)."""
        return self._plan.notices + tuple(self._extra_notices)

    def add_start_notice(self, notice: str) -> None:
        self._extra_notices.append(notice)

    def record_stage_sample(self, stage: str, model: str,
                            duration_ms: float) -> None:
        if self._recorder is None:
            return
        try:
            self._recorder(stage, model, float(duration_ms))
        except Exception:  # the recorder contract is never-raise; belt and braces
            logger.debug("voice stage recorder failed", exc_info=True)

    def note_stt_success(self, duration_ms: float,
                         ) -> tuple[str, str] | None:
        """One successful final transcription. Returns
        ``(notice, new_model)`` when the latency window trips
        the one-shot step-down, else None."""
        self._stt_failures = 0
        self.record_stage_sample("stt", self.stt_model, duration_ms)
        self._stt_window.append(float(duration_ms))
        if self._step_down_exhausted:
            return None
        if len(self._stt_window) < STT_STEP_DOWN_MIN_SAMPLES:
            return None
        p95 = _percentile(list(self._stt_window), 95)
        if p95 is None or p95 <= STT_STEP_DOWN_P95_MS:
            return None
        smaller = next_smaller_stt_model(self.stt_model)
        if smaller is None:
            # The window has spoken and there is nowhere to
            # step to: latch off so it is not re-decided (and
            # re-measured against the same wall) every turn.
            self._step_down_exhausted = True
            return None
        notice = (
            "Speech recognition is slow on this machine "
            f"(~{p95 / 1000.0:.1f} s per turn with "
            f"'{self.stt_model}'), so I've switched to the "
            f"faster '{smaller}' model — it may be a little "
            "less accurate.")
        self.stt_model = smaller
        self._stt_window.clear()
        if self.profile == PROFILE_FULL:
            self.profile = PROFILE_DEGRADED
        return notice, smaller

    def note_stt_failure(self, error: str) -> tuple[str, str | None]:
        """One failed final transcription. Returns
        ``("drop", None)`` / ``("swap", model)`` /
        ``("end", reason)`` — see the class docstring. The
        caller reads :attr:`stt_model` BEFORE calling when it
        needs the failing model's name: on "swap" the tracker
        has already advanced to the new model."""
        self._stt_failures += 1
        if self._stt_failures < STT_CONSECUTIVE_FAILURE_LIMIT:
            return "drop", None
        smaller = next_smaller_stt_model(self.stt_model)
        if smaller is not None and not self._failure_swap_used:
            self._failure_swap_used = True
            self._stt_failures = 0
            self.stt_model = smaller
            return "swap", smaller
        return "end", (
            "voice unavailable: speech-to-text failed "
            f"{self._stt_failures} times in a row with "
            f"'{self.stt_model}' (last error: {error}) — the "
            "session has ended. Starting a new voice session "
            "will try again.")

    def note_tts_failure(self) -> str | None:
        """The TTS stage failed mid-session: the session goes
        text-only. Returns the transition notice exactly once."""
        if self._tts_failure_noticed or not self.tts_enabled:
            return None
        self._tts_failure_noticed = True
        self.tts_enabled = False
        self.profile = PROFILE_TEXT_ONLY
        return (
            "Speech output just failed, so replies will be "
            "text only for the rest of this session. You can "
            "keep talking — I'll keep listening and replying "
            "in text.")
