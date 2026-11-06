"""Rewind manifest recording for graph runs (Epic 8, E8-1.1a).

Split out of ``langgraph/executor.py`` in the Wave 5 craft pass:
the manifest tracker — the per-run record of landed supersteps
(step -> checkpoint, label, files-written-so-far) that the rewind
picker reads — is one job with its own vocabulary, distinct from
run/resume orchestration. ``langgraph.executor`` re-exports every
name defined here; its namespace is the historical import and
patch surface (the suites read ``executor._new_manifest_tracker``
and ``executor._note_manifest_file_write`` directly).
"""

import asyncio
import logging
from typing import Any

from aether_engine.rewind import manifest as rewind_manifest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Epic 8, E8-1.1a: rewind manifest recording. As a graph task runs, each
# landed superstep appends a point (step -> checkpoint, label,
# files-written-so-far) to the session's manifest for THIS task. The
# tracker is a plain dict threaded through a run (and re-seeded from
# the manifest on resume):
#   {"step_index": int, "last_checkpoint_id": str, "files_written": [str],
#    "pending_write_events": [str]}
# "files_written" is the cumulative UNIQUE list (the picker's trail);
# "pending_write_events" buffers every landed write since the last
# recorded point — repeats included (N01) — and becomes that point's
# write events, which is what the not-reverted warning reads.
# Recording is best-effort by doctrine — like compaction and completion
# memory, a manifest failure is logged and never fails the task.
# ---------------------------------------------------------------------------

_WRITE_TOOL_NAMES = frozenset({"write_file", "edit_file"})


def _new_manifest_tracker(prior_checkpoint_id: str = "") -> dict[str, Any]:
    return {
        "step_index": 0,
        "last_checkpoint_id": prior_checkpoint_id,
        "files_written": [],
        "pending_write_events": [],
    }


def _note_manifest_file_write(
    tracker: dict[str, Any], msg: dict[str, Any], is_failed: bool
) -> None:
    """Track a completed file write from a tool message's args.

    The cumulative list keeps each path once; the event buffer keeps
    EVERY landed write (N01) — a repeat write to a listed path is
    invisible in the cumulative list but is exactly what the
    not-reverted warning needs to see.
    """
    if is_failed or msg.get("name") not in _WRITE_TOOL_NAMES:
        return
    args = msg.get("args") or {}
    path = args.get("path") or args.get("file_path")
    if not path:
        return
    tracker["pending_write_events"].append(path)
    if path not in tracker["files_written"]:
        tracker["files_written"].append(path)


async def _record_manifest_point(
    session_id: str,
    task_id: str,
    step_index: int,
    checkpoint_id: str,
    label: str,
    files_written: list[str],
    write_events: list[str] | None = None,
) -> None:
    try:
        await asyncio.to_thread(
            rewind_manifest.record_point,
            session_id, task_id, step_index, checkpoint_id, label,
            list(files_written), list(write_events or []),
        )
    except Exception:  # best-effort by doctrine (see block comment above)
        logger.debug("Rewind manifest recording failed", exc_info=True)


async def _advance_manifest_to_checkpoint(
    graph,
    config,
    tracker: dict[str, Any],
    *,
    session_id: str | None,
    task_id: str,
    label: str,
) -> None:
    """Append a point if the graph's checkpoint advanced past the
    tracker's last recorded one (one point per landed superstep)."""
    if not session_id:
        return
    try:
        snapshot = await graph.aget_state(config)
        checkpoint_id = (
            (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
            or ""
        )
    except Exception:  # noqa: BLE001 — best-effort by doctrine (see block comment above)
        return
    if not checkpoint_id or checkpoint_id == tracker["last_checkpoint_id"]:
        return
    tracker["last_checkpoint_id"] = checkpoint_id
    tracker["step_index"] += 1
    # The writes noted since the previous point become THIS point's
    # events (N01); the buffer then restarts for the next point.
    write_events = list(tracker["pending_write_events"])
    tracker["pending_write_events"] = []
    await _record_manifest_point(
        session_id, task_id, tracker["step_index"], checkpoint_id, label,
        tracker["files_written"], write_events,
    )
