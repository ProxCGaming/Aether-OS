"""Phase 10.2, C1: the hardened ToolRegistry.execute() path is dead; the
live path duplicates it.

Before the fix, ``execute_tool_node`` re-implemented the trust pipeline
inline (policy check, ``validate_tool_request``, direct
``execute_tool_in_worker`` call with NO audit logger), so the audited
``ToolRegistry.execute()`` pipeline had no production caller and the live
path produced no worker audit events. These tests fail on the pre-fix code
and pass after:

* ``test_policy_denied_tool_denied_through_node`` — a deny-policy tool is
  denied through the LangGraph node, and the ``TOOL_BLOCKED_BY_POLICY``
  audit event comes from the registry pipeline (``via`` =
  ``"registry.execute"``). Pre-fix the node logged the block itself with
  no ``via`` marker, so the event-shape assertion fails.
* ``test_node_calls_registry_execute`` — the node actually awaits
  ``ToolRegistry.execute``. Pre-fix it never called it at all.
* ``test_risky_tool_through_node_emits_worker_audit_events`` — a risky
  tool executed through the node runs the real worker end-to-end and the
  audit log contains ``WORKER_EXECUTION_COMPLETED``. Pre-fix the node
  called ``execute_tool_in_worker`` without an audit logger, so the file
  was written but no worker event was ever recorded.

Regression guards (pass before and after, pin the preserved behaviour):
a validation failure through the node still returns the node's
``"Validation failed: ..."`` message and writes nothing; a READ_ONLY
tool still runs in-process through the node.
"""
import json
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aether_engine.audit import AuditLogger
from aether_engine.config import UserConfig
from aether_engine.langgraph.nodes.execute_tool import execute_tool_node
from aether_engine.tools.registry import (
    Tool,
    ToolRegistry,
    ToolRisk,
    create_current_time_tool,
    create_file_tools,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token-phase10-2-c1"


def _audit_events(log_path):
    log = Path(log_path)
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


@contextmanager
def _engine_call_env(tmp_path, job_cls):
    """chdir to tmp_path (token file resolves relative to cwd), point
    PYTHONPATH at the repo for the worker child, and fake the Windows
    Job Object (same pattern as tests/test_phase10_2_worker_timeout.py).
    Restores everything on exit."""
    token_dir = tmp_path / ".run"
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "engine.token").write_text(TOKEN, encoding="utf-8")
    audit = AuditLogger(log_path=tmp_path / "logs" / "audit.log")

    old_cwd = os.getcwd()
    old_pythonpath = os.environ.get("PYTHONPATH")
    try:
        os.chdir(tmp_path)
        os.environ["PYTHONPATH"] = str(REPO_ROOT)
        with patch("aether_engine.workers.sandbox.WorkerJobObject", job_cls):
            yield audit
    finally:
        os.chdir(old_cwd)
        if old_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old_pythonpath


class FakeJobObject:
    """Job Object stand-in: construction + assignment succeed (no-op)."""

    def __init__(self, *args, **kwargs):
        pass

    def assign_process(self, pid):
        pass


@pytest.mark.asyncio
async def test_policy_denied_tool_denied_through_node(tmp_path):
    """A policy-denied tool is denied through the LangGraph node (not only
    through a direct execute() call), the tool never runs, and the block
    audit event comes from the single registry pipeline."""
    executed = []
    registry = ToolRegistry()
    registry.register(Tool(
        name="write_file",
        description="x",
        execute_fn=lambda path, content="": executed.append(path) or "x",
        risk=ToolRisk.WRITE,
    ))
    audit = AuditLogger(log_path=tmp_path / "logs" / "audit.log")
    state = {"pending_tool_call": {"name": "write_file", "call_id": "1",
                                   "args": {"path": "a.txt", "content": "x"}},
             "active_specialist": "coder"}
    config = {"configurable": {
        "tool_registry": registry,
        "user_config": UserConfig(tool_policies={"write_file": "Deny"}),
        "audit_logger": audit,
    }}

    result = await execute_tool_node(state, config)

    assert "blocked by user policy" in result["messages"][0]["content"]
    assert result["pending_tool_call"] is None
    assert executed == [], "denied tool must not execute"
    events = _audit_events(tmp_path / "logs" / "audit.log")
    blocked = [e for e in events if e.get("action") == "TOOL_BLOCKED_BY_POLICY"]
    assert blocked, f"expected TOOL_BLOCKED_BY_POLICY event, got {events}"
    assert blocked[0]["details"].get("via") == "registry.execute", (
        f"block must come from the single registry pipeline, got {blocked[0]['details']}"
    )


@pytest.mark.asyncio
async def test_node_calls_registry_execute(tmp_path):
    """The node dispatches through ToolRegistry.execute — one pipeline,
    no inline duplication."""
    registry = ToolRegistry()
    registry.register(create_current_time_tool())
    state = {"pending_tool_call": {"name": "get_current_time", "call_id": "2", "args": {}}}
    config = {"configurable": {"tool_registry": registry, "user_config": UserConfig()}}

    with patch.object(ToolRegistry, "execute", AsyncMock(wraps=registry.execute)) as mock_execute:
        result = await execute_tool_node(state, config)

    assert mock_execute.await_count == 1, "node must call ToolRegistry.execute exactly once"
    call_kwargs = mock_execute.await_args.kwargs
    assert call_kwargs.get("approved") is False
    assert result["messages"][0]["content"], "node must return the tool result"


@pytest.mark.asyncio
async def test_risky_tool_through_node_emits_worker_audit_events(tmp_path):
    """A risky tool executed through the node runs the real worker and
    appears in the audit log with WORKER_EXECUTION_COMPLETED."""
    registry = ToolRegistry()
    for tool in create_file_tools():
        registry.register(tool)
    target = tmp_path / "node-wrote.txt"
    state = {"pending_tool_call": {"name": "write_file", "call_id": "3",
                                   "args": {"path": str(target), "content": "via-node"},
                                   "approved": True}}
    with _engine_call_env(tmp_path, FakeJobObject) as audit:
        config = {"configurable": {
            "tool_registry": registry,
            "user_config": UserConfig(),
            "workspace_roots": [str(tmp_path)],
            "task_class": "instant",
            "audit_logger": audit,
        }}
        result = await execute_tool_node(state, config)

    assert target.read_text(encoding="utf-8") == "via-node"
    assert "node-wrote.txt" in result["messages"][0]["content"]
    events = _audit_events(tmp_path / "logs" / "audit.log")
    completed = [e for e in events if e.get("action") == "WORKER_EXECUTION_COMPLETED"]
    assert completed, f"expected WORKER_EXECUTION_COMPLETED event, got {events}"
    assert completed[0]["details"]["tool"] == "write_file"


@pytest.mark.asyncio
async def test_validation_failure_through_node(tmp_path):
    """Pre-flight failure through the node keeps the node's wording and
    writes nothing (regression guard for the error translation)."""
    registry = ToolRegistry()
    for tool in create_file_tools():
        registry.register(tool)
    outside = tmp_path / ".." / "outside-workspace-aether.txt"
    state = {"pending_tool_call": {"name": "write_file", "call_id": "4",
                                   "args": {"path": str(outside), "content": "x"},
                                   "approved": True}}
    config = {"configurable": {
        "tool_registry": registry,
        "user_config": UserConfig(),
        "workspace_roots": [str(tmp_path)],
    }}

    result = await execute_tool_node(state, config)

    assert "Validation failed" in result["messages"][0]["content"]
    assert not outside.exists(), "failed validation must not write"


@pytest.mark.asyncio
async def test_read_only_tool_runs_in_process_through_node():
    """A READ_ONLY tool still runs in-process through the node (no worker
    hop) and returns its result."""
    registry = ToolRegistry()
    registry.register(create_current_time_tool())
    state = {"pending_tool_call": {"name": "get_current_time", "call_id": "5", "args": {}}}
    config = {"configurable": {"tool_registry": registry, "user_config": UserConfig()}}

    result = await execute_tool_node(state, config)

    assert result["pending_tool_call"] is None
    content = result["messages"][0]["content"]
    assert content and "Error" not in content
