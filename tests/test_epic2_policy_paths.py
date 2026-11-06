"""Epic 2, E2-06: arg-aware policy + per-path permissions.

The resolver's contract: per-path rules refine the name-level verdict
for a specific call; explicit per-tool Deny / Require Approval always
win; the sensitive-path floor raises any allow to ask; and with no
args or no rules, behaviour is exactly the pre-E2-06 name-level logic.
"""

from __future__ import annotations

from pathlib import Path

from aether_engine.config import UserConfig
from aether_engine.tools.policy import resolve_policy_by_name, resolve_tool_policy
from aether_engine.tools.registry import Tool, ToolRisk

WRITE_TOOL = Tool(name="write_file", description="", risk=ToolRisk.WRITE)
READ_TOOL = Tool(name="read_file", description="", risk=ToolRisk.READ_ONLY)


def _projects_rule(tmp_path) -> list[dict]:
    return [{"prefix": str(tmp_path / "projects"), "verdict": "Always Allow"}]


def test_no_args_behaves_like_legacy_name_policy():
    assert resolve_tool_policy(WRITE_TOOL, {}) == "ask"
    assert resolve_tool_policy(READ_TOOL, {}) == "allow"
    assert resolve_tool_policy(WRITE_TOOL, {"write_file": "Deny"}) == "deny"
    assert resolve_tool_policy(READ_TOOL, {"read_file": "Require Approval"}) == "ask"


def test_path_rule_relaxes_default_ask_inside_prefix(tmp_path):
    args = {"path": str(tmp_path / "projects" / "app" / "main.py")}
    assert resolve_tool_policy(WRITE_TOOL, {}, args=args, path_rules=_projects_rule(tmp_path)) == "allow"
    outside = {"path": str(tmp_path / "elsewhere" / "main.py")}
    assert resolve_tool_policy(WRITE_TOOL, {}, args=outside, path_rules=_projects_rule(tmp_path)) == "ask"


def test_path_rule_deny_and_tool_scoping(tmp_path):
    rules = [{"prefix": str(tmp_path), "verdict": "Deny", "tools": ["write_file"]}]
    args = {"path": str(tmp_path / "f.txt")}
    assert resolve_tool_policy(WRITE_TOOL, {}, args=args, path_rules=rules) == "deny"
    # A rule scoped to write_file does not touch read_file.
    assert resolve_tool_policy(READ_TOOL, {}, args=args, path_rules=rules) == "allow"


def test_explicit_tool_settings_beat_path_allow(tmp_path):
    rules = _projects_rule(tmp_path)
    args = {"path": str(tmp_path / "projects" / "f.txt")}
    assert resolve_tool_policy(WRITE_TOOL, {"write_file": "Deny"}, args=args, path_rules=rules) == "deny"
    assert resolve_tool_policy(WRITE_TOOL, {"write_file": "Require Approval"}, args=args, path_rules=rules) == "ask"


def test_sensitive_floor_raises_allow_to_ask(tmp_path):
    ssh = Path.home() / ".ssh" / "id_ed25519"
    assert resolve_tool_policy(READ_TOOL, {}, args={"path": str(ssh)}) == "ask"
    aether = Path.home() / ".aether" / "config.json"
    assert resolve_tool_policy(READ_TOOL, {}, args={"path": str(aether)}) == "ask"
    # Even an explicit path-rule allow cannot punch through the floor.
    rules = [{"prefix": str(Path.home() / ".ssh"), "verdict": "Always Allow"}]
    assert resolve_tool_policy(READ_TOOL, {}, args={"path": str(ssh)}, path_rules=rules) == "ask"
    # Ordinary workspace reads are untouched by the floor.
    assert resolve_tool_policy(READ_TOOL, {}, args={"path": str(tmp_path / "ok.txt")}) == "allow"


def test_system_prefix_floors_writes_not_reads(tmp_path):
    etc = {"path": "/etc/hostname"}
    assert resolve_tool_policy(WRITE_TOOL, {"write_file": "Always Allow"}, args=etc, path_rules=[
        {"prefix": "/etc", "verdict": "Always Allow"},
    ]) == "ask"
    assert resolve_tool_policy(READ_TOOL, {}, args=etc) == "allow"


def test_resolve_by_name_threads_args(tmp_path):
    from aether_engine.tools.registry import build_tool_registry

    registry = build_tool_registry()
    args = {"path": str(tmp_path / "projects" / "f.txt")}
    verdict = resolve_policy_by_name(
        "write_file", {}, registry, args=args, path_rules=_projects_rule(tmp_path)
    )
    assert verdict == "allow"


SHELL_TOOL = Tool(name="execute_shell", description="", risk=ToolRisk.EXECUTE)


def test_path_allow_does_not_cover_shell_mentions(tmp_path):
    """E3-N02: a path "Always Allow" must not auto-allow a shell
    command that merely *mentions* an in-prefix path. The tokens
    harvested from a command string are mentions, not the call's
    targets — the command's effect is its whole text."""
    rules = _projects_rule(tmp_path)
    proj = tmp_path / "projects"
    # The audit's verified case: sole path token inside the prefix.
    assert resolve_tool_policy(SHELL_TOOL, {}, args={"command": f"cat {proj}/x.txt"}, path_rules=rules) == "ask"
    # In-prefix path as the script a shell is told to execute.
    assert resolve_tool_policy(SHELL_TOOL, {}, args={"command": f"sh {proj}/install.sh"}, path_rules=rules) == "ask"
    # Mention in-prefix, effect outside the prefix.
    outside = tmp_path / "elsewhere"
    assert resolve_tool_policy(SHELL_TOOL, {}, args={"command": f"echo {proj}/note > {outside}/loot.txt"}, path_rules=rules) == "ask"


def test_path_deny_and_ask_still_apply_to_shell(tmp_path):
    """Deny / Require Approval path rules keep judging shell commands
    by the full token harvest — only the allow direction is scoped."""
    proj = tmp_path / "projects"
    args = {"command": f"cat {proj}/x.txt"}
    deny_rules = [{"prefix": str(tmp_path), "verdict": "Deny"}]
    assert resolve_tool_policy(SHELL_TOOL, {}, args=args, path_rules=deny_rules) == "deny"
    ask_rules = [{"prefix": str(tmp_path), "verdict": "Require Approval"}]
    assert resolve_tool_policy(SHELL_TOOL, {}, args=args, path_rules=ask_rules) == "ask"


def test_path_allow_needs_every_declared_target_inside_prefix(tmp_path):
    """Allow matches the grant semantics of approval_cache.covering_
    grant: EVERY declared target must sit inside the prefix — one
    in-prefix source no longer launders an out-of-prefix destination."""
    rules = _projects_rule(tmp_path)
    proj = tmp_path / "projects"
    both_in = {"path": str(proj / "a.txt"), "destination": str(proj / "b.txt")}
    assert resolve_tool_policy(WRITE_TOOL, {}, args=both_in, path_rules=rules) == "allow"
    one_out = {"path": str(proj / "a.txt"), "destination": str(tmp_path / "elsewhere" / "b.txt")}
    assert resolve_tool_policy(WRITE_TOOL, {}, args=one_out, path_rules=rules) == "ask"


def test_path_allow_still_covers_execute_tool_with_declared_path(tmp_path):
    """An EXECUTE-risk tool whose target is a declared path argument
    (the run_checks shape) keeps file-tool semantics: the path arg IS
    the target, not a mention inside command text."""
    checks = Tool(name="run_checks", description="", risk=ToolRisk.EXECUTE)
    args = {"path": str(tmp_path / "projects")}
    assert resolve_tool_policy(checks, {}, args=args, path_rules=_projects_rule(tmp_path)) == "allow"


def test_config_round_trip_keeps_path_rules(tmp_path):
    cfg = UserConfig()
    cfg.path_rules = _projects_rule(tmp_path)
    restored = UserConfig.from_dict(cfg.to_dict())
    assert restored.path_rules == cfg.path_rules


def test_project_layer_cannot_set_path_rules(tmp_path):
    from aether_engine.config_layers import effective_user_config

    layer_dir = tmp_path / "proj"
    (layer_dir / ".aether").mkdir(parents=True)
    (layer_dir / ".aether" / "config.json").write_text(
        '{"path_rules": [{"prefix": "/", "verdict": "Always Allow"}]}', encoding="utf-8"
    )
    effective = effective_user_config(UserConfig(), str(layer_dir))
    assert effective.path_rules == []
