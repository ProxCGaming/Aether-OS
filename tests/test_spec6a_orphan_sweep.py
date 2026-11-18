"""SPEC-6A option A — the startup orphan sweep.

A task whose audit trail ends at RUNNING / EXECUTING_WORKER when
the engine restarts is orphaned: the ledger derives its state
from that trail and would show it Running forever (FF6-01). The
sweep appends a -> INTERRUPTED transition through the audit
logger, with a coverage note. Tasks with a pending approval in
their checkpoint are paused, not orphaned, and stay untouched
(D-1); terminal and awaiting-approval tasks are untouched too.
"""

from __future__ import annotations

import json

import pytest

from aether_common.contracts import (
    TERMINAL_STATES,
    TaskState,
    validate_transition,
)
from aether_engine.audit import AuditLogger
from aether_engine.langgraph import orphan_sweep
from aether_engine.langgraph.orphan_sweep import (
    last_transitions_by_task,
    sweep_orphaned_tasks,
)
from aether_engine.task_ledger import read_audit_records


def _seed_log(path, transitions):
    """Write TASK_TRANSITION records as the executor would."""
    lines = []
    for task_id, from_state, to_state in transitions:
        lines.append(json.dumps({
            "type": "TASK_TRANSITION", "ts": 1700000000.0,
            "task_id": task_id, "from_state": from_state,
            "to_state": to_state, "payload": {},
        }))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def audit(tmp_path):
    log = tmp_path / "audit.log"
    logger = AuditLogger(log_path=log)
    return logger, log


def test_interrupted_is_a_legal_terminal_state():
    assert TaskState.INTERRUPTED in TERMINAL_STATES
    validate_transition(TaskState.RUNNING, TaskState.INTERRUPTED)
    validate_transition(TaskState.EXECUTING_WORKER, TaskState.INTERRUPTED)


async def test_running_and_executing_tasks_are_swept(audit, tmp_path):
    logger, log = audit
    _seed_log(log, [
        ("task-a", "PENDING", "RUNNING"),
        ("task-b", "RUNNING", "EXECUTING_WORKER"),
        ("task-c", "RUNNING", "SUCCEEDED"),
    ])
    result = await sweep_orphaned_tasks(
        audit_logger=logger, audit_log_path=log,
        checkpoint_db=tmp_path / "no-checkpoints.db")
    assert sorted(result["swept"]) == ["task-a", "task-b"]
    assert result["skipped_pending_approval"] == []
    for task_id in ("task-a", "task-b"):
        transitions = read_audit_records(task_id, log_path=log)["transitions"]
        assert transitions[-1]["to_state"] == "INTERRUPTED"
    # The finished task's trail is unchanged.
    transitions = read_audit_records("task-c", log_path=log)["transitions"]
    assert transitions[-1]["to_state"] == "SUCCEEDED"


async def test_sweep_is_idempotent(audit, tmp_path):
    logger, log = audit
    _seed_log(log, [("task-a", "PENDING", "RUNNING")])
    first = await sweep_orphaned_tasks(
        audit_logger=logger, audit_log_path=log,
        checkpoint_db=tmp_path / "no-checkpoints.db")
    second = await sweep_orphaned_tasks(
        audit_logger=logger, audit_log_path=log,
        checkpoint_db=tmp_path / "no-checkpoints.db")
    assert first["swept"] == ["task-a"]
    assert second["swept"] == []


async def test_awaiting_approval_state_is_not_a_candidate(audit, tmp_path):
    logger, log = audit
    _seed_log(log, [
        ("task-p", "RUNNING", "PENDING_APPROVAL"),
        ("task-q", "PENDING", "PENDING"),
    ])
    result = await sweep_orphaned_tasks(
        audit_logger=logger, audit_log_path=log,
        checkpoint_db=tmp_path / "no-checkpoints.db")
    assert result["swept"] == []


async def test_pending_approval_checkpoint_is_skipped(audit, tmp_path, monkeypatch):
    logger, log = audit
    _seed_log(log, [("task-p", "PENDING", "RUNNING")])

    async def fake_slice(task_id, *, session_id=None, checkpoint_db=None):
        return {"pending_approvals": [{"tool_name": "shell", "pending": True}]}, False

    monkeypatch.setattr(
        "aether_engine.task_ledger.checkpoint_slice_for_task", fake_slice)
    # The sweep imports the function from the module at call time,
    # so also patch the reference it resolves.
    monkeypatch.setattr(orphan_sweep, "checkpoint_slice_for_task", fake_slice, raising=False)
    result = await sweep_orphaned_tasks(
        audit_logger=logger, audit_log_path=log,
        checkpoint_db=tmp_path / "checkpoints.db")
    assert result["swept"] == []
    assert result["skipped_pending_approval"] == ["task-p"]
    transitions = read_audit_records("task-p", log_path=log)["transitions"]
    assert transitions[-1]["to_state"] == "RUNNING"  # untouched


async def test_swept_transition_carries_the_coverage_note(audit, tmp_path):
    logger, log = audit
    _seed_log(log, [("task-a", "PENDING", "RUNNING")])
    await sweep_orphaned_tasks(
        audit_logger=logger, audit_log_path=log,
        checkpoint_db=tmp_path / "no-checkpoints.db")
    record = last_transitions_by_task(log)["task-a"]
    assert record["to_state"] == "INTERRUPTED"
    # The audit log shape-sanitises transition payloads by design;
    # the readable coverage rides in the companion event's details.
    assert record["payload"]["keys"] == [
        "approval_check", "reason", "source"]
    events = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
        if '"TASK_INTERRUPTED_BY_RESTART"' in line]
    assert len(events) == 1
    details = events[0]["details"]
    assert details["task_id"] == "task-a"
    assert details["reason"] == "engine_restart"
    assert details["approval_check"] == "no_checkpoint_found"
    assert "Interrupted by engine restart" in details["note"]


async def test_missing_log_sweeps_nothing(audit, tmp_path):
    logger, _log = audit
    result = await sweep_orphaned_tasks(
        audit_logger=logger, audit_log_path=tmp_path / "absent.log",
        checkpoint_db=tmp_path / "no-checkpoints.db")
    assert result == {"swept": [], "skipped_pending_approval": []}
