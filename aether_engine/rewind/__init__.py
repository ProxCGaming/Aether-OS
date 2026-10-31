"""Epic 8, Item 1: rewind / fork / edit-on-approval support.

E8-1.1a ships the per-task rewind manifest (see manifest.py);
E8-1.1b adds rewind execution (see service.py); E8-1.2 adds fork
from anywhere (see fork.py).
"""
from aether_engine.rewind.fork import fork_session_at
from aether_engine.rewind.manifest import (
    get_entries,
    get_latest_task_id,
    get_manifest,
    record_point,
)
from aether_engine.rewind.service import (
    commit_staged,
    has_staged,
    stage_rewind,
    undo_staged,
)

__all__ = [
    "commit_staged",
    "fork_session_at",
    "get_entries",
    "get_latest_task_id",
    "get_manifest",
    "has_staged",
    "record_point",
    "stage_rewind",
    "undo_staged",
]
