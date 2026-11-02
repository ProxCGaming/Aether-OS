"""Epic 1, E1-14: config layers.

A task's effective config is resolved per run, never by mutating the
global config:

    global (the user's saved UserConfig)
      → project (``.aether/config.json`` in the task's workspace)
      → per-task overlay (``config_overlay`` in the START_TASK payload)

Merge rule: dicts merge key-wise; every other value — arrays included —
REPLACES. An overlay value of None leaves the layer below untouched.

Trust boundary: the project file lives inside a workspace, which may
be a cloned repo the user did not author. It therefore may NOT touch
any user setting that can loosen safety or redirect behaviour — all
of ``PROJECT_FORBIDDEN_KEYS`` is stripped from the project layer
before the merge and the attempt is logged. That covers, by category:
approval policy (``approval_mode``, ``tool_policies``, ``path_rules``,
``mcp_tool_risk_overrides``, ``ttsr_rules``); containment and shell
rules (``workspace_roots``, ``extra_shell_deny_patterns``,
``extra_shell_ask_patterns`` — arrays replace, so a project could
otherwise clear the user's own patterns by setting ``[]``); tool,
skill and delegation surface (``agent_tools``, ``agent_skills``,
``supervisor_spawn_allowlist``, ``supervisor_delegation_cap``,
``task_classes`` sandbox caps); provider and model routing
(``default_provider``, ``default_model``, ``provider_models``,
``provider_excluded_models``, ``custom_base_urls``,
``custom_provider_names``, ``custom_provider_types``, ``agent_models``,
``auxiliary_models``, ``prewalk_agents``, ``fallback_chain``,
``disable_fallbacks``, ``search_provider``,
``search_fallback_to_ddgs``); memory (``memory_backend``,
``embedding_model``, the retention/count caps and
``episodic_similarity_threshold`` — a project must not be able to
trigger pruning of, or change what is recalled from, the user's
stores); browser-agent policy (``browser_agent_enabled``,
``browser_vault_autofill``, ``browser_envelopes_enabled``,
``browser_category_modes``); output validation
(``agent_output_schemas``); spend (``daily_budget_usd``); and
first-run state (``onboarding_completed``). Stripping is top-level
only, and that is sufficient: ``merge_layers`` merges key-wise at
the top level and ``UserConfig.from_dict`` reads only named
top-level fields, so a forbidden name nested inside an allowed
dict-valued key stays nested under that key and is never promoted
to a sibling setting. The per-task overlay comes from the user's
own client over their own socket and keeps full power.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from aether_engine.config import UserConfig

logger = logging.getLogger("aether_engine.config_layers")

PROJECT_CONFIG_RELPATH = Path(".aether") / "config.json"
PROJECT_FORBIDDEN_KEYS = frozenset({
    # Approval policy and containment (the original strip list).
    "tool_policies", "path_rules", "workspace_roots",
    "mcp_tool_risk_overrides", "onboarding_completed", "ttsr_rules",
    "memory_backend",
    # F-01 (EGM C-B12): approval mode — a project must never turn
    # "ask" into "write" (WRITE-risk calls auto-allowed).
    "approval_mode",
    # Tool / skill / delegation surface.
    "agent_tools", "agent_skills",
    "supervisor_spawn_allowlist", "supervisor_delegation_cap",
    # Provider and model routing, incl. custom endpoints (a project
    # must not point the user's agents, keys or spend at its own
    # endpoint) and fallback behaviour.
    "agent_models", "auxiliary_models",
    "default_provider", "default_model",
    "provider_models", "provider_excluded_models",
    "custom_base_urls", "custom_provider_names", "custom_provider_types",
    "disable_fallbacks", "fallback_chain",
    # Shell rules (arrays replace: [] would clear the user's extras).
    "extra_shell_deny_patterns", "extra_shell_ask_patterns",
    # Search routing.
    "search_provider", "search_fallback_to_ddgs",
    # Memory: embedder, retention/count caps (<=0 disables pruning;
    # a low cap could make a task prune the user's stores) and the
    # recall threshold (0.0 surfaces unrelated episodes).
    "embedding_model",
    "episodic_retention_days", "episodic_max_episodes",
    "episodic_similarity_threshold",
    "kg_fact_retention_days", "kg_max_facts",
    "session_retention_days", "session_max_sessions",
    # Browser-agent confirmation policy (vault autofill and the
    # per-category modes are the loosening surface).
    "browser_agent_enabled", "browser_vault_autofill",
    "browser_envelopes_enabled", "browser_category_modes",
    # Spend limit.
    "daily_budget_usd",
    # Beyond the programme list, judged on the same test:
    # task_classes carries the sandbox time/memory caps (raising
    # them weakens resource containment and spend control);
    # prewalk_agents switches an agent to its execution model at
    # its first write/execute tool, i.e. provider routing for
    # exactly the calls approvals guard; agent_output_schemas can
    # be weakened from inside via array-replace (e.g. required: []);
    # repo_map_token_budget is serialised in the global config
    # (UserConfig.to_dict/from_dict) but stripped here so a project
    # layer cannot change it — it stays global-only.
    "task_classes", "prewalk_agents", "agent_output_schemas",
    "repo_map_token_budget",
})


def load_project_layer(workspace_root: str | None) -> dict[str, Any]:
    """The workspace's project config dict, or {} when absent/unusable."""
    if not workspace_root:
        return {}
    path = Path(workspace_root) / PROJECT_CONFIG_RELPATH
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def merge_layers(base: dict, overlay: dict) -> dict:
    """Overlay ``overlay`` onto ``base`` (dicts merge, arrays replace)."""
    merged = dict(base)
    for key, value in (overlay or {}).items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_layers(merged[key], value)
        else:
            merged[key] = value
    return merged


def effective_user_config(global_config: UserConfig,
                          workspace_root: str | None = None,
                          overlay: Any = None) -> UserConfig:
    """Resolve the layered config for one task.

    Returns the global config object itself when neither a project
    layer nor an overlay applies, so the common path is untouched.
    """
    project = load_project_layer(workspace_root)
    has_overlay = isinstance(overlay, dict) and bool(overlay)
    if not project and not has_overlay:
        return global_config
    layered = global_config.to_dict()
    if project:
        stripped = {k: v for k, v in project.items() if k not in PROJECT_FORBIDDEN_KEYS}
        ignored = sorted(set(project) & PROJECT_FORBIDDEN_KEYS)
        if ignored:
            logger.warning(
                "Project config at %s tried to set %s; those keys are global-only and were ignored",
                workspace_root, ignored)
        layered = merge_layers(layered, stripped)
    if has_overlay:
        layered = merge_layers(layered, overlay)
    return UserConfig.from_dict(layered)
