"""SPEC-6A (option A) — the startup orphan sweep.

When the engine process dies mid-task (crash, kill, host reboot),
the task's audit trail ends at RUNNING / EXECUTING_WORKER and
nothing ever closes it: the task ledger derives its state from
that trail, so the Dashboard showed the task as Running forever
(FF6-01). At startup — before any new task can run — this sweep
closes those records honestly: each orphaned task gets a
RUNNING -> INTERRUPTED audit transition, plus a companion
TASK_INTERRUPTED_BY_RESTART event whose details carry the
readable coverage note (the audit log shape-sanitises transition
payloads by design, so the note cannot ride in the payload).

Exclusions, per the owner's D-1 ruling:

- A task whose checkpoint still carries a pending approval
  interrupt is NOT swept. It is paused, not orphaned — it can
  still be answered and resumed after the restart, so its record
  must stay exactly as it was.
- Tasks already in a terminal state, and tasks merely awaiting
  approval (PENDING_APPROVAL), are untouched.

The sweep is idempotent: a swept task's last transition is
INTERRUPTED (terminal), so a second startup finds nothing to do.
It writes only audit transitions — checkpoints are read, never
modified — and it is best-effort at the startup boundary: a
failure is logged by the caller and never blocks engine start.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from aether_common.contracts import TaskState

logger = logging.getLogger("aether_engine.langgraph.orphan_sweep")

# Audit states that mean "executing when last heard from". A task
# whose last transition lands anywhere else is not an orphan.
_ORPHAN_STATES = {TaskState.RUNNING.value, TaskState.EXECUTING_WORKER.value}


def last_transitions_by_task(log_path: Path) -> dict[str, dict[str, Any]]:
    """The last TASK_TRANSITION record per task in the audit log."""
    last: dict[str, dict[str, Any]] = {}
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return last
    for line in lines:
        line = line.strip()
        if not line or '"TASK_TRANSITION"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") != "TASK_TRANSITION" or not record.get("task_id"):
            continue
        last[str(record["task_id"])] = record
    return last


async def sweep_orphaned_tasks(
    *,
    audit_logger: Any,
    audit_log_path: Path | str | None = None,
    checkpoint_db: Path | str | None = None,
) -> dict[str, list[str]]:
    """Mark every orphaned task INTERRUPTED in the audit trail.

    Returns {"swept": [...], "skipped_pending_approval": [...]}.
    ``audit_logger`` is the engine's AuditLogger (its
    log_task_transition is the one writer of TASK_TRANSITION
    records, so the sweep writes through it, never around it).
    """
    from aether_engine.audit import DEFAULT_AUDIT_LOG_PATH
    from aether_engine.task_ledger import checkpoint_slice_for_task

    log_path = Path(audit_log_path) if audit_log_path else DEFAULT_AUDIT_LOG_PATH
    last = await asyncio.to_thread(last_transitions_by_task, log_path)
    swept: list[str] = []
    skipped: list[str] = []
    for task_id, record in last.items():
        to_state = record.get("to_state")
        if to_state not in _ORPHAN_STATES:
            continue
        # Paused-for-approval is not orphaned (D-1): the checkpoint
        # still holds the pending ask, and the task can resume.
        slice_, _capped = await checkpoint_slice_for_task(
            task_id, checkpoint_db=checkpoint_db)
        approval_check = "no_checkpoint_found"
        if slice_ is not None:
            approval_check = "checked"
            if slice_.get("pending_approvals"):
                skipped.append(task_id)
                continue
        audit_logger.log_task_transition(
            task_id,
            TaskState(to_state),
            TaskState.INTERRUPTED,
            payload={
                "reason": "engine_restart",
                "source": "startup_sweep",
                "approval_check": approval_check,
            },
        )
        audit_logger.log_event(
            "TASK_INTERRUPTED_BY_RESTART",
            {
                "task_id": task_id,
                "reason": "engine_restart",
                "source": "startup_sweep",
                "approval_check": approval_check,
                "note": (
                    "Interrupted by engine restart: the process ended "
                    "while this task was executing. Coverage: the audit "
                    "trail was scanned to its end; the checkpoint "
                    f"approval check was {approval_check}."),
            },
        )
        swept.append(task_id)
    if swept:
        logger.info(
            "Startup orphan sweep: %d task(s) marked INTERRUPTED (%s)",
            len(swept), ", ".join(swept))
    if skipped:
        logger.info(
            "Startup orphan sweep: %d task(s) left untouched — a "
            "pending approval is still open (%s)",
            len(skipped), ", ".join(skipped))
    return {"swept": swept, "skipped_pending_approval": skipped}
