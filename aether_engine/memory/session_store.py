import json
import logging
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from aether_engine.audit import AuditLogger
from aether_engine.config import UserConfig, load_config

logger = logging.getLogger("aether_engine.memory.session_store")
_audit_logger = AuditLogger()

# Database path is ~/.aether/memory.db
DB_PATH = Path.home() / ".aether" / "memory.db"

# Init guard holding the DB_PATH the tables were created in (False
# before the first init). Keying it to the path means a changed
# DB_PATH re-initialises instead of serving "no such table" errors
# from a stale flag — tests patch DB_PATH per case, and several test
# modules historically had to reset this flag by hand to avoid that.
_initialized = False

def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db() -> None:
    global _initialized
    if _initialized == DB_PATH:
        return
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                thoughts TEXT,
                timestamp INTEGER NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session_messages_session_id ON session_messages(session_id)")
        try:
            conn.execute("ALTER TABLE session_messages ADD COLUMN thoughts TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE session_messages ADD COLUMN attachments TEXT")
        except sqlite3.OperationalError:
            pass
        conn.commit()
    _initialized = DB_PATH

def list_sessions() -> list[dict[str, Any]]:
    init_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM sessions ORDER BY updated_at DESC")
        return [dict(row) for row in cur.fetchall()]

def get_session(session_id: str) -> dict[str, Any] | None:
    init_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        session_row = cur.fetchone()
        if not session_row:
            return None
        
        # rowid breaks timestamp ties: timestamps have 1-second
        # resolution, so same-second turns (a fast user/assistant pair)
        # would otherwise come back in whatever order SQLite pleases.
        cur.execute("SELECT * FROM session_messages WHERE session_id = ? ORDER BY timestamp ASC, rowid ASC", (session_id,))
        raw_messages = [dict(row) for row in cur.fetchall()]
        messages = []
        for m in raw_messages:
            th = m.get("thoughts")
            if th:
                if isinstance(th, str):
                    try:
                        m["thoughts"] = json.loads(th)
                    except (ValueError, TypeError):
                        m["thoughts"] = []
                elif isinstance(th, list):
                    m["thoughts"] = th
                else:
                    m["thoughts"] = [th]
            else:
                m["thoughts"] = []

            att = m.get("attachments")
            if att:
                if isinstance(att, str):
                    try:
                        m["attachments"] = json.loads(att)
                    except (ValueError, TypeError):
                        m["attachments"] = []
                elif isinstance(att, list):
                    m["attachments"] = att
                else:
                    m["attachments"] = [att]
            else:
                m["attachments"] = []
            messages.append(m)
        
        session = dict(session_row)
        session["messages"] = messages
        return session

def create_session(title: str = "New Chat") -> str:
    init_db()
    session_id = str(uuid.uuid4())
    now = int(time.time())
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (session_id, title, now, now)
        )
        conn.commit()
    return session_id

# Client-supplied message ids (edit-and-resend hardening): the UI
# generates the id for a user message and sends it with START_TASK, so
# the rendered message and the stored row share one identity and a
# later edit can address the exact row. Only plain id shapes are
# accepted; anything else falls back to a fresh UUID.
_CLIENT_MESSAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")


def add_message(
    session_id: str,
    role: str,
    content: str,
    thoughts: Any = None,
    message_id: str | None = None,
    attachments: Any = None,
) -> str:
    init_db()
    msg_id = message_id if (isinstance(message_id, str) and _CLIENT_MESSAGE_ID.match(message_id)) else str(uuid.uuid4())
    now = int(time.time())
    if thoughts is not None:
        if isinstance(thoughts, str):
            try:
                json.loads(thoughts)
                thoughts_json = thoughts
            except (ValueError, TypeError):
                thoughts_json = json.dumps(thoughts)
        else:
            thoughts_json = json.dumps(thoughts)
    else:
        thoughts_json = None

    if attachments is not None:
        if isinstance(attachments, str):
            try:
                json.loads(attachments)
                attachments_json = attachments
            except (ValueError, TypeError):
                attachments_json = json.dumps(attachments)
        else:
            attachments_json = json.dumps(attachments)
    else:
        attachments_json = None

    with _get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO session_messages (id, session_id, role, content, thoughts, attachments, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (msg_id, session_id, role, content, thoughts_json, attachments_json, now)
            )
        except sqlite3.IntegrityError:
            # A re-delivered packet can carry the same client id twice;
            # the message must still be stored, under a fresh UUID.
            msg_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO session_messages (id, session_id, role, content, thoughts, attachments, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (msg_id, session_id, role, content, thoughts_json, attachments_json, now)
            )
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (now, session_id)
        )
        conn.commit()
    return msg_id

def fork_session(source_session_id: str, cut_message_id: str | None, new_title: str) -> dict[str, Any] | None:
    """Copy a session and its messages up to a cut boundary (E8-1.2).

    The copy is a brand-new session: copied messages get fresh ids
    (message ids are globally unique), while role, content, thoughts
    and timestamps are preserved verbatim and inserted in stored
    order, so the copy renders exactly like the source prefix.
    ``cut_message_id`` is the last message copied (inclusive); None
    copies the whole session. The cut id is resolved inside the
    source session only — an id that lives in another session never
    cuts this one. Returns {"new_session_id", "title",
    "messages_copied"}, or None when the source session or the cut
    message cannot be resolved (nothing is created in that case).
    The source is only ever read.
    """
    init_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM sessions WHERE id = ?", (source_session_id,))
        source = cur.fetchone()
        if not source:
            return None
        cur.execute(
            "SELECT * FROM session_messages WHERE session_id = ? ORDER BY timestamp ASC, rowid ASC",
            (source_session_id,),
        )
        rows = [dict(row) for row in cur.fetchall()]
        if cut_message_id is not None:
            cut_index = next(
                (i for i, m in enumerate(rows) if m["id"] == cut_message_id), None)
            if cut_index is None:
                return None
            rows = rows[:cut_index + 1]
        new_id = str(uuid.uuid4())
        now = int(time.time())
        title = new_title if new_title else source["title"]
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (new_id, title, now, now)
        )
        for m in rows:
            conn.execute(
                "INSERT INTO session_messages (id, session_id, role, content, thoughts, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), new_id, m["role"], m["content"], m["thoughts"], m["timestamp"])
            )
        conn.commit()
    return {"new_session_id": new_id, "title": title, "messages_copied": len(rows)}


def delete_session(session_id: str) -> bool:
    init_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
        return cur.rowcount > 0

def update_session_title(session_id: str, title: str) -> bool:
    init_db()
    now = int(time.time())
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title, now, session_id)
        )
        conn.commit()
        return cur.rowcount > 0

def search_sessions(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Sessions whose title or message content matches ``query``.

    Each result carries a short snippet of the first matching message
    (empty when only the title matched). Case-insensitive substring
    match, most recently updated first.
    """
    init_db()
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT s.id, s.title, s.updated_at,
                   (SELECT substr(m.content, 1, 140) FROM session_messages m
                     WHERE m.session_id = s.id AND m.content LIKE ?
                     ORDER BY m.timestamp ASC, m.rowid ASC LIMIT 1) AS snippet
            FROM sessions s
            WHERE s.title LIKE ?
               OR EXISTS (SELECT 1 FROM session_messages m2
                          WHERE m2.session_id = s.id AND m2.content LIKE ?)
            ORDER BY s.updated_at DESC
            LIMIT ?
            """,
            (like, like, like, int(limit)),
        )
        return [
            {"id": r[0], "title": r[1], "updated_at": r[2], "snippet": r[3] or ""}
            for r in cur.fetchall()
        ]


def truncate_session(session_id: str, message_id: str) -> bool:
    init_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        # The rowid subquery is scoped to this session: an id that only
        # exists in another session must never truncate this one.
        cur.execute(
            "DELETE FROM session_messages WHERE session_id = ? AND rowid >= (SELECT rowid FROM session_messages WHERE id = ? AND session_id = ?)",
            (session_id, message_id, session_id)
        )
        conn.commit()
        return cur.rowcount > 0

def truncate_session_by_content(session_id: str, role: str, content: str, occurrence: int | None = None) -> bool:
    """Truncate at a message identified by role + content.

    Fallback for messages whose UI id is not a stored row id. With
    *occurrence* (0-based, counted in stored order) the exact duplicate
    the user clicked is the cut point — the legacy behaviour without it
    picks the most recent match, which silently desyncs the UI from
    the store when the same text was sent more than once.
    """
    init_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        if occurrence is None:
            cur.execute(
                "SELECT rowid FROM session_messages WHERE session_id = ? AND role = ? AND content = ? ORDER BY timestamp DESC, rowid DESC LIMIT 1",
                (session_id, role, content)
            )
            row = cur.fetchone()
            if not row:
                return False
            cut_rowid = row["rowid"]
        else:
            cur.execute(
                "SELECT rowid FROM session_messages WHERE session_id = ? AND role = ? AND content = ? ORDER BY rowid ASC",
                (session_id, role, content)
            )
            rows = cur.fetchall()
            if occurrence < 0 or occurrence >= len(rows):
                return False
            cut_rowid = rows[occurrence]["rowid"]
        cur.execute(
            "DELETE FROM session_messages WHERE session_id = ? AND rowid >= ?",
            (session_id, cut_rowid)
        )
        conn.commit()
        return cur.rowcount > 0


def get_chat_history(session_id: str, current_prompt: str | None = None, *, limit: int = 20, max_chars: int = 24_000) -> list[dict[str, str]]:
    """Model-facing history for a session: user/assistant turns only.

    Shared by the fast-chat runner and the graph executor (which seeds
    a fresh checkpoint thread from it, e.g. after an edit invalidated
    the old thread). A trailing user message identical to
    *current_prompt* is the just-saved current turn and is excluded —
    callers append the prompt themselves. Bounded by message count and
    total characters, most recent turns kept.
    """
    sess = get_session(session_id)
    if not sess:
        return []
    history: list[dict[str, str]] = []
    for msg in sess.get("messages", []):
        role = msg.get("role")
        content = (msg.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        history.append({"role": role, "content": content})
    if current_prompt:
        target = current_prompt.strip()
        if history and history[-1]["role"] == "user" and history[-1]["content"] == target:
            history.pop()
    history = history[-limit:]
    total = sum(len(m["content"]) for m in history)
    while total > max_chars and len(history) > 1:
        total -= len(history.pop(0)["content"])
    return history

# ---------------------------------------------------------------------------
# Phase 10.3 A1: retention / pruning. Before this, sessions had no pruning,
# decay, or TTL — sessions + session_messages grew without bound. Bounds
# come from UserConfig (config.py is the single source of the numbers);
# explicit args here override them, and a bound <= 0 disables pruning on
# that axis.
#
# Safety: nothing references a session from another tier (KG facts point
# at task_ids, not session ids), so a session is pruned as a unit — its
# messages go with it via the ON DELETE CASCADE foreign key, never left
# orphaned. Age is judged on updated_at (last activity), not created_at,
# so a long-lived but active chat is not pruned.
# ---------------------------------------------------------------------------

def prune_sessions(retention_days: int | None = None,
                   max_sessions: int | None = None,
                   *, config: UserConfig | None = None,
                   now: int | None = None) -> dict[str, int]:
    """Prune sessions by age (on updated_at), then by count (oldest first).

    Returns counts and logs them to the audit log (SESSIONS_PRUNED).
    """
    cfg = config if config is not None else load_config()
    if retention_days is None:
        retention_days = cfg.session_retention_days
    if max_sessions is None:
        max_sessions = cfg.session_max_sessions
    now = int(time.time()) if now is None else int(now)

    init_db()
    pruned_sessions = 0
    pruned_messages = 0

    with _get_conn() as conn:
        cur = conn.cursor()
        if retention_days and retention_days > 0:
            cutoff = now - int(retention_days) * 86400
            pruned_messages += cur.execute(
                "SELECT COUNT(*) FROM session_messages WHERE session_id IN "
                "(SELECT id FROM sessions WHERE updated_at < ?)",
                (cutoff,),
            ).fetchone()[0]
            cur.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
            pruned_sessions += cur.rowcount

        if max_sessions and max_sessions > 0:
            total = cur.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            excess = total - int(max_sessions)
            if excess > 0:
                rows = cur.execute(
                    "SELECT id FROM sessions ORDER BY updated_at ASC LIMIT ?",
                    (excess,),
                ).fetchall()
                ids = [r["id"] for r in rows]
                placeholders = ",".join("?" for _ in ids)
                pruned_messages += cur.execute(
                    "SELECT COUNT(*) FROM session_messages WHERE session_id IN "
                    f"({placeholders})",
                    ids,
                ).fetchone()[0]
                cur.executemany("DELETE FROM sessions WHERE id = ?", [(i,) for i in ids])
                pruned_sessions += cur.rowcount
        conn.commit()

    result = {
        "pruned_sessions": pruned_sessions,
        "pruned_messages": pruned_messages,
        "retention_days": retention_days,
        "max_sessions": max_sessions,
    }
    _audit_logger.log_event("SESSIONS_PRUNED", result)
    logger.info("SESSIONS_PRUNED %s", result)
    return result
