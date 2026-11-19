"""Delegation settings — the P1e schema (orchestration programme, K-04).

One schema for delegation behaviour, owned by the engine: the mode
the supervisor delegates in, the caps and per-delegation budgets,
one row per agent (model / delegation rights / tool access /
strategy), and the tier aliases a row's model may name. The slices
that add delegation machinery (P1a/P1b routing, P2a cascade, P2b
fan-out, P2c tool retrieval) consume settings from here instead of
inventing their own config keys, and Full Furnish Batch 4 renders
exactly this shape in Settings. Until that UI lands, the settings
are editable through the config file and the CONFIG_UPDATE write
path (``config_updates.py`` validates ``delegation_settings``
strictly), exactly like ``model_routing`` today.

**Versioning & migration (the foundation the composed-stack audit,
RA2-03, required).** ``UserConfig`` had no schema versioning at
all — a flat dataclass with ``from_dict``/``to_dict``. This slice
builds the first versioned section: ``delegation_settings_version``
on the config (current version: 1) and :func:`migrate_delegation_settings`,
which ``UserConfig.from_dict`` runs over the stored block on every
load. A config file written before P1e has no block; migration
builds it from defaults plus the legacy keys below, so an existing
user's behaviour is unchanged by the upgrade.

**Where each field lives (one storage source per field).** Two
schema fields are *composed* by the resolver from their
long-standing flat keys instead of being duplicated into the block:

* ``max_delegations_per_turn`` ← ``supervisor_delegation_cap``
* ``per_delegation_cost_budget_usd`` ← ``subagent_cost_budget_usd``
  (the S13 budget, its semantics extended to all delegations)

Those flat keys still have live writers (the Settings write path,
per-task overlays), so storing a second copy inside the block
would invite exactly the divergence the plan's risk section warns
about ("two read paths during migration = divergent behaviour").
For this transition release the flat keys are the storage and the
schema is the single *read* path: every consumer resolves through
:func:`resolve_delegation_settings`, and a copy of either key found
inside a stored block is dropped by the cleaner, never read. The
per-agent ``model`` rows are folded from ``agent_models`` the same
way (``agent_models`` is written live by the Agents settings; on
any disagreement it wins, and the fold re-projects it into the
rows, so the block the UI will render never shows a stale pin).

**Honest availability.** A mode or strategy is only *effective*
when the machinery behind it exists (the S-series
honest-unavailable pattern). ``auto-route`` arrived with P1a/P1b
(supervisor tier emission + price/headroom scoring; its entry
below is available), ``auto-broadcast`` arrives with P2b, the
``tiered-cascade`` strategy with P2a, and ``auto`` tool access
with P2c Filter 2 (K-03). The schema accepts and stores the
not-yet-landed values today, but the resolver reports them
unavailable-with-reason and the effective mode falls back to
``auto-coordinate`` — never a silent no-op. The slices that land
the machinery flip their own availability entries here.

**Defaults are today's behaviour.** Mode ``auto-coordinate`` (the
supervisor delegates as it always has), cap 8 (the historical
``supervisor_delegation_cap`` default), depth 2 (supervisor →
specialist → sub-agent; sub-agents cannot spawn further), parallel
1 (delegations run sequentially), per-delegation timeout and token
budget 0 (disabled — the S13 convention; S13 keeps its own
per-sub-agent timeout/token bounds, which are not delegation
settings), cost budget 0.0 (disabled). Per agent: model
``inherit``, only the supervisor may delegate (to the three
specialists), tool access ``manual`` (an explicit ``agent_tools``
list, when one exists, narrows the P2c Filter-1 ceiling — today's
behaviour), ``tool_top_k`` 10 (P2c's retrieval default, consumed
when Filter 2 lands), strategy ``fixed``.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("aether_engine.delegation_settings")

#: The current delegation-settings schema version. Stored on
#: UserConfig as ``delegation_settings_version``; migration bumps
#: older blocks to this shape.
DELEGATION_SETTINGS_VERSION = 1

#: The agents a delegation row exists for.
AGENTS: tuple[str, ...] = ("supervisor", "researcher", "planner", "coder")

#: Delegation modes (``delegation_mode`` in the plan).
DELEGATION_MODES: tuple[str, ...] = (
    "off", "manual", "auto-coordinate", "auto-route", "auto-broadcast",
)
DEFAULT_MODE = "auto-coordinate"

#: Per-agent tool-access modes (P2c's three filters).
TOOL_ACCESS_MODES: tuple[str, ...] = ("manual", "auto", "all-allowed")
DEFAULT_TOOL_ACCESS = "manual"

#: Per-delegation-type strategies.
STRATEGIES: tuple[str, ...] = ("fixed", "tiered-cascade", "strong-always")
DEFAULT_STRATEGY = "fixed"

#: Tier aliases a row's ``model`` (or an ``agent_models`` value) may
#: name instead of a concrete model. Resolved through the S14
#: registry tiers by ``langgraph/model_routing.py``.
TIER_ALIASES: tuple[str, ...] = ("strong", "balanced", "cheap", "local")

#: The model value meaning "no pin — the run's resolution applies".
INHERIT = "inherit"

# Bounds for the numeric fields. The load-time cleaner clamps into
# these (fail safe); the Settings write path refuses outside them.
MAX_DELEGATION_DEPTH_BOUNDS = (1, 8)
MAX_PARALLEL_DELEGATIONS_BOUNDS = (1, 16)
PER_DELEGATION_TIMEOUT_S_BOUNDS = (0, 86_400)
PER_DELEGATION_TOKEN_BUDGET_BOUNDS = (0, 10_000_000)
TOOL_TOP_K_BOUNDS = (1, 100)

#: Global block fields (the composed cap/cost fields are NOT block
#: fields — see the module docstring).
_BLOCK_INT_FIELDS = (
    "max_delegation_depth",
    "max_parallel_delegations",
    "per_delegation_timeout_s",
    "per_delegation_token_budget",
)
_BLOCK_FIELDS = frozenset({"mode", "agents", *_BLOCK_INT_FIELDS})
_ROW_FIELDS = frozenset({
    "model", "can_delegate", "delegate_to",
    "tool_access", "tool_top_k", "strategy",
})

# Honest availability: mode -> (available, reason-when-not). A mode
# is effective only when its machinery exists; the slices that land
# the machinery flip their entry here.
_MODE_AVAILABILITY: dict[str, tuple[bool, str]] = {
    "off": (True, ""),
    # Manual and off are enforced at the tools-topology
    # delegation seam (langgraph/delegation.py, the K-04 hookup):
    # off suppresses the delegation tools' offer and refuses any
    # call that still arrives; manual pauses every delegation for
    # owner approval via interrupt() before the specialist runs.
    "manual": (True, ""),
    "auto-coordinate": (True, ""),
    # P1a (supervisor tier emission, K-08) and P1b (price/headroom
    # scoring, K-09) have landed: the machinery auto-route names —
    # the supervisor stating each delegation's tier and S14
    # routing on it — exists, so the mode is honestly available.
    # S14's own model_routing flag still gates the router itself.
    "auto-route": (True, ""),
    # P2b (K-12) landed the fan-out machinery
    # (langgraph/fanout.py): the mode is effective — parallel
    # scheduling still requires a resolved width of 2+ and a
    # Gate-4 pair, judged per group at the fan-out seam.
    "auto-broadcast": (True, ""),
}

_STRATEGY_AVAILABILITY: dict[str, tuple[bool, str]] = {
    "fixed": (True, ""),
    "tiered-cascade": (False, "tiered-cascade arrives with P2a "
                              "(the coder verifier cascade)"),
    "strong-always": (False, "strong-always has no consumer yet — "
                             "it lands with the routing slices that "
                             "read per-delegation strategy"),
}

_TOOL_ACCESS_AVAILABILITY: dict[str, tuple[bool, str]] = {
    "manual": (True, ""),
    "all-allowed": (True, ""),
    "auto": (False, "auto tool access arrives with P2c Filter 2 "
                    "(task-based tool retrieval, K-03); until then "
                    "the Filter-1 ceiling and any manual list apply"),
}


def mode_availability(mode: str) -> tuple[bool, str]:
    """(available, reason) for a delegation mode."""
    return _MODE_AVAILABILITY.get(
        mode, (False, f"unknown delegation mode {mode!r}"))


def strategy_availability(strategy: str) -> tuple[bool, str]:
    """(available, reason) for a per-delegation strategy."""
    return _STRATEGY_AVAILABILITY.get(
        strategy, (False, f"unknown strategy {strategy!r}"))


def tool_access_availability(mode: str) -> tuple[bool, str]:
    """(available, reason) for a per-agent tool-access mode."""
    return _TOOL_ACCESS_AVAILABILITY.get(
        mode, (False, f"unknown tool-access mode {mode!r}"))


def is_tier_alias(value: Any) -> bool:
    """True when a model value names a tier alias, not a model."""
    return isinstance(value, str) and value.strip().lower() in TIER_ALIASES


def classify_model_value(value: Any) -> str:
    """What a per-agent model value is: "inherit" | "alias" | "pin".

    Anything that is not ``inherit`` and not a tier alias is a pin
    (a LiteLLM-resolvable ``provider:model`` or bare model name) —
    the pre-P1e reading of ``agent_models`` values, unchanged.
    """
    if not isinstance(value, str) or not value.strip() \
            or value.strip().lower() == INHERIT:
        return "inherit"
    if is_tier_alias(value):
        return "alias"
    return "pin"


# ── Defaults ──────────────────────────────────────────────────────


def _default_row(agent: str) -> dict[str, Any]:
    if agent == "supervisor":
        return {
            "model": INHERIT,
            "can_delegate": True,
            "delegate_to": ["researcher", "planner", "coder"],
            "tool_access": DEFAULT_TOOL_ACCESS,
            "tool_top_k": 10,
            "strategy": DEFAULT_STRATEGY,
        }
    return {
        "model": INHERIT,
        "can_delegate": False,
        "delegate_to": [],
        "tool_access": DEFAULT_TOOL_ACCESS,
        "tool_top_k": 10,
        "strategy": DEFAULT_STRATEGY,
    }


def default_delegation_settings() -> dict[str, Any]:
    """The full default block (fresh dicts on every call)."""
    return {
        "mode": DEFAULT_MODE,
        "max_delegation_depth": 2,
        "max_parallel_delegations": 1,
        "per_delegation_timeout_s": 0,
        "per_delegation_token_budget": 0,
        "agents": {agent: _default_row(agent) for agent in AGENTS},
    }


# ── Cleaning (load path: total, fail-safe, never raises) ─────────


def _clean_int(value: Any, default: int, bounds: tuple[int, int]) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return default
    low, high = bounds
    return max(low, min(high, value))


def _clean_row(agent: str, raw: Any) -> dict[str, Any]:
    row = _default_row(agent)
    if not isinstance(raw, dict):
        return row
    model = raw.get("model")
    if isinstance(model, str) and model.strip():
        row["model"] = model.strip()
    if isinstance(raw.get("can_delegate"), bool):
        row["can_delegate"] = raw["can_delegate"]
    delegate_to = raw.get("delegate_to")
    if isinstance(delegate_to, list):
        row["delegate_to"] = list(dict.fromkeys(
            name for name in delegate_to
            if isinstance(name, str) and name in AGENTS and name != agent))
    if raw.get("tool_access") in TOOL_ACCESS_MODES:
        row["tool_access"] = raw["tool_access"]
    row["tool_top_k"] = _clean_int(
        raw.get("tool_top_k"), row["tool_top_k"], TOOL_TOP_K_BOUNDS)
    if raw.get("strategy") in STRATEGIES:
        row["strategy"] = raw["strategy"]
    return row


def clean_delegation_settings(raw: Any) -> dict[str, Any]:
    """Clean a stored block into the full schema shape.

    Total and fail-safe, in the config loader's house style: a
    missing/corrupt block yields the defaults, an unknown mode or
    strategy falls back to its default, numerics clamp into their
    bounds, unknown keys (including copies of the composed
    cap/cost fields — see the module docstring) are dropped, and
    rows exist for exactly the four known agents.
    """
    block = default_delegation_settings()
    if not isinstance(raw, dict):
        return block
    if raw.get("mode") in DELEGATION_MODES:
        block["mode"] = raw["mode"]
    block["max_delegation_depth"] = _clean_int(
        raw.get("max_delegation_depth"), block["max_delegation_depth"],
        MAX_DELEGATION_DEPTH_BOUNDS)
    block["max_parallel_delegations"] = _clean_int(
        raw.get("max_parallel_delegations"),
        block["max_parallel_delegations"], MAX_PARALLEL_DELEGATIONS_BOUNDS)
    block["per_delegation_timeout_s"] = _clean_int(
        raw.get("per_delegation_timeout_s"),
        block["per_delegation_timeout_s"], PER_DELEGATION_TIMEOUT_S_BOUNDS)
    block["per_delegation_token_budget"] = _clean_int(
        raw.get("per_delegation_token_budget"),
        block["per_delegation_token_budget"],
        PER_DELEGATION_TOKEN_BUDGET_BOUNDS)
    raw_agents = raw.get("agents")
    if isinstance(raw_agents, dict):
        for agent in AGENTS:
            if agent in raw_agents:
                block["agents"][agent] = _clean_row(agent, raw_agents[agent])
    return block


def fold_agent_models(block: dict[str, Any],
                      agent_models: Any) -> dict[str, Any]:
    """Project ``agent_models`` pins into the block's per-agent rows.

    ``agent_models`` is the live-written pin map (Agents settings);
    on any disagreement it wins, so the rows the settings surface
    renders never show a stale pin. Existing pins migrate
    losslessly: a pin becomes the row's ``model`` verbatim
    (``inherit`` included). Returns a new block; the input is not
    mutated. Agents outside the schema's four are left to
    ``agent_models`` alone — they have no row.
    """
    out = copy.deepcopy(block) if isinstance(block, dict) \
        else default_delegation_settings()
    if not isinstance(agent_models, dict):
        return out
    agents = out.setdefault("agents", {})
    for agent in AGENTS:
        if agent in agent_models:
            value = agent_models[agent]
            row = agents.get(agent)
            if not isinstance(row, dict):
                row = _default_row(agent)
                agents[agent] = row
            row["model"] = value if isinstance(value, str) and value.strip() \
                else INHERIT
    return out


def migrate_delegation_settings(raw: Any, *,
                                agent_models: Any = None,
                                stored_version: Any = None,
                                ) -> dict[str, Any]:
    """Migrate a stored block to the current schema version.

    The one migration path ``UserConfig.from_dict`` runs. Version 1
    is the first versioned shape, so migration is: clean whatever
    is stored into the v1 shape (a pre-P1e file has no block at
    all — defaults), then fold the legacy ``agent_models`` pins
    into the rows. The composed fields need no block migration:
    their storage never moved (``supervisor_delegation_cap`` /
    ``subagent_cost_budget_usd`` are read by the resolver). A block
    stored by a NEWER engine (version above current) is still
    cleaned into the shape this engine understands, and the caller
    keeps the stored version number so a re-save does not silently
    downgrade it.
    """
    if isinstance(stored_version, int) \
            and stored_version > DELEGATION_SETTINGS_VERSION:
        logger.warning(
            "Delegation settings stored at schema version %s, above "
            "this engine's %s; reading the fields it knows.",
            stored_version, DELEGATION_SETTINGS_VERSION)
    return fold_agent_models(clean_delegation_settings(raw), agent_models)


def sync_agent_model_row(user_config: Any, agent: str, model: Any) -> None:
    """Keep a live config object's row in step with a pin write.

    The Agents settings writes ``agent_models`` on the live object
    and saves; without this the object's delegation rows would show
    the old pin until the next load's fold. Mutates the object's
    block in place; a no-op for agents outside the schema's four
    and for config doubles without a block.
    """
    if agent not in AGENTS:
        return
    block = getattr(user_config, "delegation_settings", None)
    if not isinstance(block, dict):
        return
    agents = block.setdefault("agents", {})
    row = agents.get(agent)
    if not isinstance(row, dict):
        row = _default_row(agent)
        agents[agent] = row
    row["model"] = model if isinstance(model, str) and model.strip() \
        else INHERIT


# ── Strict validation (Settings write path) ───────────────────────


def _fail(field_name: str, problem: str) -> ValueError:
    return ValueError(f"{field_name}: {problem}")


def _validate_int(field_name: str, value: Any,
                  bounds: tuple[int, int]) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(field_name, "must be an integer")
    low, high = bounds
    if not low <= value <= high:
        raise _fail(field_name, f"must be between {low} and {high}")
    return value


def _validate_row(agent: str, raw: Any) -> dict[str, Any]:
    field_name = f"delegation_settings.agents.{agent}"
    if not isinstance(raw, dict):
        raise _fail(field_name, "must be an object")
    unknown = sorted(set(raw) - _ROW_FIELDS)
    if unknown:
        raise _fail(field_name, f"unknown row keys: {', '.join(unknown)} "
                                f"(allowed: {', '.join(sorted(_ROW_FIELDS))})")
    row = _default_row(agent)
    if "model" in raw:
        model = raw["model"]
        if not isinstance(model, str) or not model.strip():
            raise _fail(f"{field_name}.model",
                        "must be a model name, a tier alias "
                        f"({', '.join(TIER_ALIASES)}) or 'inherit'")
        row["model"] = model.strip()
    if "can_delegate" in raw:
        if not isinstance(raw["can_delegate"], bool):
            raise _fail(f"{field_name}.can_delegate", "must be a boolean")
        row["can_delegate"] = raw["can_delegate"]
    if "delegate_to" in raw:
        names = raw["delegate_to"]
        if not isinstance(names, list) or any(
                not isinstance(n, str) for n in names):
            raise _fail(f"{field_name}.delegate_to",
                        "must be a list of agent names")
        bad = [n for n in names if n not in AGENTS]
        if bad:
            raise _fail(f"{field_name}.delegate_to",
                        f"unknown agents: {', '.join(bad)} "
                        f"(valid: {', '.join(AGENTS)})")
        if agent in names:
            raise _fail(f"{field_name}.delegate_to",
                        "an agent cannot delegate to itself")
        row["delegate_to"] = list(dict.fromkeys(names))
    if "tool_access" in raw:
        if raw["tool_access"] not in TOOL_ACCESS_MODES:
            raise _fail(f"{field_name}.tool_access",
                        f"must be one of {', '.join(TOOL_ACCESS_MODES)}")
        row["tool_access"] = raw["tool_access"]
    if "tool_top_k" in raw:
        row["tool_top_k"] = _validate_int(
            f"{field_name}.tool_top_k", raw["tool_top_k"], TOOL_TOP_K_BOUNDS)
    if "strategy" in raw:
        if raw["strategy"] not in STRATEGIES:
            raise _fail(f"{field_name}.strategy",
                        f"must be one of {', '.join(STRATEGIES)}")
        row["strategy"] = raw["strategy"]
    return row


def validate_delegation_settings(value: Any) -> dict[str, Any]:
    """Strictly validate a ``delegation_settings`` Settings write.

    The write-path contract (``config_updates.py``): a value the
    user just typed is applied exactly or refused with a
    field-naming error — never silently dropped or coerced, because
    a stored-but-unread value is a placebo control. A partial block
    is fine: absent fields take their defaults in the returned
    complete block. Unknown keys are refused (the consumer would
    ignore them). The composed cap/cost fields are refused here
    too: they are written through their own flat fields
    (``supervisor_delegation_cap`` / ``subagent_cost_budget_usd``),
    and accepting a block copy would store a value nothing reads.
    """
    if not isinstance(value, dict):
        raise _fail("delegation_settings", "must be an object")
    for composed in ("max_delegations_per_turn",
                     "per_delegation_cost_budget_usd"):
        if composed in value:
            raise _fail(
                "delegation_settings",
                f"{composed!r} is not a delegation_settings field — "
                "it is written through its own config field "
                "('supervisor_delegation_cap' / "
                "'subagent_cost_budget_usd') and read into the "
                "schema by the resolver")
    unknown = sorted(set(value) - _BLOCK_FIELDS)
    if unknown:
        raise _fail("delegation_settings",
                    f"unknown keys: {', '.join(unknown)} "
                    f"(allowed: {', '.join(sorted(_BLOCK_FIELDS))})")
    block = default_delegation_settings()
    if "mode" in value:
        if value["mode"] not in DELEGATION_MODES:
            raise _fail("delegation_settings.mode",
                        f"must be one of {', '.join(DELEGATION_MODES)}")
        block["mode"] = value["mode"]
    for name, bounds in (
            ("max_delegation_depth", MAX_DELEGATION_DEPTH_BOUNDS),
            ("max_parallel_delegations", MAX_PARALLEL_DELEGATIONS_BOUNDS),
            ("per_delegation_timeout_s", PER_DELEGATION_TIMEOUT_S_BOUNDS),
            ("per_delegation_token_budget",
             PER_DELEGATION_TOKEN_BUDGET_BOUNDS)):
        if name in value:
            block[name] = _validate_int(
                f"delegation_settings.{name}", value[name], bounds)
    if "agents" in value:
        raw_agents = value["agents"]
        if not isinstance(raw_agents, dict):
            raise _fail("delegation_settings.agents",
                        "must be an object mapping agent names to rows")
        bad = [a for a in raw_agents if a not in AGENTS]
        if bad:
            raise _fail("delegation_settings.agents",
                        f"unknown agents: {', '.join(bad)} "
                        f"(valid: {', '.join(AGENTS)})")
        for agent, raw_row in raw_agents.items():
            block["agents"][agent] = _validate_row(agent, raw_row)
    return block


# ── The resolved view (the single read path) ──────────────────────


@dataclass(frozen=True)
class AgentDelegationSettings:
    """One agent's resolved delegation row."""

    model: str = INHERIT
    can_delegate: bool = False
    delegate_to: tuple[str, ...] = ()
    tool_access: str = DEFAULT_TOOL_ACCESS
    tool_top_k: int = 10
    strategy: str = DEFAULT_STRATEGY

    @property
    def model_kind(self) -> str:
        """"inherit" | "alias" | "pin" — see classify_model_value."""
        return classify_model_value(self.model)


@dataclass(frozen=True)
class DelegationSettings:
    """The resolved delegation settings for one config object.

    ``mode`` is the configured mode; ``effective_mode`` is the mode
    the engine may actually run — an unavailable configured mode
    falls back to ``auto-coordinate`` with the reason carried in
    ``mode_unavailable_reason`` (empty when the configured mode is
    available). ``version`` is the stored schema version.
    """

    mode: str = DEFAULT_MODE
    effective_mode: str = DEFAULT_MODE
    mode_unavailable_reason: str = ""
    max_delegations_per_turn: int = 8
    max_delegation_depth: int = 2
    max_parallel_delegations: int = 1
    per_delegation_timeout_s: int = 0
    per_delegation_token_budget: int = 0
    per_delegation_cost_budget_usd: float = 0.0
    version: int = DELEGATION_SETTINGS_VERSION
    agents: dict[str, AgentDelegationSettings] = field(
        default_factory=lambda: {
            agent: AgentDelegationSettings(
                can_delegate=(agent == "supervisor"),
                delegate_to=(("researcher", "planner", "coder")
                             if agent == "supervisor" else ()),
            )
            for agent in AGENTS
        })

    def agent(self, name: str) -> AgentDelegationSettings:
        """The row for ``name`` (a default row for unknown agents)."""
        return self.agents.get(name, AgentDelegationSettings())


def _resolved_cap(user_config: Any) -> int:
    raw = getattr(user_config, "supervisor_delegation_cap", 8)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 8
    return raw


def _resolved_cost_budget(user_config: Any) -> float:
    raw = getattr(user_config, "subagent_cost_budget_usd", 0.0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
        return 0.0
    return float(raw)


def resolve_delegation_settings(user_config: Any | None) -> DelegationSettings:
    """Resolve a config object's delegation settings — THE read path.

    Attribute-tolerant like the spawn policy's own reads: a missing
    or partial config (None, a test double, a pre-P1e object
    without the block) yields the documented defaults rather than
    an exception on the routing path. The block is cleaned on the
    way in, so a hand-built object carrying a raw dict resolves to
    the same values a load would produce; the composed cap and
    cost budget come from their flat fields (see the module
    docstring); the rows are folded from ``agent_models`` so a pin
    written after load is honoured.
    """
    block = clean_delegation_settings(
        getattr(user_config, "delegation_settings", None))
    block = fold_agent_models(
        block, getattr(user_config, "agent_models", None))
    mode = block["mode"]
    available, reason = mode_availability(mode)
    version = getattr(user_config, "delegation_settings_version",
                      DELEGATION_SETTINGS_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) \
            or version < 1:
        version = DELEGATION_SETTINGS_VERSION
    agents = {
        agent: AgentDelegationSettings(
            model=block["agents"][agent]["model"],
            can_delegate=block["agents"][agent]["can_delegate"],
            delegate_to=tuple(block["agents"][agent]["delegate_to"]),
            tool_access=block["agents"][agent]["tool_access"],
            tool_top_k=block["agents"][agent]["tool_top_k"],
            strategy=block["agents"][agent]["strategy"],
        )
        for agent in AGENTS
    }
    return DelegationSettings(
        mode=mode,
        effective_mode=mode if available else DEFAULT_MODE,
        mode_unavailable_reason="" if available else reason,
        max_delegations_per_turn=_resolved_cap(user_config),
        max_delegation_depth=block["max_delegation_depth"],
        max_parallel_delegations=block["max_parallel_delegations"],
        per_delegation_timeout_s=block["per_delegation_timeout_s"],
        per_delegation_token_budget=block["per_delegation_token_budget"],
        per_delegation_cost_budget_usd=_resolved_cost_budget(user_config),
        version=version,
        agents=agents,
    )
