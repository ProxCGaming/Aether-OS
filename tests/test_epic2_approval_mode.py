"""Epic 2, E2-08: approval mode "write".

The mode relaxes exactly one thing — the DEFAULT ask for WRITE-risk
tools. Everything explicit or protective still asks: per-tool Require
Approval, path-rule asks, the sensitive floor, EXECUTE/DESTRUCTIVE,
and shell ask-rules.
"""

from __future__ import annotations

from pathlib import Path

from aether_engine.config import UserConfig
from aether_engine.tools.policy import resolve_tool_policy
from aether_engine.tools.registry import Tool, ToolRisk

READ = Tool(name="read_file", description="", risk=ToolRisk.READ_ONLY)
WRITE = Tool(name="write_file", description="", risk=ToolRisk.WRITE)
DELETE = Tool(name="delete_file", description="", risk=ToolRisk.DESTRUCTIVE)
SHELL = Tool(name="execute_shell", description="", risk=ToolRisk.EXECUTE)


def test_default_mode_unchanged():
    for mode in ("ask",):
        assert resolve_tool_policy(WRITE, {}, approval_mode=mode) == "ask"
        assert resolve_tool_policy(READ, {}, approval_mode=mode) == "allow"


def test_write_mode_relaxes_only_write_defaults(tmp_path):
    args = {"path": str(tmp_path / "f.txt")}
    assert resolve_tool_policy(WRITE, {}, args=args, approval_mode="write") == "allow"
    assert resolve_tool_policy(DELETE, {}, args=args, approval_mode="write") == "ask"
    assert resolve_tool_policy(SHELL, {}, args={"command": "ls"}, approval_mode="write") == "ask"
    assert resolve_tool_policy(READ, {}, args=args, approval_mode="write") == "allow"


def test_write_mode_respects_explicit_and_protective_asks(tmp_path):
    args = {"path": str(tmp_path / "f.txt")}
    assert resolve_tool_policy(WRITE, {"write_file": "Require Approval"}, args=args, approval_mode="write") == "ask"
    assert resolve_tool_policy(WRITE, {"write_file": "Deny"}, args=args, approval_mode="write") == "deny"
    # Path-rule ask still asks.
    rules = [{"prefix": str(tmp_path), "verdict": "Require Approval"}]
    assert resolve_tool_policy(WRITE, {}, args=args, path_rules=rules, approval_mode="write") == "ask"
    # The sensitive floor still asks, even in write mode.
    ssh = {"path": str(Path.home() / ".ssh" / "config")}
    assert resolve_tool_policy(WRITE, {}, args=ssh, approval_mode="write") == "ask"
    # A shell ask-rule still forces approval for a (hypothetically) relaxed shell.
    assert resolve_tool_policy(SHELL, {}, args={"command": "rm -rf build"}, approval_mode="write") == "ask"


def test_config_round_trip_and_validation():
    cfg = UserConfig()
    cfg.approval_mode = "write"
    assert UserConfig.from_dict(cfg.to_dict()).approval_mode == "write"
    assert UserConfig.from_dict({"approval_mode": "yolo"}).approval_mode == "ask"
    assert UserConfig().approval_mode == "ask"
