"""Checkpoint path constants for LangGraph state persistence.

The actual checkpointer is created inline in executor.py using
AsyncSqliteSaver.from_conn_string(). This module provides the shared
default path constant.
"""
from pathlib import Path

# Default persistent checkpoint database (ADR 0011 §4).
DEFAULT_CHECKPOINT_DB = Path.home() / ".aether" / "checkpoints.db"
