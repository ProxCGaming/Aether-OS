import sqlite3
import uuid
import time
import json
import logging
from typing import List, Dict, Any, Optional
from pathlib import Path
from aether_engine.audit import AuditLogger
from aether_engine.config import UserConfig, load_config

logger = logging.getLogger("aether_engine.memory.session_store")
_audit_logger = AuditLogger()

# Database path is ~/.aether/memory.db
DB_PATH = Path.home() / ".aether" / "memory.db"

_initialized = False

def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db() -> None:
    global _initialized
    if _initialized:
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
        conn.commit()
    _initialized = True

def list_sessions() -> List[Dict[str, Any]]:
    init_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM sessions ORDER BY updated_at DESC")
        return [dict(row) for row in cur.fetchall()]

def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    init_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        session_row = cur.fetchone()
        if not session_row:
            return None
        
        cur.execute("SELECT * FROM session_messages WHERE session_id = ? ORDER BY timestamp ASC", (session_id,))
        raw_messages = [dict(row) for row in cur.fetchall()]
        messages = []
        for m in raw_messages:
            th = m.get("thoughts")
            if th:
                if isinstance(th, str):
                    try:
                        m["thoughts"] = json.loads(th)
                    except Exception:
                        m["thoughts"] = []
                elif isinstance(th, list):
                    m["thoughts"] = th
                else:
                    m["thoughts"] = [th]
            else:
                m["thoughts"] = []
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

def add_message(session_id: str, role: str, content: str, thoughts: Optional[Any] = None) -> None:
    init_db()
    msg_id = str(uuid.uuid4())
    now = int(time.time())
    if thoughts is not None:
        if isinstance(thoughts, str):
            try:
                json.loads(thoughts)
                thoughts_json = thoughts
            except Exception:
                thoughts_json = json.dumps(thoughts)
        else:
            thoughts_json = json.dumps(thoughts)
    else:
        thoughts_json = None
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO session_messages (id, session_id, role, content, thoughts, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, session_id, role, content, thoughts_json, now)
        )
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (now, session_id)
        )
        conn.commit()

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

def truncate_session(session_id: str, message_id: str) -> bool:
    init_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM session_messages WHERE session_id = ? AND rowid >= (SELECT rowid FROM session_messages WHERE id = ?)",
            (session_id, message_id)
        )
        conn.commit()
        return cur.rowcount > 0

def truncate_session_by_content(session_id: str, role: str, content: str) -> bool:
    init_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        # Find the most recent message matching this content and role
        cur.execute(
            "SELECT id FROM session_messages WHERE session_id = ? AND role = ? AND content = ? ORDER BY timestamp DESC, rowid DESC LIMIT 1", 
            (session_id, role, content)
        )
        row = cur.fetchone()
        if not row:
            return False
        msg_id = row["id"]
        cur.execute(
            "DELETE FROM session_messages WHERE session_id = ? AND rowid >= (SELECT rowid FROM session_messages WHERE id = ?)",
            (session_id, msg_id)
        )
        conn.commit()
        return cur.rowcount > 0

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

def prune_sessions(retention_days: Optional[int] = None,
                   max_sessions: Optional[int] = None,
                   *, config: Optional[UserConfig] = None,
                   now: Optional[int] = None) -> Dict[str, int]:
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
