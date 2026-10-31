"""Per-task rewind manifest storage (Epic 8, E8-1.1a).

As a task runs, the engine records its rewind points here — step ->
checkpoint, a human label, and the files written so far (grill Q5).
Rewind consults this manifest and never guesses from raw checkpoints.

Semantics (grill Q2/Q5):
  * Points exist for a session's LAST task only. Older tasks' rows
    stay in the table (a session delete cascades them away), but
    ``get_manifest`` serves only the most recently recorded task and
    answers older task ids with no entries.
  * Graph tasks record superstep points whose checkpoint ids are real
    LangGraph checkpoints (threads keyed by session id). Fast-chat
    tasks record message-level points; they have no graph checkpoint,
    so their checkpoint_id is "" — recorded honestly, never invented.
  * Step 0 of every task is the "Before this task began" point.

The table lives in the session store's memory.db so the foreign key
cascades manifest rows away when a session is deleted, and so tests
that patch ``session_store.DB_PATH`` isolate the manifest too.
Recording is best-effort by doctrine: callers (the task runners) log
and continue on failure — a manifest gap must never fail a task.
"""
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from aether_engine.memory import session_store

logger = logging.getLogger("aether_engine.rewind.manifest")

# Init guard holding the DB path the table was created in (False
# before the first init) — the same path-keyed pattern session_store
# uses, so a changed DB path re-initialises instead of serving
# "no such table" errors from a stale flag.
_initialized = False


def _db_path() -> Path:
    return Path(session_store.DB_PATH)


def _get_conn() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_rewind_db() -> None:
    global _initialized
    path = _db_path()
    if _initialized == path:
        return
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rewind_manifest (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                checkpoint_id TEXT NOT NULL,
                label TEXT NOT NULL,
                file_writes_json TEXT NOT NULL DEFAULT '[]',
                timestamp INTEGER NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rewind_session_task "
            "ON rewind_manifest(session_id, task_id)")
        conn.commit()
    _initialized = path


def record_point(
    session_id: str,
    task_id: str,
    step_index: int,
    checkpoint_id: str,
    label: str,
    files_written: list[str],
) -> None:
    """Append one rewind point for (session, task)."""
    init_rewind_db()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO rewind_manifest "
            "(id, session_id, task_id, step_index, checkpoint_id, label, "
            "file_writes_json, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                session_id,
                task_id,
                int(step_index),
                checkpoint_id or "",
                label,
                json.dumps(list(files_written)),
                int(time.time()),
            ),
        )
        conn.commit()


def _row_to_entry(row: sqlite3.Row) -> dict[str, Any]:
    try:
        files = json.loads(row["file_writes_json"] or "[]")
    except (ValueError, TypeError):
        files = []
    return {
        "step_index": row["step_index"],
        "checkpoint_id": row["checkpoint_id"],
        "label": row["label"],
        "files_written": files if isinstance(files, list) else [],
        "timestamp": row["timestamp"],
    }


def get_entries(session_id: str, task_id: str) -> list[dict[str, Any]]:
    """All recorded points for one task, in step order."""
    init_rewind_db()
    with _get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM rewind_manifest WHERE session_id = ? AND task_id = ? "
            "ORDER BY step_index ASC, rowid ASC",
            (session_id, task_id),
        )
        return [_row_to_entry(row) for row in cur.fetchall()]


def get_latest_task_id(session_id: str) -> str | None:
    """The session's most recently recorded task (its rewindable one).

    Ordered by rowid — timestamps have one-second resolution, so two
    tasks starting in the same second would otherwise be ambiguous.
    """
    init_rewind_db()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT task_id FROM rewind_manifest WHERE session_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    return row["task_id"] if row else None


def get_manifest(session_id: str, task_id: str | None = None) -> dict[str, Any]:
    """The REWIND_MANIFEST_GET payload for a session.

    With no task_id, serves the latest task's points. An explicit
    task_id that is not the latest is answered with that id and NO
    entries — rewind points exist only for the last task (Q5), and
    the empty answer is how older steps learn to show the hint.
    """
    latest = get_latest_task_id(session_id)
    if latest is None:
        return {"session_id": session_id, "task_id": None, "entries": []}
    target = task_id or latest
    if target != latest:
        return {"session_id": session_id, "task_id": target, "entries": []}
    return {
        "session_id": session_id,
        "task_id": latest,
        "entries": get_entries(session_id, latest),
    }
