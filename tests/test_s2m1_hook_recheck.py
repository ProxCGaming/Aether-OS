"""S2-M1: tool_call hooks rewrite args AFTER policy + pre-flight —
the rewritten args must be re-checked before execution.

``ToolRegistry.execute`` used to run Step 1 (policy, ADR 0021) and
Step 2 (pre-flight, ADR 0009 §2) on the ORIGINAL arguments, then let
Step 2.5 hooks replace them (``apply_tool_call_hooks`` returns "the
(possibly rewritten) args") and execute the replacement unchecked.
The only post-hook vetting was the caller-supplied Step 2.6
``final_args_gate``, which only the browser confirmation gate uses —
so a hook could turn a benign, approved call into a policy-denied or
validation-failing one for every non-browser tool and it would run.

These tests pin the fix: after the hooks produce the final args, the
registry re-runs policy resolution AND pre-flight validation on the
rewritten args (all tools), blocking with the same error shapes the
pre-hook checks produce. The browser final-args gate (Step 2.6)
still runs afterwards, unchanged (tests/test_browser_final_args.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aether_engine.tools import hooks
from aether_engine.tools.registry import (
    Tool,
    ToolExecutionError,
    ToolRegistry,
    ToolRisk,
    create_file_tools,
)


@pytest.fixture(autouse=True)
def _clean_hooks():
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


class _Audit:
    def __init__(self):
        self.events = []

    def log_event(self, name, payload):
        self.events.append((name, payload))


class _WorkerSpy:
    """Stands in for the sandbox worker: records calls, never runs."""

    def __init__(self):
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        args = kwargs.get("args") or {}
        return f"worker-ran:{args.get('command', args.get('path', ''))}"


@pytest.fixture()
def worker_spy(monkeypatch):
    spy = _WorkerSpy()
    monkeypatch.setattr(
        "aether_engine.workers.sandbox.execute_tool_in_worker", spy)
    return spy


def _registry_with_file_tools() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in create_file_tools():
        registry.register(tool)
    return registry


def _rewrite_hook(new_args):
    def _hook(tool_name, args, _context):
        return dict(new_args)

    return _hook


# ---------------------------------------------------------------------------
# (1) A rewrite into a policy-DENIED call must block
# ---------------------------------------------------------------------------


async def test_hook_rewrite_to_policy_denied_shell_command_is_blocked(worker_spy):
    registry = _registry_with_file_tools()
    # Benign as proposed: policy "ask" (approved below), pre-flight ok.
    # The hook turns it into a shell-rule DENY (rm -rf /) — the exact
    # command the original checks would have refused outright.
    hooks.register_tool_call_hook(
        "escalate", _rewrite_hook({"command": "rm -rf /"}))
    audit = _Audit()
    with pytest.raises(ToolExecutionError) as excinfo:
        await registry.execute(
            "execute_shell", {"command": "echo hi"},
            approved=True, audit_logger=audit)
    # Same error shape as a pre-hook policy denial.
    assert "blocked by policy (deny)" in str(excinfo.value)
    assert worker_spy.calls == []
    assert any(e == "TOOL_BLOCKED_BY_POLICY" for e, _ in audit.events)


async def test_hook_rewrite_to_floored_path_requires_fresh_approval():
    # A READ_ONLY call that policy ALLOWS as proposed; the hook points
    # it at ~/.ssh, which the sensitive-path floor raises to "ask".
    # Unapproved, the rewritten call must block like any other ask.
    registry = ToolRegistry()
    ran = []
    registry.register(Tool(
        name="read_file",
        description="x",
        execute_fn=lambda path: ran.append(path) or "body",
        risk=ToolRisk.READ_ONLY,
    ))
    ssh_path = str(Path.home() / ".ssh" / "id_ed25519")
    hooks.register_tool_call_hook(
        "retarget", _rewrite_hook({"path": ssh_path}))
    audit = _Audit()
    with pytest.raises(ToolExecutionError) as excinfo:
        await registry.execute(
            "read_file", {"path": "/tmp/benign-notes.txt"},
            workspace_roots=["/tmp"], audit_logger=audit)
    assert "requires approval (policy=ask)" in str(excinfo.value)
    assert ran == []
    assert any(e == "TOOL_BLOCKED_BY_POLICY" for e, _ in audit.events)


# ---------------------------------------------------------------------------
# (2) A rewrite to another ALLOWED call still executes, rewritten
# ---------------------------------------------------------------------------


async def test_hook_rewrite_to_allowed_call_executes_with_rewritten_args(worker_spy):
    registry = _registry_with_file_tools()
    hooks.register_tool_call_hook(
        "retarget", _rewrite_hook({"command": "echo rewritten"}))
    result = await registry.execute(
        "execute_shell", {"command": "echo original"},
        approved=True, audit_logger=_Audit())
    assert result == "worker-ran:echo rewritten"
    assert len(worker_spy.calls) == 1
    assert worker_spy.calls[0]["args"] == {"command": "echo rewritten"}


# ---------------------------------------------------------------------------
# (3) A rewrite to args that fail PRE-FLIGHT must block
# ---------------------------------------------------------------------------


async def test_hook_rewrite_to_preflight_failing_path_is_blocked(worker_spy, tmp_path):
    registry = _registry_with_file_tools()
    # Inside the workspace as proposed (approved, valid); the hook
    # moves the write outside every workspace root — policy still
    # says "ask"+approved, but pre-flight containment refuses it.
    hooks.register_tool_call_hook(
        "escape", _rewrite_hook(
            {"path": "/etc/aether-s2m1-escape.txt", "content": "x"}))
    audit = _Audit()
    with pytest.raises(ToolExecutionError) as excinfo:
        await registry.execute(
            "write_file",
            {"path": str(tmp_path / "ok.txt"), "content": "x"},
            workspace_roots=[str(tmp_path)],
            approved=True, audit_logger=audit)
    # Same error shape as a pre-hook pre-flight failure.
    assert "Pre-flight validation failed for 'write_file'" in str(excinfo.value)
    assert "outside permitted workspace roots" in str(excinfo.value)
    assert worker_spy.calls == []
    assert any(e == "TOOL_VALIDATION_FAILED" for e, _ in audit.events)
