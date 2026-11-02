"""Checkpoint path constants for LangGraph state persistence.

The actual checkpointer is created inline in executor.py using
AsyncSqliteSaver.from_conn_string(). This module provides the shared
default path constant.
"""
from pathlib import Path

# Default persistent checkpoint database (ADR 0011 §4).
DEFAULT_CHECKPOINT_DB = Path.home() / ".aether" / "checkpoints.db"


async def delete_thread_checkpoints(thread_id: str) -> None:
    """Delete every checkpoint for *thread_id*.

    Used when a session's history is truncated (edit-and-resend): the
    graph thread is keyed by session id, so without this the next run
    would silently keep reasoning over the deleted branch. The graph
    executor re-seeds a fresh thread from the session store's retained
    history, so no surviving context is lost.
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    DEFAULT_CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(DEFAULT_CHECKPOINT_DB)) as saver:
        await saver.setup()
        await saver.adelete_thread(thread_id)
