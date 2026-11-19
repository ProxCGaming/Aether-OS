"""Rate-limit evidence and provider headroom (P1b).

No provider API tells Aether "you have X requests left" for the
owner's keys, so headroom is **learned from evidence, never
invented**: this module keeps one row per observed rate-limit
event (provider, model, when, and the reset time *when the
provider's error states one* — otherwise null), and derives a
three-state heat signal at routing time:

* **hot** — a rate-limit event inside the rolling
  :data:`HOT_WINDOW_S` (60 min), or a known reset time still in
  the future;
* **warm** — an event inside :data:`WARM_WINDOW_S` (6 h) but
  nothing hotter;
* **cool** — no evidence. Cool is the absence of evidence, not a
  claim about the provider's real quota.

Three explainable states, deliberately not a fake percentage:
the routing reason strings can say "provider X is cooling down
after a rate limit at 14:32" and mean exactly what happened.

House style mirrors the latency store: a bounded SQLite file in
the data home (``~/.aether/rate_limits.db``, schema version 1),
short-lived connections, and recording that **never raises** —
a broken or corrupt store degrades routing to the pre-P1b
behaviour (everyone reads as cool), exactly like every other
routing input. A successful call cools a provider immediately
(:func:`clear_provider_heat`, wired from
``ProviderHealthManager.record_success``): stale heat must not
punish a provider that has demonstrably recovered.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("aether_engine.routing.headroom")

_DEFAULT_DB_PATH = Path.home() / ".aether" / "rate_limits.db"

#: An event younger than this makes its provider hot.
HOT_WINDOW_S = 3600.0

#: An event younger than this (but not hot) makes its provider warm.
WARM_WINDOW_S = 6 * 3600.0

#: Rows retained in the evidence store; older rows are pruned on
#: insert so the store stays bounded forever.
EVENT_ROW_CAP = 1000

HEAT_COOL = "cool"
HEAT_WARM = "warm"
HEAT_HOT = "hot"

_HEAT_RANK = {HEAT_COOL: 0, HEAT_WARM: 1, HEAT_HOT: 2}


def heat_rank(state: str | None) -> int:
    """Ordering rank for a heat state (hot sorts last in routing)."""
    return _HEAT_RANK.get(state or HEAT_COOL, 0)


@dataclass(frozen=True)
class HeatInfo:
    """One provider's derived headroom signal."""

    provider: str
    state: str = HEAT_COOL
    last_event_at: float | None = None
    reset_at: float | None = None


def heat_state_for(
    last_event_at: float | None,
    reset_at: float | None,
    *,
    now: float | None = None,
) -> str:
    """The heat state for one provider's newest evidence.

    A known reset time in the future is hot regardless of the
    event's age (the provider itself said when it resets); a
    reset time in the past leaves only the rolling windows.
    """
    current = now if now is not None else time.time()
    if reset_at is not None and reset_at > current:
        return HEAT_HOT
    if last_event_at is None:
        return HEAT_COOL
    age = current - last_event_at
    if age <= HOT_WINDOW_S:
        return HEAT_HOT
    if age <= WARM_WINDOW_S:
        return HEAT_WARM
    return HEAT_COOL


# ── Reset-time extraction ─────────────────────────────────────────
#
# Providers rarely state a reset time, but when they do it is in
# the error text: Gemini's "Please retry in 12.345s" and its
# structured ``retryDelay: "12s"``, the HTTP ``Retry-After``
# header echoed into messages, or an explicit ISO timestamp.
# Anything else is honestly unknown (null), never guessed.

_RETRY_IN_RE = re.compile(r"retry in (\d+(?:\.\d+)?)\s*s", re.IGNORECASE)
_RETRY_DELAY_RE = re.compile(r"retry_?delay[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)s", re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(r"retry-after[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)", re.IGNORECASE)
_RESET_IN_RE = re.compile(
    r"reset in (\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes)",
    re.IGNORECASE)
_ISO_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")


def extract_reset_at(text: Any, *, now: float | None = None) -> float | None:
    """The reset time stated in an error message, as epoch seconds.

    Relative hints ("retry in 30s") resolve against ``now``.
    Returns None when the message states no reset time.
    """
    if not text:
        return None
    current = now if now is not None else time.time()
    message = str(text)
    for pattern in (_RETRY_IN_RE, _RETRY_DELAY_RE, _RETRY_AFTER_RE):
        match = pattern.search(message)
        if match:
            return current + float(match.group(1))
    match = _RESET_IN_RE.search(message)
    if match:
        amount = float(match.group(1))
        if match.group(2).lower().startswith("m"):
            amount *= 60.0
        return current + amount
    match = _ISO_TS_RE.search(message)
    if match:
        try:
            parsed = datetime.fromisoformat(match.group(0).replace("Z", "+00:00"))  # noqa: FURB162 — provider text is arbitrary; keep the explicit Z rewrite rather than depend on fromisoformat's 3.11+ Z handling
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            return parsed.timestamp()
        except ValueError:
            return None
    return None


_RATE_LIMIT_TEXT_MARKERS = ("RESOURCE_EXHAUSTED", "Rate limit exceeded")


def is_rate_limit_signal(exc_or_text: Any) -> bool:
    """True when an exception/message is rate-limit evidence.

    Mirrors ``routing.fallback.classify_provider_error``'s
    recognition (typed ``RateLimitError``, a standalone 429,
    ``RESOURCE_EXHAUSTED``) so the evidence store learns from
    exactly the failures the classifier already calls rate
    limits — no second, divergent definition.
    """
    if exc_or_text is None:
        return False
    try:
        from aether_engine.providers.base import RateLimitError

        if isinstance(exc_or_text, RateLimitError):
            return True
    except Exception:  # recognition must never raise.
        logger.debug("rate-limit type recognition failed", exc_info=True)
    message = str(exc_or_text)
    if any(marker in message for marker in _RATE_LIMIT_TEXT_MARKERS):
        return True
    return re.search(r"(?<!\d)429(?!\d)", message) is not None


# ── The evidence store ────────────────────────────────────────────


class RateLimitStore:
    """SQLite-backed rate-limit events (schema version 1)."""

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
                CREATE TABLE IF NOT EXISTS rate_limit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    provider_name TEXT NOT NULL,
                    model_name TEXT,
                    reset_at REAL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_rate_limit_provider_ts
                ON rate_limit_events(provider_name, timestamp DESC, id DESC)
            """)
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
        self._initialized = True

    def record(
        self,
        *,
        provider_name: str,
        model_name: str | None = None,
        reset_at: float | None = None,
        timestamp: float | None = None,
    ) -> None:
        """Insert one rate-limit event and prune to the newest
        :data:`EVENT_ROW_CAP` rows."""
        self.init_db()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO rate_limit_events "
                "(timestamp, provider_name, model_name, reset_at) "
                "VALUES (?, ?, ?, ?)",
                (float(timestamp if timestamp is not None else time.time()),
                 provider_name, model_name, reset_at),
            )
            conn.execute(
                "DELETE FROM rate_limit_events WHERE id NOT IN "
                "(SELECT id FROM rate_limit_events ORDER BY id DESC LIMIT ?)",
                (EVENT_ROW_CAP,),
            )
            conn.commit()

    def latest(self, provider_name: str) -> tuple[float, float | None] | None:
        """``(timestamp, reset_at)`` of the provider's newest
        event, or None when it has none."""
        self.init_db()
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT timestamp, reset_at FROM rate_limit_events "
                "WHERE provider_name = ? "
                "ORDER BY timestamp DESC, id DESC LIMIT 1",
                (provider_name,),
            ).fetchone()
        if row is None:
            return None
        return float(row["timestamp"]), (
            float(row["reset_at"]) if row["reset_at"] is not None else None)

    def clear(self, provider_name: str) -> None:
        """Forget a provider's events (it demonstrably recovered)."""
        self.init_db()
        with self._get_conn() as conn:
            conn.execute(
                "DELETE FROM rate_limit_events WHERE provider_name = ?",
                (provider_name,),
            )
            conn.commit()


GLOBAL_RATE_LIMIT_STORE: RateLimitStore | None = None


def get_rate_limit_store() -> RateLimitStore:
    global GLOBAL_RATE_LIMIT_STORE
    if GLOBAL_RATE_LIMIT_STORE is None:
        GLOBAL_RATE_LIMIT_STORE = RateLimitStore()
    return GLOBAL_RATE_LIMIT_STORE


# ── Module-level seams (all fail-open, none raise) ────────────────


def record_rate_limit(
    provider_name: str,
    model_name: str | None = None,
    *,
    reset_at: float | None = None,
    timestamp: float | None = None,
    store: RateLimitStore | None = None,
) -> bool:
    """Record one rate-limit event. Returns False (never raises)
    when recording failed — the failure it describes is unaffected."""
    if not provider_name:
        return False
    try:
        (store or get_rate_limit_store()).record(
            provider_name=str(provider_name),
            model_name=str(model_name) if model_name else None,
            reset_at=reset_at,
            timestamp=timestamp,
        )
        return True
    except Exception:
        logger.debug("Rate-limit evidence recording failed", exc_info=True)
        return False


def note_rate_limit(
    exc_or_text: Any,
    provider_name: str,
    model_name: str | None = None,
    *,
    store: RateLimitStore | None = None,
) -> bool:
    """Record *exc_or_text* as evidence when it is a rate limit.

    The writer seam for the places 429s are already classified:
    callers pass the failure they just handled; non-rate-limit
    failures record nothing. The reset time is taken from the
    error text when the provider stated one, else null.
    """
    try:
        if not is_rate_limit_signal(exc_or_text):
            return False
        return record_rate_limit(
            provider_name, model_name,
            reset_at=extract_reset_at(exc_or_text),
            store=store)
    except Exception:
        logger.debug("Rate-limit noting failed", exc_info=True)
        return False


def heat_info(
    provider_name: str,
    *,
    now: float | None = None,
    store: RateLimitStore | None = None,
) -> HeatInfo:
    """One provider's current heat. A store failure reads as
    cool — routing degrades to the pre-P1b behaviour, fail-open."""
    try:
        latest = (store or get_rate_limit_store()).latest(str(provider_name))
    except Exception:
        logger.debug("Rate-limit evidence read failed", exc_info=True)
        return HeatInfo(provider=str(provider_name))
    if latest is None:
        return HeatInfo(provider=str(provider_name))
    last_event_at, reset_at = latest
    return HeatInfo(
        provider=str(provider_name),
        state=heat_state_for(last_event_at, reset_at, now=now),
        last_event_at=last_event_at,
        reset_at=reset_at,
    )


def heat_states(
    provider_names: Any,
    *,
    now: float | None = None,
    store: RateLimitStore | None = None,
) -> dict[str, HeatInfo]:
    """Batch :func:`heat_info` for a set of providers."""
    result: dict[str, HeatInfo] = {}
    for name in provider_names or []:
        key = str(name)
        if key not in result:
            result[key] = heat_info(key, now=now, store=store)
    return result


def clear_provider_heat(
    provider_name: str,
    *,
    store: RateLimitStore | None = None,
) -> None:
    """Forget a provider's rate-limit evidence (never raises).

    Wired from ``ProviderHealthManager.record_success``: a
    successful call from any path is proof of recovery, so the
    heat clears immediately instead of lingering for the window.
    """
    if not provider_name:
        return
    try:
        (store or get_rate_limit_store()).clear(str(provider_name))
    except Exception:
        logger.debug("Rate-limit heat clear failed", exc_info=True)
