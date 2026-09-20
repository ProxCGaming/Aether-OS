import sqlite3
import uuid
import time
from typing import List, Dict, Any, Optional
from pathlib import Path

# Database path is ~/.aether/memory.db
DB_PATH = Path.home() / ".aether" / "memory.db"

def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db() -> None:
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
                timestamp INTEGER NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
        """)
        conn.commit()

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
        messages = [dict(row) for row in cur.fetchall()]
        
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

def add_message(session_id: str, role: str, content: str) -> None:
    init_db()
    msg_id = str(uuid.uuid4())
    now = int(time.time())
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO session_messages (id, session_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            (msg_id, session_id, role, content, now)
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
