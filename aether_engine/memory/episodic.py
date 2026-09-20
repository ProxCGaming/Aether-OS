import sqlite3
import time
import uuid
from typing import List, Dict, Any, Optional
from pathlib import Path
from aether_engine.audit import AuditLogger

_audit_logger = AuditLogger()

# Database path is ~/.aether/memory.db
DB_PATH = Path.home() / ".aether" / "memory.db"

def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_episodic_db() -> None:
    with _get_conn() as conn:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS episodic_memory USING fts5(
                task_id UNINDEXED,
                timestamp UNINDEXED,
                user_prompt,
                task_summary,
                tags,
                outcome,
                tokenize = 'porter unicode61'
            )
        """)
        conn.commit()

def store_episode(task_id: str, user_prompt: str, task_summary: str, tags: List[str], outcome: str) -> None:
    init_episodic_db()
    now = int(time.time())
    tags_str = ",".join(tags)
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO episodic_memory (task_id, timestamp, user_prompt, task_summary, tags, outcome)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, now, user_prompt, task_summary, tags_str, outcome)
        )
        conn.commit()
    
    _audit_logger.log_event("EPISODIC_MEMORY_STORED", {
        "task_id": task_id,
        "tags": tags
    })

def query_episodes(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    init_episodic_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        
        # Sanitize query by removing quotes and special FTS characters
        clean_query = query.replace('"', ' ').replace("'", " ").replace(":", " ")
        
        if not clean_query.strip():
            return []
            
        try:
            # We use OR matching for terms or wrap in quotes for phrase match.
            # Here we wrap the whole thing in quotes for exact phrase, or we can just let it match terms.
            # Let's just let it match terms by providing the clean string (which acts as AND).
            # If we want it to be more lenient, we can split and join with OR. Let's just use the clean string.
            cur.execute(
                """SELECT *, rank 
                   FROM episodic_memory 
                   WHERE episodic_memory MATCH ? 
                   ORDER BY rank LIMIT ?""",
                (clean_query, limit)
            )
            results = [dict(row) for row in cur.fetchall()]
            _audit_logger.log_event("EPISODIC_MEMORY_RETRIEVED", {
                "query": query,
                "results_count": len(results)
            })
            return results
        except sqlite3.OperationalError:
            # Fallback if syntax error in MATCH
            return []

def get_all_episodes(limit: int = 50) -> List[Dict[str, Any]]:
    init_episodic_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM episodic_memory ORDER BY timestamp DESC LIMIT ?", (limit,))
        return [dict(row) for row in cur.fetchall()]
