"""Phase 9, C4: real integration tests for the supervisor.

The old file defined helpers but collected zero tests, so it could never
fail. These tests exercise live routing behavior with a mock provider.

S1-M4: the hand-written supervisor node these tests originally called
is dead code and has been removed. Every test now drives the production
supervisor path instead — ``AetherSupervisorModel._agenerate`` (the
create_supervisor routing model) plus ``supervisor_post_model_hook``
for the state bookkeeping — with the run config supplied through
langgraph's ``var_child_runnable_config``, the same way ``get_config()``
receives it inside a real graph run.
"""
import pytest
from langchain_core.messages import HumanMessage
from langgraph.config import var_child_runnable_config

from aether_engine.langgraph.supervisor_model import (
    AetherSupervisorModel,
    supervisor_post_model_hook,
)
from aether_engine.providers.base import StreamChunk


class InspectProvider:
    """Scripted provider: records its calls, returns a fixed decision."""

    def __init__(self, decision_json='{"next": "planner", "reason": "test", "briefing": ""}'):
        self.decision_json = decision_json
        self.received_messages = []
        self.calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls += 1
        self.received_messages.extend(messages)
        yield StreamChunk(text=self.decision_json, finish_reason="stop")


def _human():
    return HumanMessage(content="write a hello world script")


async def _generate(provider=None, **configurable_extra):
    """One production supervisor model step, returning its AIMessage."""
    configurable = {"session_id": "test-session"}
    if provider is not None:
        configurable["provider"] = provider
    configurable.update(configurable_extra)
    token = var_child_runnable_config.set({"configurable": configurable})
    try:
        result = await AetherSupervisorModel()._agenerate([_human()])
    finally:
        var_child_runnable_config.reset(token)
    return result.generations[0].message


@pytest.mark.asyncio
async def test_supervisor_delegates_to_planner():
    """Live path: provider returns a routing decision; the adapter
    translates it into a transfer_to_planner handoff, and the
    post-model hook records the delegation in state."""
    provider = InspectProvider()
    message = await _generate(provider=provider)
    assert provider.calls == 1
    assert [tc["name"] for tc in message.tool_calls] == ["transfer_to_planner"]
    decision = message.additional_kwargs["aether_decision"]
    assert decision["next"] == "planner"

    update = supervisor_post_model_hook({"messages": [_human(), message]})
    assert update["active_specialist"] == "planner"
    assert update["delegation_log"][0]["decision"]["next"] == "planner"


@pytest.mark.asyncio
async def test_supervisor_forced_agent_skips_provider():
    """forced_agent short-circuits in the adapter without calling
    any model (production: forced_agent in the available specialists
    returns a handoff result directly)."""
    provider = InspectProvider()
    message = await _generate(provider=provider, forced_agent="coder")
    assert [tc["name"] for tc in message.tool_calls] == ["transfer_to_coder"]
    assert message.additional_kwargs["aether_decision"]["next"] == "coder"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_supervisor_all_nodes_disabled_ends_task():
    """No available specialists -> the adapter's end result, no
    provider call."""
    provider = InspectProvider()
    message = await _generate(
        provider=provider,
        disabled_nodes={"researcher", "planner", "coder"},
    )
    assert message.tool_calls == []
    decision = message.additional_kwargs["aether_decision"]
    assert decision["next"] == "END"
    assert decision["reason"] == "All specialists are disabled."
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_supervisor_invalid_next_falls_back_to_end():
    """A decision naming an unavailable node produces no handoff —
    the run ends instead of crashing. The attempted decision is kept
    on the message for the delegation log (diagnosability), and the
    post-model hook records no active specialist."""
    provider = InspectProvider('{"next": "nonexistent", "reason": "bad", "briefing": ""}')
    message = await _generate(provider=provider)
    assert message.tool_calls == []
    assert message.additional_kwargs["aether_decision"]["next"] == "nonexistent"

    update = supervisor_post_model_hook({"messages": [_human(), message]})
    assert update["active_specialist"] is None


@pytest.mark.asyncio
async def test_supervisor_requires_provider():
    """Without a provider in config, the adapter fails loudly
    instead of hanging."""
    with pytest.raises(RuntimeError, match="Valid provider not found"):
        await _generate()



class ExplodingProvider:
    """Provider that fails every call (S1-N5: the tool-less retry)."""

    def __init__(self):
        self.calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls += 1
        raise RuntimeError("provider down")
        yield  # pragma: no cover — makes this an async generator


@pytest.mark.asyncio
async def test_supervisor_provider_down_on_both_attempts_ends_task():
    """S1-N5: when the with-tools call AND the tool-less retry both
    fail, _agenerate degrades to an end result instead of letting the
    retry's exception escape raw and kill the routing step."""
    provider = ExplodingProvider()
    message = await _generate(provider=provider)
    assert provider.calls == 2  # with-tools attempt + tool-less retry
    assert "failed" in str(message.content)
    assert message.additional_kwargs["aether_decision"]["next"] == "END"
