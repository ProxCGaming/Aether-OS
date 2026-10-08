"""Phase 9, C4: real integration tests for the supervisor node.

The old file defined helpers but collected zero tests, so it could never
fail. These tests exercise live routing behavior with a mock provider.
"""
import pytest
from langchain_core.runnables import RunnableConfig

from aether_engine.langgraph.nodes.supervisor import supervisor_node
from aether_engine.providers.litellm_provider import LiteLLMProvider


class InspectProvider(LiteLLMProvider):
    def __init__(self, decision_json='{"next": "planner", "reason": "test", "briefing": ""}'):
        super().__init__(api_key="test", model="mock", provider_name="mock")
        self.decision_json = decision_json
        self.received_messages = []
        self.calls = 0

    async def call_stream(self, messages, **kwargs):
        self.calls += 1
        self.received_messages.extend(messages)

        class MockChunk:
            def __init__(self, text, finish_reason=None):
                self.text = text
                self.tool_calls = []
                self.finish_reason = finish_reason

        yield MockChunk(self.decision_json, finish_reason="stop")


def _base_state(**overrides):
    state = {
        "original_prompt": "write a hello world script",
        "messages": [{"role": "user", "content": "write a hello world script"}],
        "session_id": "test-session",
        "task_id": "test-task",
        "plan": "",
    }
    state.update(overrides)
    return state


def _config(provider=None, **extra):
    configurable = {"session_id": "test-session"}
    if provider is not None:
        configurable["provider"] = provider
    configurable.update(extra)
    return RunnableConfig(configurable=configurable)


@pytest.mark.asyncio
async def test_supervisor_delegates_to_planner():
    """Live path: provider returns a routing decision; node delegates."""
    provider = InspectProvider()
    result = await supervisor_node(_base_state(), _config(provider=provider))
    assert result["next"] == "planner"
    assert result["active_specialist"] == "planner"
    assert provider.calls == 1
    assert result["delegation_log"][0]["decision"]["next"] == "planner"


@pytest.mark.asyncio
async def test_supervisor_forced_agent_skips_provider():
    """forced_agent short-circuits without calling any model."""
    provider = InspectProvider()
    result = await supervisor_node(
        _base_state(), _config(provider=provider, forced_agent="coder")
    )
    assert result["next"] == "coder"
    assert result["active_specialist"] == "coder"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_supervisor_all_nodes_disabled_ends_task():
    """No available specialists -> END, no provider call."""
    provider = InspectProvider()
    result = await supervisor_node(
        _base_state(),
        _config(provider=provider, disabled_nodes={"researcher", "planner", "coder"}),
    )
    assert result["next"] == "END"
    assert result["active_specialist"] is None
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_supervisor_invalid_next_falls_back_to_end():
    """A decision naming an unavailable node is clamped to END, not crashed on."""
    provider = InspectProvider('{"next": "nonexistent", "reason": "bad", "briefing": ""}')
    result = await supervisor_node(_base_state(), _config(provider=provider))
    assert result["next"] == "END"


@pytest.mark.asyncio
async def test_supervisor_requires_provider():
    """Without a provider in config, the node fails loudly instead of hanging."""
    with pytest.raises(RuntimeError, match="Valid provider not found"):
        await supervisor_node(_base_state(), _config())
