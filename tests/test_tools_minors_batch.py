"""Tool-layer minors batch — E0-02, E2-F10, E2-F11, E2-F12, E2-F13, E2-F14.

Each section pins one finding from the full-product review at its real
seam, in the audit's own repro shape:

- E0-02: a successful MCP execution must leave the same TOOL_EXECUTED
  audit record a native execution leaves (the MCP branch used to return
  through the result hooks before the shared audit tail).
- E2-F10: an anchored edit (atomic_write_bytes) must not strip an
  existing file's permission bits.
- E2-F11: run_checks' Jest parsing must report TEST counts, not the
  suite counts Jest prints first.
- E2-F12: Brave search must URL-encode the query (`C++ & C# examples`
  used to arrive as q="C   ").
- E2-F13: a shell-rule denial's audit event must name the matched rule.
- E2-F14: run_checks command configuration: callers can override the
  detected commands (keyed by check kind) through both call sites.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from urllib.parse import parse_qs, urlparse

import pytest

from aether_common.checks import detect_checks, parse_result
from aether_common.text_edit import atomic_write_bytes


class _RecordingAudit:
    """Audit-logger stub: records (action, details) verbatim."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def log_event(self, action, details):
        self.events.append((action, details))

    def of(self, action):
        return [details for a, details in self.events if a == action]


@pytest.fixture(autouse=True)
def _clean_hooks():
    from aether_engine.tools import hooks

    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


# ---------------------------------------------------------------------------
# E0-02 — successful MCP executions emit TOOL_EXECUTED exactly once
# ---------------------------------------------------------------------------


def _mcp_tool():
    from aether_engine.tools.registry import Tool, ToolRisk

    return Tool(
        name="fake__add",
        description="synthetic MCP tool",
        input_schema={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        },
        execute_fn=lambda a, b: str(a + b),
        risk=ToolRisk.EXECUTE,
        source="mcp:fake",
    )


def test_mcp_success_emits_tool_executed_exactly_once():
    from aether_engine.tools.registry import ToolRegistry

    audit = _RecordingAudit()
    registry = ToolRegistry()
    registry.register(_mcp_tool())
    result = asyncio.run(
        registry.execute("fake__add", {"a": 3, "b": 5}, approved=True, audit_logger=audit)
    )
    assert result == "8"
    executed = audit.of("TOOL_EXECUTED")
    assert len(executed) == 1, f"expected one TOOL_EXECUTED event, got: {audit.events}"
    # Same payload shape the native path's shared tail writes:
    # argument NAMES only, never values.
    assert executed[0]["tool_name"] == "fake__add"
    assert executed[0]["risk"] == "execute"
    assert executed[0]["arg_keys"] == ["a", "b"]


def test_mcp_result_hooks_still_apply_exactly_once():
    from aether_engine.tools import hooks
    from aether_engine.tools.registry import ToolRegistry

    hooks.register_tool_result_hook("tag", lambda t, a, r, c: r + " [tagged]")
    audit = _RecordingAudit()
    registry = ToolRegistry()
    registry.register(_mcp_tool())
    result = asyncio.run(
        registry.execute("fake__add", {"a": 1, "b": 2}, approved=True, audit_logger=audit)
    )
    assert result == "3 [tagged]"


# ---------------------------------------------------------------------------
# E2-F10 — atomic_write_bytes preserves an existing file's mode
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_atomic_write_preserves_executable_bit(tmp_path):
    target = tmp_path / "script.sh"
    target.write_bytes(b"#!/bin/sh\necho old\n")
    os.chmod(target, 0o755)
    atomic_write_bytes(str(target), b"#!/bin/sh\necho new\n")
    assert target.read_bytes() == b"#!/bin/sh\necho new\n"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o755


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_atomic_write_preserves_restrictive_mode_and_new_file_still_writes(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_bytes(b"old")
    os.chmod(target, 0o640)
    atomic_write_bytes(str(target), b"new")
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o640

    fresh = tmp_path / "fresh.txt"
    atomic_write_bytes(str(fresh), b"data")
    assert fresh.read_bytes() == b"data"


# ---------------------------------------------------------------------------
# E2-F11 — Jest parsing reports test counts, not suite counts
# ---------------------------------------------------------------------------

_JEST_MIXED = """\
PASS src/alpha.test.js
FAIL src/beta.test.js
PASS src/gamma.test.js
Test Suites: 2 failed, 3 passed, 5 total
Tests:       7 failed, 93 passed, 100 total
Snapshots:   0 total
"""


def test_jest_counts_are_test_counts_not_suite_counts():
    parsed = parse_result("jest", _JEST_MIXED, 1)
    assert parsed["passed"] == 93
    assert parsed["failed"] == 7
    assert parsed["ok"] is False
    # Suite totals remain available under their own keys.
    assert parsed["suites_passed"] == 3
    assert parsed["suites_failed"] == 2


def test_jest_all_passing_counts():
    parsed = parse_result(
        "jest",
        "Test Suites: 4 passed, 4 total\nTests:       25 passed, 25 total\n",
        0,
    )
    assert parsed["passed"] == 25 and parsed["failed"] == 0 and parsed["ok"] is True
    assert parsed["suites_passed"] == 4 and parsed["suites_failed"] == 0


# ---------------------------------------------------------------------------
# E2-F12 — Brave search URL-encodes the query
# ---------------------------------------------------------------------------


def test_brave_query_is_url_encoded(monkeypatch):
    from aether_engine.tools.web_search import search_web_dispatch

    monkeypatch.setenv("BRAVE_API_KEY", "test-brave-key")
    captured = {}

    def transport(method, url, headers, json_body):
        captured["url"] = url
        return {"web": {"results": []}}

    search_web_dispatch("C++ & C# examples", provider="brave", transport=transport)
    params = parse_qs(urlparse(captured["url"]).query)
    assert params["q"] == ["C++ & C# examples"]
    assert params["count"] == ["5"]


# ---------------------------------------------------------------------------
# E2-F13 — shell-rule denials name the matched rule in the audit trail
# ---------------------------------------------------------------------------


def _shell_registry():
    from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="execute_shell",
            description="synthetic shell tool",
            execute_fn=lambda command: "ran",
            risk=ToolRisk.EXECUTE,
        )
    )
    return registry


def test_shell_rule_denial_audit_names_the_matched_rule():
    from aether_engine.tools.registry import ToolExecutionError

    audit = _RecordingAudit()
    registry = _shell_registry()
    with pytest.raises(ToolExecutionError):
        asyncio.run(
            registry.execute("execute_shell", {"command": "rm -rf /"}, audit_logger=audit)
        )
    blocked = audit.of("TOOL_BLOCKED_BY_POLICY")
    assert len(blocked) == 1
    assert blocked[0].get("rule") == "rm-root"


def test_config_denial_audit_has_no_rule_attribution():
    from aether_engine.tools.registry import ToolExecutionError

    audit = _RecordingAudit()
    registry = _shell_registry()
    with pytest.raises(ToolExecutionError):
        asyncio.run(
            registry.execute(
                "execute_shell",
                {"command": "ls"},
                policies={"execute_shell": "Deny"},
                audit_logger=audit,
            )
        )
    blocked = audit.of("TOOL_BLOCKED_BY_POLICY")
    assert len(blocked) == 1
    assert "rule" not in blocked[0]


def test_shell_denial_rule_helper():
    from aether_engine.tools.policy import shell_denial_rule
    from aether_engine.tools.registry import Tool, ToolRisk

    shell = Tool(name="execute_shell", description="", risk=ToolRisk.EXECUTE)
    assert shell_denial_rule(shell, {"command": "rm -rf /"}) == "rm-root"
    assert shell_denial_rule(shell, {"command": "ls -la"}) is None
    other = Tool(name="read_file", description="", risk=ToolRisk.READ_ONLY)
    assert shell_denial_rule(other, {"command": "rm -rf /"}) is None


# ---------------------------------------------------------------------------
# E2-F14 — run_checks command-configuration override
# ---------------------------------------------------------------------------


@pytest.fixture()
def checks_repo(tmp_path):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    return root


_PYTEST_STUB = [sys.executable, "-c", "print('7 passed in 0.10s')"]


def test_detect_checks_command_override(checks_repo):
    default = {c["kind"]: c["argv"] for c in detect_checks(checks_repo)}
    overridden = {
        c["kind"]: c["argv"]
        for c in detect_checks(checks_repo, commands={"pytest": _PYTEST_STUB})
    }
    assert overridden["pytest"] == _PYTEST_STUB
    assert overridden["ruff"] == default["ruff"]
    # No override (or a malformed one) leaves every default untouched.
    assert {c["kind"]: c["argv"] for c in detect_checks(checks_repo)} == default
    malformed = {
        c["kind"]: c["argv"]
        for c in detect_checks(checks_repo, commands={"pytest": "not-a-list"})
    }
    assert malformed == default


def test_run_checks_locally_threads_command_override(checks_repo):
    from aether_engine.tools.codebase import run_checks_locally

    summary = run_checks_locally(checks_repo, commands={"pytest": _PYTEST_STUB})
    assert "7 passed" in summary


@pytest.mark.asyncio
async def test_worker_run_checks_threads_command_override(checks_repo):
    from aether_worker.__main__ import run_worker

    out = await run_worker(
        {
            "tool_name": "run_checks",
            "args": {"path": str(checks_repo), "commands": {"pytest": _PYTEST_STUB}},
        }
    )
    assert "7 passed" in out["summary"]


def test_run_checks_schema_exposes_commands():
    from aether_engine.tools.codebase import codebase_tools

    tool = next(t for t in codebase_tools() if t.name == "run_checks")
    assert "commands" in tool.input_schema["properties"]
