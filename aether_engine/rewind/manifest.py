"""Per-task rewind manifest storage (Epic 8, E8-1.1a).

As a task runs, the engine records its rewind points here — step ->
checkpoint, a human label, and the files written so far (grill Q5).
Rewind consults this manifest and never guesses from raw checkpoints.

Each point also carries its WRITE EVENTS (N01): the paths written
since the previous point, repeats included. The cumulative
``files_written`` list cannot see a repeat write to an already-listed
path, but the not-reverted warning must — a file rewritten after the
rewind point keeps its new content, so it belongs on the warning
even when it was also written before the point.

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
                write_events_json TEXT NOT NULL DEFAULT '[]',
                timestamp INTEGER NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
        """)
        # Migration for databases created before write events (N01)
        # existed — the same try/except ALTER pattern session_store
        # uses. Pre-events rows read back with no events; their
        # warnings keep the accuracy the cumulative lists give them.
        try:
            conn.execute(
                "ALTER TABLE rewind_manifest "
                "ADD COLUMN write_events_json TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass
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
    write_events: list[str] | None = None,
) -> None:
    """Append one rewind point for (session, task).

    ``write_events`` are the paths written since the previous point
    (repeats included, N01); callers that track no events (fast chat
    writes no files) omit it and the point records none.
    """
    init_rewind_db()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO rewind_manifest "
            "(id, session_id, task_id, step_index, checkpoint_id, label, "
            "file_writes_json, write_events_json, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                session_id,
                task_id,
                int(step_index),
                checkpoint_id or "",
                label,
                json.dumps(list(files_written)),
                json.dumps(list(write_events or [])),
                int(time.time()),
            ),
        )
        conn.commit()


def _parse_path_list(raw: Any) -> list[str]:
    try:
        parsed = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _row_to_entry(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "step_index": row["step_index"],
        "checkpoint_id": row["checkpoint_id"],
        "label": row["label"],
        "files_written": _parse_path_list(row["file_writes_json"]),
        "write_events": _parse_path_list(row["write_events_json"]),
        "timestamp": row["timestamp"],
    }


def _cut_message_ids(
    session_id: str, entries: list[dict[str, Any]],
) -> list[str | None]:
    """The message each point's retained history ends at (inclusive).

    Mirrors the cut the rewind/fork services apply, computed from
    the session's stored messages — the authoritative record: the
    task begins at the session's last user row, so step 0 ends at
    the message before it (None when the task is the session's only
    content), an intermediate point ends at the task's opening
    message (its response belongs to later steps), and the final
    point ends at the session's last message. Anchors are None when
    the transcript cannot anchor the task (no messages / no user
    row) — unknown, never invented.
    """
    if not entries:
        return []
    init_rewind_db()
    with _get_conn() as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "session_messages" not in tables:
            return [None] * len(entries)
        rows = conn.execute(
            "SELECT id, role FROM session_messages WHERE session_id = ? "
            "ORDER BY rowid ASC",
            (session_id,),
        ).fetchall()
    if not rows:
        return [None] * len(entries)
    start_pos = None
    for pos, row in enumerate(rows):
        if row["role"] == "user":
            start_pos = pos
    if start_pos is None:
        return [None] * len(entries)
    final_step = entries[-1]["step_index"]
    cuts: list[str | None] = []
    for entry in entries:
        step = entry["step_index"]
        if step == 0:
            cuts.append(rows[start_pos - 1]["id"] if start_pos > 0 else None)
        elif step < final_step:
            cuts.append(rows[start_pos]["id"])
        else:
            cuts.append(rows[-1]["id"])
    return cuts


def get_entries(session_id: str, task_id: str) -> list[dict[str, Any]]:
    """All recorded points for one task, in step order.

    Each entry also carries ``cut_message_id``: the id of the
    message the point's retained history ends at (see
    :func:`_cut_message_ids`), so step pickers can name the actual
    manifest boundaries instead of guessing from message positions.
    """
    init_rewind_db()
    with _get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM rewind_manifest WHERE session_id = ? AND task_id = ? "
            "ORDER BY step_index ASC, rowid ASC",
            (session_id, task_id),
        )
        entries = [_row_to_entry(row) for row in cur.fetchall()]
    for entry, cut in zip(entries, _cut_message_ids(session_id, entries)):
        entry["cut_message_id"] = cut
    return entries


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
