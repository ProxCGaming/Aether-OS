from pathlib import Path

from aether_engine.tools.registry import Tool, ToolRisk

# known supervisor memory / safe built-in tools that might be evaluated without full registry
KNOWN_NON_REGISTRY_TOOLS: dict[str, ToolRisk] = {
    "get_current_chat_history": ToolRisk.READ_ONLY,
    "search_knowledge_graph": ToolRisk.READ_ONLY,
    "search_past_episodes": ToolRisk.READ_ONLY,
    "get_current_time": ToolRisk.READ_ONLY,
}

_VERDICT_MAP = {"Always Allow": "allow", "Require Approval": "ask", "Deny": "deny"}


def _floor_prefixes() -> list[Path]:
    """Sensitive prefixes whose calls can never be auto-allowed (E2-06).

    Two tiers, applied in :func:`_apply_floor`: the home-sensitive
    prefixes (SSH keys, Aether's own data dir with its secret store)
    floor EVERY tool at "ask" — reading a private key into a model
    context is an exfiltration risk, not a convenience. System prefixes
    floor only tools that can change or execute things.
    """
    home = Path.home()
    return [home / ".ssh", home / ".aether"]


def _system_prefixes() -> list[Path]:
    return [Path(p) for p in ("/etc", "/usr", "/bin", "/sbin", "/var", "/System", "/Library")]


def _candidate_paths(args: dict | None) -> list[Path]:
    """Resolved paths a call would touch (shared: tools.paths)."""
    from aether_engine.tools.paths import candidate_paths

    return candidate_paths(args)


def is_sensitive_floor_path(path: Path) -> bool:
    """True when ``path`` sits under a home-sensitive floor prefix.

    The per-path form of the E2-06 floor, for tools that discover
    paths policy never judged as call arguments (E2-F03): a recursive
    reader must not open a floored descendant on the strength of an
    ancestor's verdict.
    """
    return any(path.is_relative_to(p) for p in _floor_prefixes())


def path_rule_denies(
    path: Path, path_rules: list | None, *, tool_name: str, risk: ToolRisk
) -> bool:
    """True when the first matching user path rule denies ``path``.

    The per-path form of :func:`_path_rule_verdict` for a concrete
    discovered path (E2-F03), honouring the rules' tool/risk scoping
    exactly as call-level resolution does.
    """
    probe = Tool(name=tool_name, description="", risk=risk)
    return _path_rule_verdict(probe, {"path": str(path)}, path_rules) == "deny"


def _apply_floor(verdict: str, tool: Tool, args: dict | None) -> str:
    """Raise an "allow" to "ask" when the call touches a floored prefix."""
    if verdict != "allow":
        return verdict
    for candidate in _candidate_paths(args):
        if any(candidate.is_relative_to(p) for p in _floor_prefixes()):
            return "ask"
        if tool.risk != ToolRisk.READ_ONLY and any(
            candidate.is_relative_to(p) for p in _system_prefixes()
        ):
            return "ask"
    return verdict


def _path_rule_verdict(
    tool: Tool, args: dict | None, path_rules: list | None
) -> str | None:
    """The first matching user path rule's verdict, or None.

    A rule is ``{"prefix": str, "verdict": "Always Allow" |
    "Require Approval" | "Deny", "tools": [names]?, "risks":
    [risk values]?}`` — ordered, first match wins, matching the
    resolved candidate path from the call's arguments.
    """
    if not path_rules:
        return None
    candidates = _candidate_paths(args)
    if not candidates:
        return None
    for rule in path_rules:
        if not isinstance(rule, dict):
            continue
        verdict = _VERDICT_MAP.get(str(rule.get("verdict", "")))
        prefix_raw = rule.get("prefix")
        if verdict is None or not isinstance(prefix_raw, str) or not prefix_raw:
            continue
        tools = rule.get("tools")
        if isinstance(tools, list) and tools and tool.name not in tools:
            continue
        risks = rule.get("risks")
        if isinstance(risks, list) and risks and tool.risk.value not in risks:
            continue
        prefix = Path(prefix_raw).expanduser().resolve()
        if any(c.is_relative_to(prefix) for c in candidates):
            return verdict
    return None


def resolve_tool_policy(
    tool: Tool,
    user_policies: dict[str, str],
    *,
    args: dict | None = None,
    path_rules: list | None = None,
    shell_extras: dict | None = None,
    approval_mode: str = "ask",
) -> str:
    """
    Resolves the effective policy for a tool based on user settings and its risk.
    Returns: 'allow', 'ask', or 'deny'.

    Epic 2, E2-06: when the call's arguments are available, ordered
    per-path rules refine the name-level verdict — a path rule can
    relax the DEFAULT ask for non-read-only tools (its whole purpose:
    "writes under ~/projects are fine"), but it can never override an
    explicit per-tool Deny or Require Approval, and the sensitive-path
    floor can raise any allow back to ask.

    Epic 2, E2-07: shell commands carry segment-wise safety rules on
    top of everything else — a deny rule denies outright (no config
    relaxes a fork bomb), an ask rule raises any allow to ask.
    """
    shell_verdict = _shell_verdict(tool, args, shell_extras)
    if shell_verdict is not None and shell_verdict.verdict == "deny":
        return "deny"

    verdict = _resolve_base_verdict(tool, user_policies, args=args, path_rules=path_rules, approval_mode=approval_mode)
    if shell_verdict is not None and shell_verdict.verdict == "ask" and verdict == "allow":
        return "ask"
    return verdict


def _shell_verdict(tool: Tool, args: dict | None, shell_extras: dict | None = None):
    """The shell safety verdict for an execute_shell call, or None.

    The single evaluation both the verdict path and the audit
    attribution path (E2-F13) read, so the rule named in the audit
    trail is always the rule that produced the verdict.
    """
    if tool.name != "execute_shell" or not args:
        return None
    from aether_engine.validation.shell_rules import evaluate_shell_command

    extras = shell_extras or {}
    return evaluate_shell_command(
        str(args.get("command", "")),
        extra_deny=extras.get("deny"),
        extra_ask=extras.get("ask"),
    )


def shell_denial_rule(
    tool: Tool, args: dict | None, shell_extras: dict | None = None
) -> str | None:
    """Name of the shell safety rule denying this call, or None.

    E2-F13: :func:`resolve_tool_policy` returns a bare "deny",
    discarding the evaluator's matched rule, so denial audit events
    could only record a generic reason. Callers that audit a denial
    use this to attribute it — the rule name as the policy model
    carries it (``ShellVerdict.rule``, e.g. "rm-root" or a configured
    "user-deny-0").
    """
    verdict = _shell_verdict(tool, args, shell_extras)
    if verdict is not None and verdict.verdict == "deny":
        return verdict.rule
    return None


def _resolve_base_verdict(
    tool: Tool,
    user_policies: dict[str, str],
    *,
    args: dict | None = None,
    path_rules: list | None = None,
    approval_mode: str = "ask",
) -> str:
    user_setting = user_policies.get(tool.name)

    # 1. Deny is always honored
    if user_setting == "Deny":
        return "deny"

    # 2. Per-path rules (only when this specific call's args are known)
    path_verdict = _path_rule_verdict(tool, args, path_rules)
    if path_verdict is not None:
        if path_verdict == "deny":
            return "deny"
        if user_setting == "Require Approval":
            return "ask"  # an explicit per-tool ask beats a path allow
        return _apply_floor(path_verdict, tool, args)

    # 3. explicit 'Require Approval' is always honored
    if user_setting == "Require Approval":
        return "ask"

    # 4. If risk is not read_only, 'Always Allow' is ignored, and it defaults to ask.
    #    E2-08: approval_mode "write" relaxes exactly this DEFAULT for
    #    WRITE-risk tools (explicit asks above are untouched, path-rule
    #    asks returned earlier, and the floor still applies).
    if tool.risk != ToolRisk.READ_ONLY:
        if approval_mode == "write" and tool.risk == ToolRisk.WRITE:
            return _apply_floor("allow", tool, args)
        return "ask"

    # 5. If read_only, and 'Always Allow', we allow it
    if user_setting == "Always Allow":
        return _apply_floor("allow", tool, args)

    # 6. Default by risk (read_only -> allow)
    # Since we already handled non-read_only above, this is guaranteed to be read_only here.
    return _apply_floor("allow", tool, args)


def tool_risk_by_name(tool_name: str, registry=None) -> ToolRisk:
    """The risk class a tool name resolves to for policy purposes.

    The registry's registered risk when the tool is registered, the
    known non-registry table for supervisor memory tools, and EXECUTE
    (fail closed) for anything unknown. This is the single derivation
    :func:`resolve_policy_by_name` uses; the Settings policy setter
    gates "Always Allow" on it too, so the setter, the Settings list
    and enforcement can no longer disagree about a tool's risk
    (E2-F07).
    """
    from aether_engine.tools.registry import ToolNotFoundError

    risk = ToolRisk.EXECUTE

    if registry:
        try:
            risk = registry.get_tool(tool_name).risk
        except ToolNotFoundError:
            pass

    if risk == ToolRisk.EXECUTE and tool_name in KNOWN_NON_REGISTRY_TOOLS:
        risk = KNOWN_NON_REGISTRY_TOOLS[tool_name]

    return risk


def resolve_policy_by_name(tool_name: str, user_policies: dict[str, str], registry=None, *, args: dict | None = None, path_rules: list | None = None, shell_extras: dict | None = None, approval_mode: str = "ask") -> str:
    """
    Helper to resolve policy given just a tool name, looking up in registry or fallbacks.
    """
    from aether_engine.tools.registry import Tool

    dummy_tool = Tool(name=tool_name, description="", risk=tool_risk_by_name(tool_name, registry))
    return resolve_tool_policy(dummy_tool, user_policies, args=args, path_rules=path_rules, shell_extras=shell_extras, approval_mode=approval_mode)
