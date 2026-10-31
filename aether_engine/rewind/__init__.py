"""Epic 8, Item 1: rewind / fork / edit-on-approval support.

E8-1.1a ships the per-task rewind manifest (see manifest.py). Rewind
execution lands in E8-1.1b, fork in E8-1.2.
"""
from aether_engine.rewind.manifest import (
    get_entries,
    get_latest_task_id,
    get_manifest,
    record_point,
)

__all__ = [
    "get_entries",
    "get_latest_task_id",
    "get_manifest",
    "record_point",
]
