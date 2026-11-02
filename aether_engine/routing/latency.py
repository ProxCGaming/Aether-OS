"""Per-model measured-latency samples and percentile summaries.

Finding E5-F11: the plan addendum (Epic 5, omp #37) promises a
"Latency bench shape: TTFT/prefill/decode percentiles recorded per
model so routing decisions use measured latency." Until now the
capability was absent, not mis-measured — health kept one
last-duration scalar per provider, self-tests recorded whole-test
durations and a mean, and the routing policy selected with no
measured-latency input at all.

This module is the measurement half. Samples come from REAL provider
calls only, recorded by ``LiteLLMProvider`` at the same firing
points as the usage ledger (finding M19: the success path and the
GeneratorExit early-close paths — a call whose output was consumed
happened either way). Persistence mirrors the cost ledger's house
style: a bounded SQLite store in the data home
(``~/.aether/latency.db``, schema version 1), one short-lived
connection per operation.

Honesty rules:

* **TTFT** is the observed time from request dispatch to the first
  streamed event of the call. A separate "prefill" figure is NOT
  fabricated: from the client side of a streaming call, prefill is
  not separately observable — the first token arrives only after
  prefill completes — so ``ttft_ms`` is the prefill-inclusive
  measure and is named as exactly that.
* **Decode throughput** (tokens/second over the post-first-token
  span) is recorded only when the provider reported an output-token
  count AND a positive decode window was observed. It is never
  estimated from chunk counts.
* Recording is observational. A store failure must never break the
  call that produced the sample, so :func:`record_latency_sample`
  never raises.

Percentiles are computed over the newest :data:`WINDOW` samples per
(provider, model), using linear interpolation between closest
ranks; the store retains at most :data:`MAX_SAMPLES_PER_MODEL` rows
per model, pruned on insert, so it stays bounded forever.
"""
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("aether_engine.routing.latency")

_DEFAULT_DB_PATH = Path.home() / ".aether" / "latency.db"

#: Samples per (provider, model) that percentile summaries read.
WINDOW = 100

#: Rows retained per (provider, model); older rows are pruned on
#: insert so the store cannot grow without bound.
MAX_SAMPLES_PER_MODEL = 200


def _percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile (the numpy default method)."""
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
class LatencySummary:
    """Percentile summary over a model's recent measured samples.

    Every metric is None when no sample carried it — a model whose
    calls never reported token usage has totals and TTFT but no
    decode rate, and the summary says so instead of inventing one.
    """

    provider: str
    model: str
    samples: int
    ttft_p50_ms: float | None = None
    ttft_p95_ms: float | None = None
    total_p50_ms: float | None = None
    total_p95_ms: float | None = None
    decode_tps_p50: float | None = None
    decode_tps_p95: float | None = None


class LatencyStore:
    """SQLite-backed per-model latency samples (schema version 1)."""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._initialized = False

    def _get_conn(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        """Initialise schema (PRAGMA user_version = 1)."""
        if self._initialized:
            return
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS latency_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    provider_name TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    ttft_ms REAL,
                    total_ms REAL,
                    decode_ms REAL,
                    output_tokens INTEGER,
                    decode_tps REAL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_latency_model_ts
                ON latency_samples(provider_name, model_name, timestamp DESC, id DESC)
            """)
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
        self._initialized = True

    def record_sample(
        self,
        *,
        provider_name: str,
        model_name: str,
        ttft_ms: float | None = None,
        total_ms: float | None = None,
        decode_ms: float | None = None,
        output_tokens: int | None = None,
        decode_tps: float | None = None,
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        """Insert one completed call's sample and prune the model's
        retained rows to the newest :data:`MAX_SAMPLES_PER_MODEL`.

        ``decode_tps`` is derived from ``output_tokens`` /
        ``decode_ms`` when not given explicitly — and only when both
        were genuinely observed (positive decode window, positive
        token count); otherwise the sample carries no decode rate.
        """
        self.init_db()
        if (
            decode_tps is None
            and decode_ms is not None and decode_ms > 0
            and output_tokens is not None and output_tokens > 0
        ):
            decode_tps = float(output_tokens) * 1000.0 / float(decode_ms)
        row = {
            "timestamp": float(timestamp if timestamp is not None else time.time()),
            "provider_name": provider_name,
            "model_name": model_name,
            "ttft_ms": ttft_ms,
            "total_ms": total_ms,
            "decode_ms": decode_ms,
            "output_tokens": output_tokens,
            "decode_tps": decode_tps,
        }
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO latency_samples (
                    timestamp, provider_name, model_name, ttft_ms,
                    total_ms, decode_ms, output_tokens, decode_tps
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["timestamp"], row["provider_name"],
                    row["model_name"], row["ttft_ms"], row["total_ms"],
                    row["decode_ms"], row["output_tokens"],
                    row["decode_tps"],
                ),
            )
            conn.execute(
                """
                DELETE FROM latency_samples
                WHERE provider_name = ? AND model_name = ? AND id NOT IN (
                    SELECT id FROM latency_samples
                    WHERE provider_name = ? AND model_name = ?
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                )
                """,
                (provider_name, model_name,
                 provider_name, model_name, MAX_SAMPLES_PER_MODEL),
            )
            conn.commit()
        return row

    def summary(self, provider_name: str, model_name: str) -> LatencySummary | None:
        """Percentiles over the model's newest :data:`WINDOW`
        samples, or None when the model has no samples at all."""
        self.init_db()
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT ttft_ms, total_ms, decode_tps FROM latency_samples
                WHERE provider_name = ? AND model_name = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (provider_name, model_name, WINDOW),
            ).fetchall()
        if not rows:
            return None
        ttfts = [r["ttft_ms"] for r in rows if r["ttft_ms"] is not None]
        totals = [r["total_ms"] for r in rows if r["total_ms"] is not None]
        rates = [r["decode_tps"] for r in rows if r["decode_tps"] is not None]
        return LatencySummary(
            provider=provider_name,
            model=model_name,
            samples=len(rows),
            ttft_p50_ms=_percentile(ttfts, 50),
            ttft_p95_ms=_percentile(ttfts, 95),
            total_p50_ms=_percentile(totals, 50),
            total_p95_ms=_percentile(totals, 95),
            decode_tps_p50=_percentile(rates, 50),
            decode_tps_p95=_percentile(rates, 95),
        )

    def ttft_p50_ms(self, provider_name: str, model_name: str) -> float | None:
        """The routing tie-break input: the model's measured median
        time-to-first-token, or None when it has no TTFT samples."""
        summary = self.summary(provider_name, model_name)
        return summary.ttft_p50_ms if summary else None


GLOBAL_LATENCY_STORE: LatencyStore | None = None


def get_latency_store() -> LatencyStore:
    global GLOBAL_LATENCY_STORE
    if GLOBAL_LATENCY_STORE is None:
        GLOBAL_LATENCY_STORE = LatencyStore()
    return GLOBAL_LATENCY_STORE


def record_latency_sample(
    *,
    provider_name: str,
    model_name: str,
    ttft_ms: float | None = None,
    total_ms: float | None = None,
    decode_ms: float | None = None,
    output_tokens: int | None = None,
    store: LatencyStore | None = None,
) -> dict[str, Any] | None:
    """Record one completed call's latency sample in the global
    store. Observational only — never raises; returns the row, or
    None when recording failed (the call it describes is unaffected
    either way)."""
    try:
        return (store or get_latency_store()).record_sample(
            provider_name=provider_name,
            model_name=model_name,
            ttft_ms=ttft_ms,
            total_ms=total_ms,
            decode_ms=decode_ms,
            output_tokens=output_tokens,
        )
    except Exception:  # latency recording is observational only.
        logger.debug("Latency recording failed", exc_info=True)
        return None
