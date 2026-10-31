"""Usage ledger managing ~/.aether/usage.db (Epic 8, E8-A8.1a).

The persistent per-call cost record every cost surface reads: one row
per completed model call — cloud and local — with model, provider,
token counts, USD cost, pricing status, and session/task/agent
attribution. Local calls record ``is_local = 1`` at $0.00; models no
price source knows record ``pricing_status = 'unpriced'`` at $0.00
(honest, never a silent zero); calls whose tokens had to be estimated
record ``pricing_status = 'estimated'``.

Attribution flows through a ContextVar: the WebSocket task pump sets
the session (and, once known, the task) for the task it is streaming,
and the provider recording hook reads it when a call completes. The
hook also publishes ``TASK_COST_UPDATE`` on the event bus so a running
graph task's header cost ticks live; the payload's figures are read
back from this ledger, so what the header shows is what the rows say.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from aether_common.contracts import Event, EventType
from aether_engine.cost import pricing
from aether_engine.event_bus import event_bus

logger = logging.getLogger("aether_engine.cost.ledger")

_DEFAULT_DB_PATH = Path.home() / ".aether" / "usage.db"

# ---------------------------------------------------------------------------
# Call attribution context
# ---------------------------------------------------------------------------

_usage_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "aether_usage_context", default=None)


def set_usage_context(
    *,
    session_id: str | None = None,
    task_id: str | None = None,
    task_class: str | None = None,
):
    """Merge attribution fields into the current usage context.

    Returns the ContextVar token; pass it to
    :func:`reset_usage_context` to restore the previous value. Fields
    left as None keep their current value, so a caller that learns the
    task id late (the WebSocket pump, once TASK_CREATED flows past)
    can refine the context without restating the session.
    """
    current = dict(_usage_context.get() or {})
    if session_id is not None:
        current["session_id"] = session_id
    if task_id is not None:
        current["task_id"] = task_id
    if task_class is not None:
        current["task_class"] = task_class
    return _usage_context.set(current)


def reset_usage_context(token) -> None:
    _usage_context.reset(token)


def get_usage_context() -> dict[str, Any]:
    return dict(_usage_context.get() or {})


@contextmanager
def usage_context(
    *,
    session_id: str | None = None,
    task_id: str | None = None,
    task_class: str | None = None,
):
    """Scope a fresh usage context (used by tests and one-shot calls)."""
    fresh: dict[str, Any] = {}
    if session_id is not None:
        fresh["session_id"] = session_id
    if task_id is not None:
        fresh["task_id"] = task_id
    if task_class is not None:
        fresh["task_class"] = task_class
    token = _usage_context.set(fresh)
    try:
        yield
    finally:
        _usage_context.reset(token)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class UsageLedger:
    """SQLite-backed per-call usage ledger (schema version 1)."""

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
            # PRD §E8-A8.1 schema, plus pricing_status (PRD open
            # point 8: unpriced calls record pricing_status =
            # 'unpriced' rather than a bare $0.00).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS usage_ledger (
                    id TEXT PRIMARY KEY,
                    timestamp INTEGER NOT NULL,
                    session_id TEXT,
                    task_id TEXT,
                    agent_name TEXT,
                    model_name TEXT NOT NULL,
                    provider_name TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL NOT NULL DEFAULT 0.0,
                    is_local INTEGER NOT NULL DEFAULT 0,
                    pricing_status TEXT NOT NULL DEFAULT 'priced',
                    task_class TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_ledger(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_task ON usage_ledger(task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage_ledger(timestamp DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_ledger(model_name)")
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
        self._initialized = True

    def record_call(
        self,
        *,
        model_name: str,
        provider_name: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        is_local: bool = False,
        pricing_status: str = "priced",
        session_id: str | None = None,
        task_id: str | None = None,
        agent_name: str | None = None,
        task_class: str | None = None,
        timestamp: int | None = None,
    ) -> dict[str, Any]:
        """Insert one completed call's row and return it."""
        self.init_db()
        row = {
            "id": str(uuid.uuid4()),
            "timestamp": int(timestamp if timestamp is not None else time.time()),
            "session_id": session_id,
            "task_id": task_id,
            "agent_name": agent_name,
            "model_name": model_name,
            "provider_name": provider_name,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "total_tokens": int(input_tokens) + int(output_tokens),
            "cost_usd": float(cost_usd),
            "is_local": 1 if is_local else 0,
            "pricing_status": pricing_status,
            "task_class": task_class,
        }
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO usage_ledger (
                    id, timestamp, session_id, task_id, agent_name,
                    model_name, provider_name, input_tokens, output_tokens,
                    total_tokens, cost_usd, is_local, pricing_status,
                    task_class
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"], row["timestamp"], row["session_id"],
                    row["task_id"], row["agent_name"], row["model_name"],
                    row["provider_name"], row["input_tokens"],
                    row["output_tokens"], row["total_tokens"],
                    row["cost_usd"], row["is_local"],
                    row["pricing_status"], row["task_class"],
                ),
            )
            conn.commit()
        return row

    def query(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> dict[str, Any]:
        """Ledger query behind USAGE_LEDGER_QUERY_REQUEST.

        Returns ``{"entries": [...], "totals": {...}}``. Totals are
        aggregated from the returned entries themselves, so a surface
        can always reconcile what it displays against the rows.
        """
        self.init_db()
        clauses: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if start_ts is not None:
            clauses.append("timestamp >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("timestamp <= ?")
            params.append(end_ts)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._get_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM usage_ledger {where} ORDER BY timestamp ASC, rowid ASC",
                params,
            ).fetchall()
        entries = [dict(r) for r in rows]

        by_model: dict[str, dict[str, Any]] = {}
        by_agent: dict[str, dict[str, Any]] = {}
        total_cost = 0.0
        total_in = 0
        total_out = 0
        for entry in entries:
            total_cost += entry["cost_usd"]
            total_in += entry["input_tokens"]
            total_out += entry["output_tokens"]
            for bucket, key in ((by_model, entry["model_name"]),
                                (by_agent, entry["agent_name"] or "unknown")):
                agg = bucket.setdefault(
                    key, {"cost_usd": 0.0, "input_tokens": 0,
                          "output_tokens": 0, "calls": 0})
                agg["cost_usd"] += entry["cost_usd"]
                agg["input_tokens"] += entry["input_tokens"]
                agg["output_tokens"] += entry["output_tokens"]
                agg["calls"] += 1

        return {
            "entries": entries,
            "totals": {
                "total_cost_usd": total_cost,
                "input_tokens": total_in,
                "output_tokens": total_out,
                "by_model": by_model,
                "by_agent": by_agent,
            },
        }

    def task_total(self, task_id: str) -> float:
        self.init_db()
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM usage_ledger WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return float(row[0]) if row else 0.0

    def session_total(self, session_id: str) -> float:
        self.init_db()
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM usage_ledger WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return float(row[0]) if row else 0.0


GLOBAL_USAGE_LEDGER: UsageLedger | None = None


def get_usage_ledger() -> UsageLedger:
    global GLOBAL_USAGE_LEDGER
    if GLOBAL_USAGE_LEDGER is None:
        GLOBAL_USAGE_LEDGER = UsageLedger()
    return GLOBAL_USAGE_LEDGER


# ---------------------------------------------------------------------------
# Provider recording hook
# ---------------------------------------------------------------------------


def record_provider_call(
    *,
    model_name: str,
    provider_name: str,
    input_tokens: int | None,
    output_tokens: int | None,
    agent_name: str | None = None,
    pricing_model: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    output_text: str = "",
) -> dict[str, Any] | None:
    """Price, record, and broadcast one completed provider call.

    Reads session/task attribution from the usage context. When the
    provider reported no token usage, counts are estimated and the
    row is labelled ``estimated``; when estimation is impossible the
    row records zero tokens as ``unpriced``. Publishes
    ``TASK_COST_UPDATE`` (figures read back from the ledger) when the
    call belongs to a task. Never raises — a ledger failure must not
    break the call that produced it.
    """
    try:
        ctx = get_usage_context()
        if not ctx:
            # Outside a task's attribution context (provider probes,
            # reachability pings, self-tests): not agent work, so no
            # row — the ledger records calls made by agents on tasks.
            return None
        estimated = False
        in_tokens = input_tokens
        out_tokens = output_tokens
        if in_tokens is None and out_tokens is None:
            counts = pricing.estimate_token_counts(
                model=pricing_model or model_name,
                messages=messages,
                output_text=output_text,
            )
            if counts is not None:
                in_tokens, out_tokens = counts
                estimated = True
            else:
                in_tokens, out_tokens = 0, 0

        result = pricing.price_call(
            pricing_model or model_name,
            provider_name,
            in_tokens or 0,
            out_tokens or 0,
            estimated=estimated,
        )
        ledger = get_usage_ledger()
        row = ledger.record_call(
            model_name=model_name,
            provider_name=provider_name,
            input_tokens=in_tokens or 0,
            output_tokens=out_tokens or 0,
            cost_usd=result.cost_usd,
            is_local=result.is_local,
            pricing_status=result.status,
            session_id=ctx.get("session_id"),
            task_id=ctx.get("task_id"),
            agent_name=agent_name,
            task_class=ctx.get("task_class"),
        )

        task_id = row.get("task_id")
        if task_id:
            event_bus.publish(
                task_id,
                Event(
                    type=EventType.TASK_COST_UPDATE,
                    request_id=task_id,
                    payload={
                        "session_id": row.get("session_id"),
                        "task_id": task_id,
                        "delta_cost_usd": row["cost_usd"],
                        "task_total_cost_usd": ledger.task_total(task_id),
                        "input_tokens": row["input_tokens"],
                        "output_tokens": row["output_tokens"],
                        "model": row["model_name"],
                        "is_local": row["is_local"],
                        "pricing_status": row["pricing_status"],
                    },
                ),
            )
        return row
    except Exception:  # recording is observational; it must never break a model call.
        logger.exception("Failed to record provider call in usage ledger")
        return None
