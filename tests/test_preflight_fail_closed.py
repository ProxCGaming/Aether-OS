"""F-03: Phase-9 pre-flight must fail CLOSED on internal errors.

Programme acceptance: "test injecting a preflight internal error —
call is denied, error is logged/shown."

Recon (verified against the code in this tree):

* ``ToolRegistry.execute`` Step 2 called ``validate_tool_request``
  with no try/except. An internal error there propagated out of
  ``execute()`` as a raw, unwrapped exception (e.g. ``RuntimeError``):
  the tool did not run (so execution was *incidentally* denied), but
  there was no ``TOOL_VALIDATION_FAILED`` audit event and no
  ``ToolExecutionError`` — the clean denial surface every other
  pipeline refusal uses, and the type the graph node and direct
  callers are written to expect.
* The shell-rules check is evaluated TWICE on the way to execution:
  first inside the Step-1 policy resolution
  (``tools/policy.py: resolve_tool_policy``) and again inside
  pre-flight (``validation/pre_flight.py``). An internal error in
  ``evaluate_shell_command`` therefore escaped raw from Step 1,
  before Step 2 was ever reached.
* Nowhere in ``validation/pre_flight.py``, ``validation/shell_rules.py``
  or ``tools/policy.py`` is an exception swallowed into an
  "allow"/"valid" verdict — ``_is_path_in_roots`` already fails closed
  by returning ``False``. The gap was purely the unwrapped escape.

These tests pin the hardened contract: an internal error anywhere in
the policy / pre-flight validation path is a denial —
``ToolExecutionError`` mentioning pre-flight validation, a
``TOOL_VALIDATION_FAILED`` audit event, and the tool (in-process
``execute_fn`` AND the sandbox worker) never invoked.
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aether_engine.audit import AuditLogger
from aether_engine.tools.registry import (
    Tool,
    ToolExecutionError,
    ToolRegistry,
    ToolRisk,
)
from aether_engine.validation.shell_rules import ShellVerdict


def _audit_events(log_path):
    log = Path(log_path)
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _validation_failures(log_path):
    return [e for e in _audit_events(log_path) if e.get("action") == "TOOL_VALIDATION_FAILED"]


def _write_registry(calls):
    registry = ToolRegistry()
    registry.register(Tool(
        name="write_file",
        description="x",
        execute_fn=lambda path, content="": calls.append(("execute_fn", path)) or "wrote",
        risk=ToolRisk.WRITE,
    ))
    return registry


def _shell_registry(calls):
    registry = ToolRegistry()
    registry.register(Tool(
        name="execute_shell",
        description="x",
        execute_fn=lambda command: calls.append(("execute_fn", command)) or "ran",
        risk=ToolRisk.EXECUTE,
    ))
    return registry


def test_preflight_internal_error_denies_write_tool(tmp_path):
    """(a) validate_tool_request raising internally -> denied, logged,
    tool never invoked (neither execute_fn nor the sandbox worker)."""
    calls = []
    registry = _write_registry(calls)
    audit = AuditLogger(log_path=tmp_path / "audit.log")
    target = tmp_path / "out.txt"

    # registry.execute imports validate_tool_request from
    # aether_engine.validation.pre_flight at call time, so patching
    # that module attribute is patching the function the registry runs.
    with patch(
        "aether_engine.validation.pre_flight.validate_tool_request",
        side_effect=RuntimeError("boom: preflight internal error"),
    ), patch(
        "aether_engine.workers.sandbox.execute_tool_in_worker",
        new=AsyncMock(side_effect=AssertionError("worker must not be invoked")),
    ) as worker, pytest.raises(ToolExecutionError) as excinfo:
        asyncio.run(registry.execute(
            "write_file",
            {"path": str(target), "content": "x"},
            approved=True,  # policy passes; validation is the failing step
            workspace_roots=[str(tmp_path)],
            audit_logger=audit,
        ))

    assert "Pre-flight validation failed" in str(excinfo.value)
    assert "boom" in str(excinfo.value)
    assert calls == [], "execute_fn must never be invoked when pre-flight errors"
    worker.assert_not_awaited()
    assert not target.exists()
    failures = _validation_failures(tmp_path / "audit.log")
    assert failures, "internal pre-flight error must be logged as TOOL_VALIDATION_FAILED"
    assert "boom" in json.dumps(failures[0])


def test_shell_rules_internal_error_denies_execute_shell(tmp_path):
    """(b) evaluate_shell_command raising internally -> execute_shell
    must not result in execution.

    The shell check runs first inside Step-1 policy resolution, so
    this also pins that a policy-step internal error in the validation
    path is wrapped into the same clean denial, not a raw escape."""
    calls = []
    registry = _shell_registry(calls)
    audit = AuditLogger(log_path=tmp_path / "audit.log")

    with patch(
        "aether_engine.validation.shell_rules.evaluate_shell_command",
        side_effect=RuntimeError("boom: shell rules internal error"),
    ), patch(
        "aether_engine.workers.sandbox.execute_tool_in_worker",
        new=AsyncMock(side_effect=AssertionError("worker must not be invoked")),
    ) as worker, pytest.raises(ToolExecutionError) as excinfo:
        asyncio.run(registry.execute(
            "execute_shell",
            {"command": "echo hi"},
            approved=True,
            workspace_roots=[str(tmp_path)],
            audit_logger=audit,
        ))

    assert "Pre-flight validation failed" in str(excinfo.value)
    assert "boom" in str(excinfo.value)
    assert calls == [], "execute_fn must never be invoked when shell validation errors"
    worker.assert_not_awaited()
    failures = _validation_failures(tmp_path / "audit.log")
    assert failures, "internal shell-rules error must be logged as TOOL_VALIDATION_FAILED"
    assert "boom" in json.dumps(failures[0])


def test_shell_rules_error_inside_preflight_denies_execute_shell(tmp_path):
    """(b, Step-2 isolation) The policy-step shell check succeeds, then
    the SAME check raising inside pre-flight validation (Step 2) must
    still deny — this isolates the Step-2 wrap from the Step-1 wrap."""
    calls = []
    registry = _shell_registry(calls)
    audit = AuditLogger(log_path=tmp_path / "audit.log")
    seen = {"count": 0}

    def flaky_evaluate(command, **kwargs):
        seen["count"] += 1
        if seen["count"] == 1:
            return ShellVerdict("allow")  # Step-1 policy check passes
        raise RuntimeError("boom: preflight shell check internal error")

    with patch(
        "aether_engine.validation.shell_rules.evaluate_shell_command",
        side_effect=flaky_evaluate,
    ), patch(
        "aether_engine.workers.sandbox.execute_tool_in_worker",
        new=AsyncMock(side_effect=AssertionError("worker must not be invoked")),
    ) as worker, pytest.raises(ToolExecutionError) as excinfo:
        asyncio.run(registry.execute(
            "execute_shell",
            {"command": "echo hi"},
            approved=True,
            workspace_roots=[str(tmp_path)],
            audit_logger=audit,
        ))

    assert seen["count"] >= 2, "test must actually reach the pre-flight shell check"
    assert "Pre-flight validation failed" in str(excinfo.value)
    assert "boom" in str(excinfo.value)
    assert calls == [], "execute_fn must never be invoked when pre-flight shell validation errors"
    worker.assert_not_awaited()
    failures = _validation_failures(tmp_path / "audit.log")
    assert failures, "internal pre-flight error must be logged as TOOL_VALIDATION_FAILED"
    assert "boom" in json.dumps(failures[0])
