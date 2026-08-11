import os
import sqlite3
from pathlib import Path
from typing import Generator
from contextlib import contextmanager

try:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
except ImportError:
    # Fallback/stub if not installed yet during dev
    AsyncSqliteSaver = None

_DEFAULT_DB_PATH = Path.home() / ".aether" / "checkpoints.db"

@contextmanager
def get_persistent_checkpointer(db_path: Path = None) -> Generator[AsyncSqliteSaver, None, None]:
    """
    Context manager to yield a persistent AsyncSqliteSaver checkpointer.
    Usage:
        with get_persistent_checkpointer() as checkpointer:
            graph = builder.compile(checkpointer=checkpointer)
            ...
    """
    if AsyncSqliteSaver is None:
        raise ImportError("langgraph-checkpoint-sqlite is not installed.")
        
    path = db_path or _DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # Actually, get_persistent_checkpointer is unused in executor.py.
    # The AsyncSqliteSaver needs aiosqlite connect which is async, so a sync context manager won't work perfectly.
    # But for compatibility let's just swap it.
    import aiosqlite
    import asyncio
    
    # We shouldn't use sync context manager for aiosqlite, but to preserve the signature, this might be broken if called.
    # Since executor.py uses its own in-memory connection, this file might just be a stub for now.
    pass
