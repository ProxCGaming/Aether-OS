"""P1e (K-04) — the delegation settings schema: S22 contract tests.

The schema is the contract the delegation slices consume and the
Settings UI will render, so these tests pin its wire shape: the
stored block's fields and defaults, the version field, the
migration of pre-P1e config files (legacy keys only — behaviour
unchanged), fail-safe cleaning of corrupt values, the strict
Settings write path, the single read path (supervisor cap and the
S13 cost budget resolve through the schema), tier-alias
classification/resolution, and the runner's tool_access modes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aether_engine.config import UserConfig
from aether_engine.config_updates import validate_handed_off_updates
from aether_engine.delegation_settings import (
    DELEGATION_SETTINGS_VERSION,
    classify_model_value,
    clean_delegation_settings,
    default_delegation_settings,
    is_tier_alias,
    migrate_delegation_settings,
    mode_availability,
    resolve_delegation_settings,
    strategy_availability,
    sync_agent_model_row,
    tool_access_availability,
    validate_delegation_settings,
)

# ── Defaults: today's behaviour, verbatim ─────────────────────────


class TestDefaults:
    def test_global_defaults(self):
        s = resolve_delegation_settings(UserConfig())
        assert s.mode == "auto-coordinate"
        assert s.effective_mode == "auto-coordinate"
        assert s.mode_unavailable_reason == ""
        assert s.max_delegations_per_turn == 8
        assert s.max_delegation_depth == 2
        assert s.max_parallel_delegations == 1
        assert s.per_delegation_timeout_s == 0
        assert s.per_delegation_token_budget == 0
        assert s.per_delegation_cost_budget_usd == 0.0
        assert s.version == DELEGATION_SETTINGS_VERSION == 1

    def test_agent_row_defaults(self):
        s = resolve_delegation_settings(UserConfig())
        supervisor = s.agent("supervisor")
        assert supervisor.model == "inherit"
        assert supervisor.can_delegate is True
        assert supervisor.delegate_to == ("researcher", "planner", "coder")
        for name in ("researcher", "planner", "coder"):
            row = s.agent(name)
            assert row.model == "inherit"
            assert row.can_delegate is False
            assert row.delegate_to == ()
            assert row.tool_access == "manual"
            assert row.tool_top_k == 10
            assert row.strategy == "fixed"

    def test_default_block_shape(self):
        block = default_delegation_settings()
        # The composed fields are NOT block fields (their storage
        # never moved — see the module docstring).
        assert "max_delegations_per_turn" not in block
        assert "per_delegation_cost_budget_usd" not in block
        assert set(block) == {
            "mode", "max_delegation_depth", "max_parallel_delegations",
            "per_delegation_timeout_s", "per_delegation_token_budget",
            "agents",
        }
        assert set(block["agents"]) == {
            "supervisor", "researcher", "planner", "coder"}

    def test_resolver_tolerates_missing_config(self):
        s = resolve_delegation_settings(None)
        assert s.max_delegations_per_turn == 8
        assert s.agent("coder").tool_access == "manual"


# ── Round-trip and migration ──────────────────────────────────────


class TestRoundTripAndMigration:
    def test_round_trip_preserves_block_and_version(self):
        cfg = UserConfig.from_dict({
            "delegation_settings": {
                "mode": "manual",
                "max_delegation_depth": 3,
                "agents": {"coder": {"strategy": "fixed",
                                     "tool_top_k": 25}},
            },
            "delegation_settings_version": 1,
        })
        restored = UserConfig.from_dict(cfg.to_dict())
        assert restored.to_dict()["delegation_settings"] == \
            cfg.to_dict()["delegation_settings"]
        s = resolve_delegation_settings(restored)
        assert s.mode == "manual"
        assert s.max_delegation_depth == 3
        assert s.agent("coder").tool_top_k == 25

    def test_fresh_config_round_trips(self):
        cfg = UserConfig()
        restored = UserConfig.from_dict(cfg.to_dict())
        assert restored.delegation_settings == cfg.delegation_settings
        assert restored.delegation_settings_version == 1

    def test_pre_p1e_config_loads_with_unchanged_behaviour(self):
        # A config file written before P1e: no block, legacy keys
        # carry the user's choices.
        cfg = UserConfig.from_dict({
            "supervisor_delegation_cap": 3,
            "subagent_cost_budget_usd": 0.25,
            "agent_models": {"coder": "openai:gpt-x"},
        })
        s = resolve_delegation_settings(cfg)
        assert s.max_delegations_per_turn == 3
        assert s.per_delegation_cost_budget_usd == 0.25
        # The pin migrated losslessly into the row.
        assert s.agent("coder").model == "openai:gpt-x"
        assert s.agent("coder").model_kind == "pin"
        # The flat fields themselves are untouched.
        assert cfg.supervisor_delegation_cap == 3
        assert cfg.subagent_cost_budget_usd == 0.25

    def test_migration_function_directly(self):
        block = migrate_delegation_settings(
            None, agent_models={"researcher": "cheap"})
        assert block["agents"]["researcher"]["model"] == "cheap"
        assert block["mode"] == "auto-coordinate"

    def test_agent_models_wins_over_stale_row(self):
        cfg = UserConfig.from_dict({
            "delegation_settings": {
                "agents": {"coder": {"model": "openai:old"}}},
            "agent_models": {"coder": "openai:new"},
        })
        assert resolve_delegation_settings(cfg).agent("coder").model \
            == "openai:new"

    def test_newer_stored_version_is_kept(self):
        cfg = UserConfig.from_dict({"delegation_settings_version": 99})
        assert cfg.delegation_settings_version == 99
        assert resolve_delegation_settings(cfg).version == 99

    def test_sync_agent_model_row(self):
        cfg = UserConfig()
        sync_agent_model_row(cfg, "coder", "anthropic:claude-x")
        assert cfg.delegation_settings["agents"]["coder"]["model"] \
            == "anthropic:claude-x"
        # Unknown agents have no row; the call is a no-op.
        sync_agent_model_row(cfg, "nobody", "openai:x")
        assert "nobody" not in cfg.delegation_settings["agents"]


# ── Fail-safe cleaning ────────────────────────────────────────────


class TestCleaning:
    def test_corrupt_block_yields_defaults(self):
        for raw in (None, "junk", 42, ["mode"]):
            assert clean_delegation_settings(raw) == \
                default_delegation_settings()

    def test_unknown_mode_and_strategy_fall_back(self):
        block = clean_delegation_settings({
            "mode": "teleport",
            "agents": {"coder": {"strategy": "yolo",
                                 "tool_access": "everything"}},
        })
        assert block["mode"] == "auto-coordinate"
        assert block["agents"]["coder"]["strategy"] == "fixed"
        assert block["agents"]["coder"]["tool_access"] == "manual"

    def test_numerics_clamp_into_bounds(self):
        block = clean_delegation_settings({
            "max_delegation_depth": 10_000,
            "max_parallel_delegations": -3,
            "agents": {"planner": {"tool_top_k": 0}},
        })
        assert block["max_delegation_depth"] == 8
        assert block["max_parallel_delegations"] == 1
        assert block["agents"]["planner"]["tool_top_k"] == 1

    def test_composed_keys_and_unknown_agents_dropped(self):
        block = clean_delegation_settings({
            "max_delegations_per_turn": 99,
            "per_delegation_cost_budget_usd": 5.0,
            "agents": {"ghost": {"model": "openai:x"}},
        })
        assert "max_delegations_per_turn" not in block
        assert "per_delegation_cost_budget_usd" not in block
        assert "ghost" not in block["agents"]

    def test_delegate_to_cleaned(self):
        block = clean_delegation_settings({
            "agents": {"supervisor": {
                "delegate_to": ["coder", "coder", "supervisor", "ghost"]}},
        })
        assert block["agents"]["supervisor"]["delegate_to"] == ["coder"]

    def test_corrupt_config_file_still_loads(self):
        cfg = UserConfig.from_dict({
            "delegation_settings": {"mode": 42, "agents": "nope"},
            "delegation_settings_version": "soon",
        })
        s = resolve_delegation_settings(cfg)
        assert s.mode == "auto-coordinate"
        assert s.version == 1


# ── Honest availability ───────────────────────────────────────────


class TestAvailability:
    def test_mode_availability(self):
        assert mode_availability("off")[0] is True
        assert mode_availability("manual")[0] is True
        assert mode_availability("auto-coordinate")[0] is True
        # Both flips have landed: P1a/P1b (K-08/K-09) flipped
        # auto-route, P2b (K-12) flipped auto-broadcast — every
        # mode in the vocabulary is honestly available now. The
        # falls-back-with-reason mechanism is still exercised by
        # the strategy and tool-access tables below.
        assert mode_availability("auto-route")[0] is True
        assert mode_availability("auto-broadcast")[0] is True

    def test_auto_broadcast_mode_is_effective(self):
        cfg = UserConfig.from_dict(
            {"delegation_settings": {"mode": "auto-broadcast"}})
        s = resolve_delegation_settings(cfg)
        assert s.mode == "auto-broadcast"
        assert s.effective_mode == "auto-broadcast"
        assert not s.mode_unavailable_reason

    def test_auto_route_mode_is_effective(self):
        cfg = UserConfig.from_dict(
            {"delegation_settings": {"mode": "auto-route"}})
        s = resolve_delegation_settings(cfg)
        assert s.mode == "auto-route"
        assert s.effective_mode == "auto-route"
        assert not s.mode_unavailable_reason

    def test_strategy_and_tool_access_availability(self):
        assert strategy_availability("fixed")[0] is True
        assert strategy_availability("tiered-cascade")[0] is False
        assert tool_access_availability("manual")[0] is True
        assert tool_access_availability("all-allowed")[0] is True
        available, reason = tool_access_availability("auto")
        assert available is False and "Filter 2" in reason


# ── Strict write path ─────────────────────────────────────────────


class TestStrictValidation:
    def test_partial_write_fills_defaults(self):
        block = validate_delegation_settings({"mode": "off"})
        assert block["mode"] == "off"
        assert block["max_delegation_depth"] == 2
        assert block["agents"]["coder"]["tool_access"] == "manual"

    def test_bad_enum_refused_naming_the_field(self):
        with pytest.raises(ValueError, match="delegation_settings.mode"):
            validate_delegation_settings({"mode": "teleport"})

    def test_out_of_range_refused(self):
        with pytest.raises(ValueError, match="max_delegation_depth"):
            validate_delegation_settings({"max_delegation_depth": 0})

    def test_unknown_key_refused(self):
        with pytest.raises(ValueError, match="unknown keys"):
            validate_delegation_settings({"surprise": 1})

    def test_composed_key_refused_with_directions(self):
        with pytest.raises(ValueError, match="supervisor_delegation_cap"):
            validate_delegation_settings({"max_delegations_per_turn": 4})

    def test_unknown_agent_and_self_delegation_refused(self):
        with pytest.raises(ValueError, match="unknown agents"):
            validate_delegation_settings({"agents": {"ghost": {}}})
        with pytest.raises(ValueError, match="itself"):
            validate_delegation_settings(
                {"agents": {"coder": {"delegate_to": ["coder"]}}})

    def test_handed_off_updates_integration(self):
        cleaned = validate_handed_off_updates(
            {"delegation_settings": {"mode": "manual"}})
        assert cleaned["delegation_settings"]["mode"] == "manual"
        with pytest.raises(ValueError):
            validate_handed_off_updates(
                {"delegation_settings": {"mode": "nope"}})


# ── The single read path ──────────────────────────────────────────


class TestSingleReadPath:
    def test_supervisor_cap_resolves_through_schema(self):
        from aether_engine.langgraph.supervisor_model import (
            _delegation_cap,
        )

        cfg = {"configurable": {
            "user_config": UserConfig(supervisor_delegation_cap=3)}}
        assert _delegation_cap(cfg) == 3
        assert _delegation_cap({"configurable": {}}) == 8

    def test_spawn_policy_uses_schema_cap(self):
        from aether_engine.langgraph.supervisor_model import (
            _spawn_policy_denial,
        )

        def _config(user_config):
            return {"configurable": {"user_config": user_config}}

        state = {"messages": [], "delegation_count": 2}
        denial = _spawn_policy_denial(
            state, "coder",
            _config(UserConfig(supervisor_delegation_cap=2)))
        assert denial is not None
        assert _spawn_policy_denial(
            state, "coder",
            _config(UserConfig(supervisor_delegation_cap=3))) is None

    def test_s13_cost_budget_resolves_through_schema(self):
        from aether_engine.langgraph.subagents import subagent_settings

        settings = subagent_settings(
            UserConfig(subagent_cost_budget_usd=0.5))
        assert settings.cost_budget_usd == 0.5
        # S13's own bounds are untouched by the schema.
        assert settings.timeout_s == 120
        assert settings.token_budget == 20000


# ── Tier aliases ──────────────────────────────────────────────────


class TestTierAliases:
    def test_classification(self):
        assert classify_model_value("inherit") == "inherit"
        assert classify_model_value("") == "inherit"
        assert classify_model_value(None) == "inherit"
        for alias in ("strong", "balanced", "cheap", "local"):
            assert classify_model_value(alias) == "alias"
            assert is_tier_alias(alias)
        assert classify_model_value("openai:gpt-x") == "pin"
        assert classify_model_value("gpt-x") == "pin"
        assert not is_tier_alias("stronger")

    def test_pick_alias_candidate_tiers(self):
        from aether_engine.langgraph.model_routing import (
            Candidate,
            History,
            pick_alias_candidate,
        )

        pool = [
            Candidate("openai", "big", 2, ("chat", "reasoning")),
            Candidate("openai", "mid", 1, ("chat",)),
            Candidate("google_gemini", "mini", 0, ("chat", "fast")),
            Candidate("ollama", "local-7b", 0, ("chat", "fast")),
        ]
        history = History()
        assert pick_alias_candidate(
            "strong", pool, history, "coder").model == "big"
        assert pick_alias_candidate(
            "balanced", pool, history, "coder").model == "mid"
        assert pick_alias_candidate(
            "cheap", pool, history, "coder").model == "mini"
        local = pick_alias_candidate("local", pool, history, "coder")
        assert (local.provider, local.model) == ("ollama", "local-7b")
        assert local.source == "alias"

    def test_pick_alias_candidate_empty_and_no_local(self):
        from aether_engine.langgraph.model_routing import (
            Candidate,
            History,
            pick_alias_candidate,
        )

        assert pick_alias_candidate("strong", [], History(), "coder") \
            is None
        pool = [Candidate("openai", "mid", 1, ("chat",))]
        assert pick_alias_candidate("local", pool, History(), "coder") \
            is None
        # A missing tier falls to the nearest available tier.
        assert pick_alias_candidate(
            "strong", pool, History(), "coder").model == "mid"

    def test_alias_pin_is_never_auto_routed(self):
        # The runner captures _pin_present from the raw value before
        # resolution; an alias is a pin-class directive. Pinned here
        # at the classification level the runner consults.
        row = resolve_delegation_settings(
            UserConfig(agent_models={"coder": "strong"})).agent("coder")
        assert row.model == "strong"
        assert row.model_kind == "alias"


# ── Runner tool_access modes (through the specialist node) ────────


class _CapturingProvider:
    """Streams fixed text; records the tools kwarg (K-02's shape)."""

    def __init__(self, text="done"):
        self.text = text
        self.seen_tools = None

    async def call_stream(self, messages, tools=None, node_name=None):
        self.seen_tools = tools
        yield SimpleNamespace(
            text=self.text, tool_calls=None, finish_reason="stop")


def _runner_state():
    return {
        "original_prompt": "Do the task",
        "messages": [{"role": "user", "content": "Do the task"}],
        "plan": "",
        "current_phase": "",
        "artifact_paths": {},
        "tool_results_summary": {},
        "pending_tool_call": None,
        "queued_tool_calls": [],
        "active_specialist": None,
        "tool_call_signature": None,
        "tool_call_repeats": 0,
        "delegation_log": [],
        "task_id": "p1e-test",
        "session_id": "p1e-session",
        "context_briefing": None,
        "active_skills": [],
        "blackboard": {},
        "plan_mode": False,
    }


def _runner_config(provider, user_config, **extra):
    from aether_engine.tools.registry import build_tool_registry

    configurable = {"provider": provider,
                    "tool_registry": build_tool_registry(),
                    "user_config": user_config}
    configurable.update(extra)
    return {"configurable": configurable}


def _offered(provider):
    return [d["function"]["name"] for d in provider.seen_tools
            if d["function"]["name"] != "spawn_subagent"]


@pytest.fixture(autouse=True)
def _no_artifacts(monkeypatch):
    from aether_engine.langgraph.nodes import researcher as _researcher

    monkeypatch.setattr(
        _researcher, "save_artifact",
        lambda task_id, kind, filename, content: f"/tmp/{filename}")


class TestRunnerToolAccessModes:
    @pytest.mark.asyncio
    async def test_manual_mode_applies_the_legacy_list(self):
        from aether_engine.langgraph.nodes import researcher as _r

        provider = _CapturingProvider()
        await _r.researcher_node(
            _runner_state(),
            _runner_config(provider, UserConfig(),
                           agent_tools={"researcher": ["read_file"]}))
        assert _offered(provider) == ["read_file"]

    @pytest.mark.asyncio
    async def test_all_allowed_mode_sets_the_list_aside(self):
        from aether_engine.langgraph.nodes import researcher as _r

        cfg = UserConfig(delegation_settings={
            "agents": {"researcher": {"tool_access": "all-allowed"}}})
        provider = _CapturingProvider()
        await _r.researcher_node(
            _runner_state(),
            _runner_config(provider, cfg,
                           agent_tools={"researcher": ["read_file"]}))
        offered = _offered(provider)
        # Ceiling only: the full read set is offered despite the
        # one-tool legacy list; write tools stay ceiling-excluded.
        assert "read_file" in offered and "search_web" in offered
        assert "write_file" not in offered

    @pytest.mark.asyncio
    async def test_auto_mode_falls_back_to_manual_semantics(self):
        from aether_engine.langgraph.nodes import researcher as _r

        cfg = UserConfig(delegation_settings={
            "agents": {"researcher": {"tool_access": "auto"}}})
        provider = _CapturingProvider()
        await _r.researcher_node(
            _runner_state(),
            _runner_config(provider, cfg,
                           agent_tools={"researcher": ["read_file"]}))
        # Filter 2 has not landed: the manual list applies (and the
        # runner logs the gap) rather than the mode silently
        # widening or narrowing the offer.
        assert _offered(provider) == ["read_file"]
