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
the user's safety settings — ``tool_policies`` (a project must never
loosen a Deny), ``path_rules`` (Epic 2: a project must not relax the
path rules that govern it), and ``workspace_roots`` (a project must
not redefine its own containment) are stripped from the project layer
before the merge and the attempt is logged. The per-task overlay
comes from the user's own client over their own socket and keeps
full power.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from aether_engine.config import UserConfig

logger = logging.getLogger("aether_engine.config_layers")

PROJECT_CONFIG_RELPATH = Path(".aether") / "config.json"
PROJECT_FORBIDDEN_KEYS = frozenset({"tool_policies", "path_rules", "workspace_roots", "mcp_tool_risk_overrides",
    "onboarding_completed", "ttsr_rules", "memory_backend"})


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
