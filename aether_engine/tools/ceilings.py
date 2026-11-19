"""Per-agent tool ceilings — P2c Filter 1 (orchestration programme).

Three filters decide what a specialist delegation can do with tools:

* **Filter 1 — this module.** A hard per-agent ceiling on what may be
  OFFERED to the specialist's model, applied where the specialist's
  tool list is resolved (`langgraph/nodes/runner.py`). Retrieval
  (P2c Filter 2, a later slice) may narrow an offer further; nothing
  may widen it past the ceiling.
* **Filter 2 — task-based retrieval** inside the ceiling
  (`tools/retrieval.py`, P2c step 3): narrows this filter's output
  per delegation when enabled; the offered-vs-called log this slice
  adds is its evidence base.
* **Filter 3 — the policy/approval execution gate**
  (`tools/policy.py`, `tools/enforcement.py`), unchanged: it judges
  each call at execution time, offered or not.

A ceiling is expressed in `ToolRisk` classes plus explicit name
allowances, never as a per-tool list, so a tool registered later —
including an MCP tool — is judged by the risk it carries. MCP tools
default to `EXECUTE` (fail closed, the registry's own default), so an
MCP server that labels nothing grants a read-only agent nothing.

**Superseded mechanism.** Before this module, `runner.py` applied a
per-specialist `agent_tools` allow-list from config when one was set.
The ceiling replaces it as the one declarative source: a legacy
`agent_tools` list now INTERSECTS with the ceiling — it can narrow an
agent's offer further, never widen it past the ceiling.

**Already-used tools.** A tool the specialist has already used in
this conversation is never removed from the offer outright; it is
kept with a "DO NOT USE" description (the pre-existing behaviour),
so a conversation whose earlier rounds legitimately used the tool
never dangles. The ceiling removes only never-used tools.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from aether_engine.tools.registry import ToolRisk

#: Description stamped on an already-used tool the ceiling now
#: excludes — the exact wording the legacy allow-list used, kept so
#: prompts built on it read identically.
DISABLED_DESCRIPTION = "DO NOT USE THIS TOOL. IT IS CURRENTLY DISABLED FOR YOUR ROLE."


@dataclass(frozen=True)
class ToolCeiling:
    """What one agent may be offered: a set of risk classes, plus
    explicit name allowances for tools whose risk class alone would
    exclude them (an exact-name set and name prefixes)."""

    risks: frozenset[ToolRisk]
    extra_names: frozenset[str] = field(default_factory=frozenset)
    name_prefixes: tuple[str, ...] = ()

    def allows(self, name: str, risk: ToolRisk) -> bool:
        if risk in self.risks:
            return True
        if name in self.extra_names:
            return True
        return any(name.startswith(prefix) for prefix in self.name_prefixes)


#: The shipped ceilings. An agent with no entry has NO ceiling (its
#: offer is the full registry set, the pre-P2c behaviour) — a new
#: specialist starts unrestricted until a ceiling is declared here.
AGENT_TOOL_CEILINGS: dict[str, ToolCeiling] = {
    # Read/search only — never write, delete or execute. This is the
    # programme's headline guarantee: no prompt injection in a fetched
    # page can talk the researcher into a write, because no write
    # tool is ever in its offer.
    "researcher": ToolCeiling(risks=frozenset({ToolRisk.READ_ONLY})),
    # Read plus its scratchpad: the scratchpad tools render/keep the
    # planner's working notes and are allowed by name prefix whatever
    # risk they carry; general write/execute tools are not offered.
    "planner": ToolCeiling(
        risks=frozenset({ToolRisk.READ_ONLY}),
        name_prefixes=("scratchpad",),
    ),
    # The full current set — the coder's work IS writing, executing
    # and (with approval) deleting. Declared explicitly so the
    # absence of restriction is a decision, not an omission; Filter 3
    # still judges every call.
    "coder": ToolCeiling(risks=frozenset(ToolRisk)),
}


def ceiling_for(agent: str) -> ToolCeiling | None:
    """The agent's ceiling, or None when it has none."""
    return AGENT_TOOL_CEILINGS.get(agent)


def apply_tool_ceiling(
    agent: str,
    definitions: list[dict[str, Any]],
    risk_by_name: dict[str, ToolRisk],
    *,
    legacy_allowed: list[str] | None = None,
    used_names: frozenset[str] | set[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Filter one specialist's offered tool definitions by its ceiling.

    ``definitions`` are the registry's OpenAI-shaped dicts;
    ``risk_by_name`` maps tool name → `ToolRisk` for every registered
    tool. A definition whose name is unknown to the registry is
    judged `EXECUTE` (fail closed, the registry's own default for an
    unlabelled tool).

    ``legacy_allowed`` is the agent's pre-P2c `agent_tools` config
    list when one is set: it intersects with the ceiling (narrows,
    never widens). ``used_names`` are tools already used in this
    conversation: an excluded one is kept with `DISABLED_DESCRIPTION`
    instead of being removed.
    """
    ceiling = ceiling_for(agent)
    if ceiling is None and legacy_allowed is None:
        return list(definitions)
    legacy = set(legacy_allowed) if legacy_allowed is not None else None
    out: list[dict[str, Any]] = []
    for definition in definitions:
        name = (definition.get("function") or {}).get("name")
        if not name:
            out.append(definition)
            continue
        allowed = True
        if ceiling is not None:
            allowed = ceiling.allows(name, risk_by_name.get(name, ToolRisk.EXECUTE))
        if allowed and legacy is not None:
            allowed = name in legacy
        if allowed:
            out.append(definition)
        elif name in used_names:
            disabled = copy.deepcopy(definition)
            disabled["function"]["description"] = DISABLED_DESCRIPTION
            out.append(disabled)
    return out
