import os
import sqlite3
from pathlib import Path
from typing import Generator
from contextlib import contextmanager

try:
    from langgraph.checkpoint.sqlite import SqliteSaver
except ImportError:
    # Fallback/stub if not installed yet during dev
    SqliteSaver = None

_DEFAULT_DB_PATH = Path.home() / ".aether" / "checkpoints.db"

@contextmanager
def get_persistent_checkpointer(db_path: Path = None) -> Generator[SqliteSaver, None, None]:
    """
    Context manager to yield a persistent SqliteSaver checkpointer.
    Usage:
        with get_persistent_checkpointer() as checkpointer:
            graph = builder.compile(checkpointer=checkpointer)
            ...
    """
    if SqliteSaver is None:
        raise ImportError("langgraph-checkpoint-sqlite is not installed.")
        
    path = db_path or _DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    
    # langgraph.checkpoint.sqlite.SqliteSaver can be initialized with a sqlite3 connection
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    
    try:
        checkpointer = SqliteSaver(conn)
        yield checkpointer
    finally:
        conn.close()
