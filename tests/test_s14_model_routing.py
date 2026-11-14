"""Master build S14: automatic per-agent model routing + escalation.

Audit G4/G5: a specialist's model was a static pin lookup — the
``agent_models`` pin or the run's provider — and no failure
anywhere stepped up to a stronger model. S14 adds the router
(``langgraph/model_routing.py``) that decides what ``"inherit"``
resolves to per delegation when the user turns it on, plus the
one-step escalation when an auto-routed run fails.

The contract these tests pin:

* pins always win (any non-"inherit" ``agent_models`` value), in
  both modes, and are never escalated;
* "off" (the default) is byte-identical to the pre-S14 behaviour:
  the resolver is never consulted, the run provider serves, and
  the specialist's prompt is unchanged;
* an image turn never routes to a text-only model; an
  unconfigured or unavailable provider is never chosen;
  ``gemini-2.5-flash`` (404 on the owner's key) is never chosen;
* with no recorded history the static tier table decides; with
  history, a model with a losing record for the agent loses to a
  reliable one, and recorded per-call cost breaks ties;
* escalation fires ONCE on a specialist failure in auto mode —
  never when off, never when pinned, never for a non-provider
  error, never after partial output already streamed — and the
  S13 child retry carries the step-up flag;
* every decision lands in the S3 Task Ledger, whose summary
  names the model that ran each delegation.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from aether_common.contracts import Event, EventType
from aether_engine import task_ledger
from aether_engine.config import UserConfig
from aether_engine.config_updates import validate_handed_off_updates
from aether_engine.cost.ledger import UsageLedger
from aether_engine.event_bus import event_bus
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph import model_routing
from aether_engine.langgraph import subagents as subagents_module
from aether_engine.langgraph.model_routing import (
    History,
    ModelOutcomeStore,
    TaskSignals,
    collect_candidates,
    escalate,
    record_model_outcome,
    route,
    tier_for_model,
)
from aether_engine.langgraph.nodes import coder as coder_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.nodes import researcher as researcher_module
from aether_engine.langgraph.nodes.coder import CODER_SPEC
from aether_engine.langgraph.nodes.runner import run_specialist
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.orchestration import core as orchestration_core
from aether_engine.providers.base import NetworkError, StreamChunk
from aether_engine.routing.registry import ModelRegistry
from aether_engine.state import engine_state
from aether_engine.tools.registry import ToolRegistry

# ── Fakes ─────────────────────────────────────────────────────────


class FakeHealth:
    def __init__(self, unavailable=()):
        self._unavailable = set(unavailable)

    def is_provider_available(self, provider):
        return provider not in self._unavailable


class FakeProvider:
    """A scripted provider with a routable identity.

    ``script`` is a list of per-call behaviours: a string yields
    that text; an exception instance raises it; a list yields its
    chunks then raises/returns per its last element if it is an
    exception.
    """

    def __init__(self, provider_name, model, script=()):
        self.provider_name = provider_name
        self.model = model
        self._script = list(script)
        self.calls = []  # (messages, tools, node_name)

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append((messages, tools, node_name))
        behaviour = self._script.pop(0) if self._script else "done"
        if isinstance(behaviour, Exception):
            raise behaviour
        if isinstance(behaviour, list):
            for item in behaviour:
                if isinstance(item, Exception):
                    raise item
                yield item
            return
        yield StreamChunk(text=str(behaviour), finish_reason="stop")


def _registry():
    return ModelRegistry()


def _candidates(configured=("google_gemini",), health=None,
                incumbent=("google_gemini", "gemini-3.5-flash"), **kw):
    return collect_candidates(
        registry=_registry(),
        configured_providers=list(configured),
        health_manager=health or FakeHealth(),
        incumbent_provider=incumbent[0],
        incumbent_model=incumbent[1],
        **kw)


# ── Config + validation ───────────────────────────────────────────


def test_model_routing_config_defaults_and_round_trip():
    cfg = UserConfig()
    assert cfg.model_routing == "off"
    assert UserConfig.from_dict({"model_routing": "auto"}).model_routing == "auto"
    assert UserConfig.from_dict({"model_routing": "banana"}).model_routing == "off"
    assert UserConfig.from_dict({}).model_routing == "off"
    assert UserConfig(model_routing="auto").to_dict()["model_routing"] == "auto"


def test_model_routing_update_validation_is_strict():
    assert validate_handed_off_updates(
        {"model_routing": "auto"}) == {"model_routing": "auto"}
    assert validate_handed_off_updates(
        {"model_routing": "off"}) == {"model_routing": "off"}
    with pytest.raises(ValueError, match="model_routing"):
        validate_handed_off_updates({"model_routing": "banana"})
    with pytest.raises(ValueError, match="model_routing"):
        validate_handed_off_updates({"model_routing": True})


# ── The pure router ───────────────────────────────────────────────


def test_tiers_come_from_the_registry_capabilities():
    registry = _registry()
    assert tier_for_model("google_gemini", "gemini-3.5-flash", registry) == 0
    assert tier_for_model("google_gemini", "gemini-2.5-pro", registry) == 2
    assert tier_for_model("anthropic", "claude-3-5-sonnet", registry) == 2
    assert tier_for_model("deepseek", "deepseek-chat", registry) == 0


def test_standard_task_keeps_the_incumbent_without_history():
    choice = route(
        TaskSignals(agent="coder", task_text="update the readme links please"),
        _candidates(), History())
    assert (choice.provider, choice.model) == ("google_gemini", "gemini-3.5-flash")
    assert choice.source == "auto"


def test_short_task_picks_small_tier_and_never_the_unusable_model():
    # Both gemini flashes are small tier; 2.5-flash must never be
    # chosen (it 404s on the owner's key), so the short task lands
    # on 3.5-flash or 2.0-flash — deterministically the incumbent
    # on an honest tie.
    choice = route(
        TaskSignals(agent="researcher", task_text="capital of France?"),
        _candidates(), History())
    assert choice.model != "gemini-2.5-flash"
    assert choice.tier == 0


def test_reasoning_task_steps_up_to_the_strong_tier():
    choice = route(
        TaskSignals(agent="planner",
                    task_text="Prove the derivation, step by step, and check every case"),
        _candidates(), History())
    assert (choice.provider, choice.model) == ("google_gemini", "gemini-2.5-pro")
    assert choice.tier == 2


def test_image_turn_never_routes_to_a_text_only_model():
    candidates = _candidates(
        configured=("google_gemini", "deepseek"),
        incumbent=("deepseek", "deepseek-chat"))
    choice = route(
        TaskSignals(agent="researcher", task_text="what is in this picture?",
                    has_images=True),
        candidates, History())
    assert choice is not None
    assert "vision" in (
        _registry().get_model(choice.model, provider=choice.provider).capabilities)


def test_unconfigured_and_unavailable_providers_are_never_chosen():
    candidates = _candidates(configured=("google_gemini",))
    assert {c.provider for c in candidates} == {"google_gemini"}

    candidates = _candidates(
        configured=("google_gemini", "openai"),
        health=FakeHealth(unavailable={"openai"}),
        incumbent=("google_gemini", "gemini-3.5-flash"))
    assert {c.provider for c in candidates} == {"google_gemini"}


def test_local_only_pool_excludes_cloud_candidates():
    candidates = _candidates(
        configured=("google_gemini", "ollama"), local_only=True)
    non_incumbent = [c for c in candidates if not c.is_incumbent]
    assert all(c.provider == "ollama" for c in non_incumbent)


def test_empty_pool_routes_nowhere():
    assert route(TaskSignals(agent="coder"), [], History()) is None


def test_history_demotion_a_losing_model_loses_to_a_reliable_one():
    # The researcher carries no capability want, so the small
    # tier competes on record alone. (For the coder the pool
    # first narrows to code-capable models — the registry marks
    # 2.0-flash without "code" — by design.)
    history = History(outcomes={
        ("google_gemini", "gemini-3.5-flash", "researcher"): (0, 4),
        ("google_gemini", "gemini-2.0-flash", "researcher"): (5, 0),
    })
    choice = route(
        TaskSignals(agent="researcher", task_text="fix it"),
        _candidates(), history)
    assert choice.model == "gemini-2.0-flash"
    assert "failed 4 of 4" in choice.reason


def test_history_cost_breaks_ties_toward_the_cheaper_recorded_model():
    history = History(usage={
        ("google_gemini", "gemini-3.5-flash"): {
            "calls": 10, "cost_usd": 1.0, "total_tokens": 5000},
        ("google_gemini", "gemini-2.0-flash"): {
            "calls": 10, "cost_usd": 0.02, "total_tokens": 5000},
    })
    choice = route(
        TaskSignals(agent="researcher", task_text="look this up for me please"),
        _candidates(), history)
    assert choice.model == "gemini-2.0-flash"


def test_escalate_steps_one_tier_up_and_stops_at_the_top():
    candidates = _candidates()
    history = History()
    signals = TaskSignals(agent="coder", task_text="fix the bug")
    step = escalate(signals, candidates, history,
                    current_provider="google_gemini",
                    current_model="gemini-3.5-flash", current_tier=0)
    assert step is not None
    assert (step.provider, step.model) == ("google_gemini", "gemini-2.5-pro")
    assert step.source == "escalation"
    assert step.escalated_from == "google_gemini:gemini-3.5-flash"
    assert escalate(signals, candidates, history,
                    current_provider="google_gemini",
                    current_model="gemini-2.5-pro", current_tier=2) is None


# ── Recorded history ──────────────────────────────────────────────


def test_usage_ledger_model_totals(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.db")
    ledger.record_call(model_name="m-a", provider_name="p1",
                       input_tokens=10, output_tokens=5, cost_usd=0.25)
    ledger.record_call(model_name="m-a", provider_name="p1",
                       input_tokens=10, output_tokens=5, cost_usd=0.25)
    ledger.record_call(model_name="m-a", provider_name="p2",
                       input_tokens=1, output_tokens=1, cost_usd=0.5)
    totals = ledger.model_usage_totals()
    assert totals[("p1", "m-a")] == {
        "calls": 2, "cost_usd": 0.5, "total_tokens": 30}
    assert totals[("p2", "m-a")]["calls"] == 1


def test_outcome_store_records_and_aggregates(tmp_path):
    store = ModelOutcomeStore(tmp_path / "routing.db")
    record_model_outcome("p", "m", "coder", "success", store=store)
    record_model_outcome("p", "m", "coder", "failure", store=store)
    record_model_outcome("p", "m", "coder", "failure", store=store)
    record_model_outcome(None, "m", "coder", "failure", store=store)  # no identity: skipped
    assert store.stats() == {("p", "m", "coder"): (1, 2)}


# ── The runner seam ───────────────────────────────────────────────


@pytest.fixture
def routing_env(tmp_path, monkeypatch):
    """Isolated routing inputs for direct run_specialist calls."""
    store = ModelOutcomeStore(tmp_path / "routing.db")
    monkeypatch.setattr(model_routing, "_GLOBAL_OUTCOME_STORE", store)
    monkeypatch.setattr(engine_state, "configured_providers", ["google_gemini"])
    monkeypatch.setattr(
        model_routing, "collect_history",
        lambda candidates, **kw: History())
    factory_calls = []

    def _factory(provider_name, model, secret_store, **kw):
        factory_calls.append((provider_name, model))
        return factory_providers[(provider_name, model)]

    factory_providers = {}
    monkeypatch.setattr(
        orchestration_core, "_create_provider_instance", _factory)
    return {
        "store": store,
        "factory_calls": factory_calls,
        "factory_providers": factory_providers,
    }


def _specialist_config(run_provider, user_config, **configurable):
    base = {
        "provider": run_provider,
        "tool_registry": None,
        "agent_models": {},
        "agent_tools": {},
        "secret_store": object(),
        "user_config": user_config,
    }
    base.update(configurable)
    return {"configurable": base}


def _specialist_state(**overrides):
    state = {
        "messages": [{"role": "user", "content": "do the work"}],
        "original_prompt": "do the work",
        "task_id": "s14-test-task",
        "session_id": "s14-test-session",
    }
    state.update(overrides)
    return state


async def _run_coder(provider, user_config, state=None, **configurable):
    return await run_specialist(
        CODER_SPEC, state or _specialist_state(),
        _specialist_config(provider, user_config, **configurable),
        save_artifact=lambda *a, **k: "s14/code.txt")


@pytest.mark.asyncio
async def test_routing_off_is_byte_identical_and_never_constructs(routing_env):
    run = FakeProvider("google_gemini", "gemini-3.5-flash", ["the answer"])
    result = await _run_coder(run, UserConfig(model_routing="off"))
    assert routing_env["factory_calls"] == []
    assert result["messages"] == [{"role": "assistant", "content": "the answer"}]
    off_messages = run.calls[0][0]

    run2 = FakeProvider("google_gemini", "gemini-3.5-flash", ["the answer"])
    result2 = await _run_coder(run2, UserConfig(model_routing="auto"))
    # Auto with an empty history keeps the incumbent: same provider,
    # and the specialist saw byte-identical messages.
    assert routing_env["factory_calls"] == []
    assert run2.calls[0][0] == off_messages
    assert result2["messages"] == result["messages"]


@pytest.mark.asyncio
async def test_auto_routing_serves_the_routed_model(routing_env):
    strong = FakeProvider("google_gemini", "gemini-2.5-pro", ["deep answer"])
    routing_env["factory_providers"][("google_gemini", "gemini-2.5-pro")] = strong
    run = FakeProvider("google_gemini", "gemini-3.5-flash", ["shallow"])
    state = _specialist_state(delegation_log=[{
        "agent": "supervisor", "timestamp": 1.0,
        "decision": {
            "next": "coder",
            "reason": "Prove the derivation, step by step",
            "briefing": "",
        },
    }])
    result = await _run_coder(run, UserConfig(model_routing="auto"), state=state)
    assert routing_env["factory_calls"] == [("google_gemini", "gemini-2.5-pro")]
    assert run.calls == []
    assert strong.calls and strong.calls[0][2] == "coder"
    assert result["messages"] == [{"role": "assistant", "content": "deep answer"}]


@pytest.mark.asyncio
async def test_pin_beats_routing_in_auto_mode(routing_env):
    pinned = FakeProvider("openai", "gpt-4o", ["pinned answer"])
    routing_env["factory_providers"][("openai", "gpt-4o")] = pinned
    run = FakeProvider("google_gemini", "gemini-3.5-flash", ["run answer"])
    state = _specialist_state(delegation_log=[{
        "agent": "supervisor", "timestamp": 1.0,
        "decision": {
            "next": "coder",
            "reason": "Prove the derivation, step by step",
            "briefing": "",
        },
    }])
    result = await _run_coder(
        run, UserConfig(model_routing="auto"), state=state,
        agent_models={"coder": "openai:gpt-4o"})
    # Exactly one construction: the pin. Routing would have picked
    # gemini-2.5-pro for these signals — it was never consulted.
    assert routing_env["factory_calls"] == [("openai", "gpt-4o")]
    assert result["messages"] == [{"role": "assistant", "content": "pinned answer"}]


@pytest.mark.asyncio
async def test_escalation_fires_once_on_provider_failure(routing_env):
    strong = FakeProvider("google_gemini", "gemini-2.5-pro", ["recovered"])
    routing_env["factory_providers"][("google_gemini", "gemini-2.5-pro")] = strong
    run = FakeProvider(
        "google_gemini", "gemini-3.5-flash",
        [NetworkError("connection lost")])
    queue: asyncio.Queue = asyncio.Queue()
    event_bus.subscribe("s14-test-task", queue)
    try:
        result = await _run_coder(run, UserConfig(model_routing="auto"))
    finally:
        event_bus.unsubscribe("s14-test-task", queue)
    assert result["messages"] == [{"role": "assistant", "content": "recovered"}]
    assert routing_env["factory_calls"] == [("google_gemini", "gemini-2.5-pro")]
    stats = routing_env["store"].stats()
    assert stats[("google_gemini", "gemini-3.5-flash", "coder")] == (0, 1)
    assert stats[("google_gemini", "gemini-2.5-pro", "coder")] == (1, 0)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    escalations = [
        ev for ev in events
        if ev.type == EventType.NODE_ACTIVITY
        and ev.payload.get("action") == "model_escalation"]
    assert len(escalations) == 1
    assert escalations[0].payload["output_payload"]["model"] == "gemini-2.5-pro"
    assert escalations[0].payload["output_payload"]["escalated_from"] == \
        "google_gemini:gemini-3.5-flash"


@pytest.mark.asyncio
async def test_escalation_never_fires_when_off_pinned_or_wrong_error(routing_env):
    # Off: the failure propagates untouched.
    run = FakeProvider("google_gemini", "gemini-3.5-flash",
                       [NetworkError("down")])
    with pytest.raises(NetworkError):
        await _run_coder(run, UserConfig(model_routing="off"))
    assert routing_env["factory_calls"] == []

    # Pinned: the pin's failure is the user's chosen model's
    # failure — it propagates, no step up is constructed.
    pinned = FakeProvider("openai", "gpt-4o", [NetworkError("down")])
    routing_env["factory_providers"][("openai", "gpt-4o")] = pinned
    run2 = FakeProvider("google_gemini", "gemini-3.5-flash", ["unused"])
    with pytest.raises(NetworkError):
        await _run_coder(run2, UserConfig(model_routing="auto"),
                         agent_models={"coder": "openai:gpt-4o"})
    assert routing_env["factory_calls"] == [("openai", "gpt-4o")]

    # A non-provider error is a bug, not a model outcome: it
    # propagates and records nothing. (The two provider failures
    # above DID record — outcomes are kept for every specialist
    # round, in every mode; that record is the router's history.)
    stats_before = routing_env["store"].stats()
    run3 = FakeProvider("google_gemini", "gemini-3.5-flash",
                        [ValueError("bug in the provider shim")])
    with pytest.raises(ValueError):
        await _run_coder(run3, UserConfig(model_routing="auto"))
    assert routing_env["factory_calls"] == [("openai", "gpt-4o")]
    assert routing_env["store"].stats() == stats_before


@pytest.mark.asyncio
async def test_escalation_stops_after_one_step_and_after_partial_output(routing_env):
    # The escalated model failing too ends the ladder: the second
    # failure propagates, and exactly one escalation was built.
    strong = FakeProvider("google_gemini", "gemini-2.5-pro",
                          [NetworkError("also down")])
    routing_env["factory_providers"][("google_gemini", "gemini-2.5-pro")] = strong
    run = FakeProvider("google_gemini", "gemini-3.5-flash",
                       [NetworkError("down")])
    with pytest.raises(NetworkError):
        await _run_coder(run, UserConfig(model_routing="auto"))
    assert routing_env["factory_calls"] == [("google_gemini", "gemini-2.5-pro")]
    stats = routing_env["store"].stats()
    assert stats[("google_gemini", "gemini-2.5-pro", "coder")] == (0, 1)

    # Partial output already streamed: no restart on another model.
    routing_env["factory_calls"].clear()
    run2 = FakeProvider(
        "google_gemini", "gemini-3.5-flash",
        [[StreamChunk(text="partial answer"), NetworkError("mid-stream")]])
    with pytest.raises(NetworkError):
        await _run_coder(run2, UserConfig(model_routing="auto"))
    assert routing_env["factory_calls"] == []


@pytest.mark.asyncio
async def test_structured_validation_failure_escalates_the_correction(routing_env):
    strong = FakeProvider("google_gemini", "gemini-2.5-pro",
                          ['{"summary": "recovered"}'])
    routing_env["factory_providers"][("google_gemini", "gemini-2.5-pro")] = strong
    run = FakeProvider("google_gemini", "gemini-3.5-flash",
                       ["not json", "still not json"])
    user_config = UserConfig(
        model_routing="auto",
        agent_output_schemas={
            "coder": {"type": "object", "required": ["summary"]}})
    result = await _run_coder(run, user_config)
    assert result["messages"] == [
        {"role": "assistant", "content": '{"summary": "recovered"}'}]
    assert result["structured_outputs"]["coder"] == {"summary": "recovered"}
    stats = routing_env["store"].stats()
    assert stats[("google_gemini", "gemini-3.5-flash", "coder")] == (0, 1)
    assert stats[("google_gemini", "gemini-2.5-pro", "coder")] == (1, 0)

    # Off: the same script fails loudly, exactly as before S14.
    run2 = FakeProvider("google_gemini", "gemini-3.5-flash",
                        ["not json", "still not json"])
    result2 = await _run_coder(run2, UserConfig(
        model_routing="off",
        agent_output_schemas={
            "coder": {"type": "object", "required": ["summary"]}}))
    assert "Structured output validation failed" in result2["messages"][0]["content"]
    assert "error" in result2["structured_outputs"]["coder"]


@pytest.mark.asyncio
async def test_subagent_retry_flag_steps_the_child_up(routing_env):
    # The S13 retry seam sets model_routing_escalate in the child
    # config; the child's own resolution steps one tier above its
    # fresh (neutral-signal) decision.
    strong = FakeProvider("google_gemini", "gemini-2.5-pro", ["child answer"])
    routing_env["factory_providers"][("google_gemini", "gemini-2.5-pro")] = strong
    run = FakeProvider("google_gemini", "gemini-3.5-flash", ["unused"])
    result = await _run_coder(
        run, UserConfig(model_routing="auto"),
        is_subagent_run=True, model_routing_escalate=True)
    assert routing_env["factory_calls"] == [("google_gemini", "gemini-2.5-pro")]
    assert result["messages"] == [{"role": "assistant", "content": "child answer"}]


# ── The Settings report + WS surface ──────────────────────────────


def test_effective_models_report_shapes(monkeypatch):
    monkeypatch.setattr(engine_state, "configured_providers", ["google_gemini"])
    off = model_routing.effective_models_report(UserConfig())
    assert off["mode"] == "off"
    assert [a["agent"] for a in off["agents"]] == [
        "supervisor", "researcher", "planner", "coder"]
    assert all(a["source"] == "run_default" for a in off["agents"])

    auto = model_routing.effective_models_report(UserConfig(
        model_routing="auto", agent_models={"coder": "openai:gpt-4o"}))
    by_agent = {a["agent"]: a for a in auto["agents"]}
    assert by_agent["coder"]["source"] == "pinned"
    assert by_agent["coder"]["model"] == "gpt-4o"
    assert by_agent["supervisor"]["source"] == "run_default"
    assert by_agent["researcher"]["source"] == "auto"
    assert by_agent["researcher"]["model"] == "gemini-3.5-flash"
    assert by_agent["researcher"]["reason"]


def test_routing_request_is_dispatched():
    from aether_engine.routes import websocket

    assert websocket._DISPATCH[EventType.AGENT_ROUTING_REQUEST] is \
        websocket._handle_agent_routing_request


def test_routing_handler_answers_with_the_report(monkeypatch):
    from aether_engine.routes import websocket

    monkeypatch.setattr(
        model_routing, "effective_models_report",
        lambda user_config: {"mode": "auto", "agents": []})

    class _FakeSocket:
        def __init__(self):
            self.sent = []

        async def send_text(self, data):
            import json
            self.sent.append(json.loads(data))

    from types import SimpleNamespace

    socket = _FakeSocket()
    conn = SimpleNamespace(ws=socket)
    msg = Event(type=EventType.AGENT_ROUTING_REQUEST,
                request_id="r14", payload={})
    asyncio.run(websocket._handle_agent_routing_request(conn, msg))
    assert socket.sent[0]["type"] == "AGENT_ROUTING_RESPONSE"
    assert socket.sent[0]["payload"] == {"mode": "auto", "agents": []}


# ── Ledger: the summary names the model that ran ──────────────────


class JourneyProvider:
    """Supervisor delegates to coder once, then ends (S12/S13 harness)."""

    provider_name = "google_gemini"
    model = "gemini-3.5-flash"

    def __init__(self):
        self._supervisor = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "coder":
            yield StreamChunk(text="code written", finish_reason="stop")
            return
        self._supervisor += 1
        text = (
            '{"next": "coder", "reason": "needs code", "briefing": ""}'
            if self._supervisor == 1
            else '{"next": "END", "reason": "done", "briefing": ""}')
        yield StreamChunk(text=text, finish_reason="stop")


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        subagents_module, "SUBAGENT_CHECKPOINT_DB",
        tmp_path / "subagent_checkpoints.db")
    monkeypatch.setattr(episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(planner_module, "save_artifact", lambda *a, **k: "s14/plan.txt")
    monkeypatch.setattr(coder_module, "save_artifact", lambda *a, **k: "s14/code.txt")
    monkeypatch.setattr(researcher_module, "save_artifact", lambda *a, **k: "s14/research.txt")
    store = ModelOutcomeStore(tmp_path / "routing.db")
    monkeypatch.setattr(model_routing, "_GLOBAL_OUTCOME_STORE", store)
    monkeypatch.setattr(engine_state, "configured_providers", ["google_gemini"])
    task_ledger.clear_tap()
    return tmp_path


@pytest.mark.asyncio
async def test_ledger_summary_names_the_model_that_ran(graph_env):
    provider = JourneyProvider()
    events = [ev async for ev in executor_module.run_langgraph_task(
        "please write some code",
        provider,
        tools=ToolRegistry(),
        task_id="task-s14-journey",
        request_id="req-s14-journey",
        session_id="session-s14-journey",
        user_config=UserConfig(model_routing="auto"),
    )]
    assert any(ev.type == EventType.TASK_COMPLETED for ev in events)
    summary = await task_ledger.task_summary("task-s14-journey")
    assert summary["found"]
    delegations = [d for d in summary["delegations"] if d.get("agent") == "coder"]
    assert delegations, summary["delegations"]
    assert delegations[0]["model"] == "gemini-3.5-flash"
    assert delegations[0]["model_provider"] == "google_gemini"
    assert delegations[0]["model_source"] == "auto"
    assert summary["model_routing"]
    assert summary["model_routing"][0]["agent"] == "coder"
    briefing = task_ledger.task_briefing(summary)
    assert "coder on gemini-3.5-flash" in briefing
