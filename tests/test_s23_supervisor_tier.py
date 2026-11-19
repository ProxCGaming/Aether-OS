"""S23 contract tests — P1a (orchestration programme, slice K-08):
the supervisor emits the tier.

The contract under test, end to end across the real seams
(supervisor post-model hook -> delegation log -> specialist
runner -> S14 router -> NODE_ACTIVITY event):

* A delegation whose briefing carries a tier tag produces a
  ``RoutingChoice`` at THAT tier when an eligible candidate
  exists — even when text inference would have chosen a
  different tier — and the published NODE_ACTIVITY payload
  carries the tier source (``tier_source == "explicit"``) with
  the reason naming the supervisor's mark.
* The same delegation WITHOUT the tag routes by inference, as
  before P1a, and its payload says ``tier_source == "inferred"``.
* With model routing "off", the tag is still parsed and logged
  by the hook (the log entry carries the tier, the briefing is
  stripped) but never consulted: the run provider serves and no
  routing decision is published.
* A pin still beats the stated tier in auto mode: routing is
  only ever consulted for the unpinned "inherit" case.

Unit-level pins (parser, hook shapes on both topologies, the
need function, the vision hard filter) live in
``tests/test_p1a_supervisor_tier.py``.
"""
import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from aether_common.contracts import EventType
from aether_engine.config import UserConfig
from aether_engine.event_bus import event_bus
from aether_engine.langgraph import model_routing
from aether_engine.langgraph.model_routing import History, ModelOutcomeStore
from aether_engine.langgraph.nodes.coder import CODER_SPEC
from aether_engine.langgraph.nodes.runner import run_specialist
from aether_engine.langgraph.supervisor_model import supervisor_post_model_hook
from aether_engine.orchestration import core as orchestration_core
from aether_engine.providers.base import StreamChunk
from aether_engine.state import engine_state


class FakeProvider:
    """A scripted provider with a routable identity (S14 style)."""

    def __init__(self, provider_name, model, script=()):
        self.provider_name = provider_name
        self.model = model
        self._script = list(script)
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append((messages, tools, node_name))
        behaviour = self._script.pop(0) if self._script else "done"
        yield StreamChunk(text=str(behaviour), finish_reason="stop")


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
    factory_providers = {}

    def _factory(provider_name, model, secret_store, **kw):
        factory_calls.append((provider_name, model))
        return factory_providers[(provider_name, model)]

    monkeypatch.setattr(
        orchestration_core, "_create_provider_instance", _factory)
    return {"factory_calls": factory_calls,
            "factory_providers": factory_providers}


def _hook_state_for_tagged_delegation(briefing):
    """Run the real post-model hook over a tools-topology
    delegation whose briefing is ``briefing``; return the hook's
    update (the delegation log entry + context_briefing)."""
    decision = {"next": "coder", "reason": "fix it", "briefing": briefing}
    message = AIMessage(
        content="fix it",
        tool_calls=[{"name": "delegate_to_coder",
                     "args": {"briefing": briefing},
                     "id": "call-s23", "type": "tool_call"}],
        additional_kwargs={"aether_decision": decision},
        id="msg-s23",
    )
    state = {"messages": [HumanMessage(content="fix it please"), message],
             "delegation_count": 0}
    return supervisor_post_model_hook(state)


def _specialist_state(hook_update):
    return {
        "messages": [{"role": "user", "content": "fix it please"}],
        "original_prompt": "fix it please",
        "task_id": "s23-test-task",
        "session_id": "s23-test-session",
        "delegation_log": hook_update["delegation_log"],
        "context_briefing": hook_update["context_briefing"],
    }


def _config(run_provider, user_config, **configurable):
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


async def _run_coder(provider, user_config, state, **configurable):
    return await run_specialist(
        CODER_SPEC, state, _config(provider, user_config, **configurable),
        save_artifact=lambda *a, **k: "s23/code.txt")


def _routing_events(queue):
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return [
        ev for ev in events
        if ev.type == EventType.NODE_ACTIVITY
        and ev.payload.get("action") in ("model_routing", "model_escalation")
    ]


@pytest.mark.asyncio
async def test_tagged_delegation_routes_at_the_stated_tier(routing_env):
    """The delegation text is short and simple — inference alone
    would keep the small-tier incumbent. The supervisor marked it
    strong, so the strong model is constructed and serves, and
    the published decision names the mark as its source."""
    strong = FakeProvider("google_gemini", "gemini-2.5-pro", ["deep answer"])
    routing_env["factory_providers"][("google_gemini", "gemini-2.5-pro")] = strong
    run = FakeProvider("google_gemini", "gemini-3.5-flash", ["shallow"])

    update = _hook_state_for_tagged_delegation("[tier: strong] the facts")
    logged = update["delegation_log"][0]["decision"]
    assert logged["tier"] == "strong"
    assert logged["briefing"] == "the facts"

    queue: asyncio.Queue = asyncio.Queue()
    event_bus.subscribe("s23-test-task", queue)
    try:
        result = await _run_coder(
            run, UserConfig(model_routing="auto"),
            _specialist_state(update))
    finally:
        event_bus.unsubscribe("s23-test-task", queue)

    assert routing_env["factory_calls"] == [("google_gemini", "gemini-2.5-pro")]
    assert run.calls == []
    assert result["messages"] == [
        {"role": "assistant", "content": "deep answer"}]
    (event,) = _routing_events(queue)
    out = event.payload["output_payload"]
    assert out["model"] == "gemini-2.5-pro"
    assert out["tier"] == "strong"
    assert out["tier_source"] == "explicit"
    assert "supervisor marked this delegation strong tier" in out["reason"]


@pytest.mark.asyncio
async def test_untagged_delegation_routes_by_inference_as_before(routing_env):
    """The control: the identical delegation without a tag keeps
    the incumbent (inference reads the short text as small-tier
    work) and the published decision says the tier was inferred."""
    run = FakeProvider("google_gemini", "gemini-3.5-flash", ["the answer"])
    update = _hook_state_for_tagged_delegation("the facts")
    assert "tier" not in update["delegation_log"][0]["decision"]

    queue: asyncio.Queue = asyncio.Queue()
    event_bus.subscribe("s23-test-task", queue)
    try:
        result = await _run_coder(
            run, UserConfig(model_routing="auto"),
            _specialist_state(update))
    finally:
        event_bus.unsubscribe("s23-test-task", queue)

    assert routing_env["factory_calls"] == []
    assert result["messages"] == [
        {"role": "assistant", "content": "the answer"}]
    (event,) = _routing_events(queue)
    out = event.payload["output_payload"]
    assert out["model"] == "gemini-3.5-flash"
    assert out["tier_source"] == "inferred"
    assert "supervisor marked" not in out["reason"]


@pytest.mark.asyncio
async def test_routing_off_parses_and_logs_but_never_consults(routing_env):
    """Off mode: the hook still records the tier (and strips the
    briefing the specialist reads), but the router is never
    consulted — the run provider serves and nothing is published."""
    run = FakeProvider("google_gemini", "gemini-3.5-flash", ["the answer"])
    update = _hook_state_for_tagged_delegation("[tier: strong] the facts")
    assert update["delegation_log"][0]["decision"]["tier"] == "strong"
    assert update["context_briefing"] == "the facts"

    queue: asyncio.Queue = asyncio.Queue()
    event_bus.subscribe("s23-test-task", queue)
    try:
        result = await _run_coder(
            run, UserConfig(model_routing="off"),
            _specialist_state(update))
    finally:
        event_bus.unsubscribe("s23-test-task", queue)

    assert routing_env["factory_calls"] == []
    assert result["messages"] == [
        {"role": "assistant", "content": "the answer"}]
    assert _routing_events(queue) == []
    # The specialist's prompt carried the stripped briefing.
    served_messages = run.calls[0][0]
    assert "the facts" in served_messages[0]["content"]
    assert "[tier:" not in served_messages[0]["content"]


@pytest.mark.asyncio
async def test_pin_beats_the_stated_tier(routing_env):
    """A pin is never overridden by an explicit tier: routing is
    consulted only for the unpinned "inherit" case, so the pinned
    model is the only construction."""
    pinned = FakeProvider("openai", "gpt-4o", ["pinned answer"])
    routing_env["factory_providers"][("openai", "gpt-4o")] = pinned
    run = FakeProvider("google_gemini", "gemini-3.5-flash", ["run answer"])
    update = _hook_state_for_tagged_delegation("[tier: strong] the facts")
    result = await _run_coder(
        run, UserConfig(model_routing="auto"),
        _specialist_state(update),
        agent_models={"coder": "openai:gpt-4o"})
    assert routing_env["factory_calls"] == [("openai", "gpt-4o")]
    assert result["messages"] == [
        {"role": "assistant", "content": "pinned answer"}]
