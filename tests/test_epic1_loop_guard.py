"""Epic 1, E1-03 (part 2): the tool-call loop guard.

Five identical consecutive tool calls (same tool, same arguments) must
not execute a fifth time — the specialist gets a corrective steer as the
tool result and the counter resets. A different call in between resets
the streak. These tests drive execute_tool_node directly with a counting
fake registry, threading the returned guard fields into the next state
the way LangGraph's overwrite semantics do.
"""
import pytest

from aether_engine.langgraph.nodes.execute_tool import execute_tool_node
from aether_engine.langgraph.state import TOOL_LOOP_GUARD_THRESHOLD


class CountingRegistry:
    def __init__(self):
        self.calls = 0

    async def execute(self, name, args, **kwargs):
        self.calls += 1
        return f"result-{self.calls}"


def _state(registry_marker=None, **overrides):
    state = {
        "messages": [],
        "pending_tool_call": {"name": "search_web", "args": {"query": "x"}, "call_id": "c1"},
        "active_specialist": None,
        "tool_call_signature": None,
        "tool_call_repeats": 0,
    }
    state.update(overrides)
    return state


def _config(registry):
    return {"configurable": {"tool_registry": registry, "agent_tools": {}, "user_config": None}}


async def _call(registry, state):
    result = await execute_tool_node(state, _config(registry))
    # Thread the guard fields forward, as the graph would.
    state = dict(state)
    state["tool_call_signature"] = result.get("tool_call_signature")
    state["tool_call_repeats"] = result.get("tool_call_repeats", 0)
    return result, state


@pytest.mark.asyncio
async def test_fifth_identical_call_is_steered_not_executed():
    registry = CountingRegistry()
    state = _state()
    result = None
    for _ in range(TOOL_LOOP_GUARD_THRESHOLD):
        result, state = await _call(registry, state)
    # Threshold calls arrived; the last one must be the steer.
    assert registry.calls == TOOL_LOOP_GUARD_THRESHOLD - 1
    content = result["messages"][0]["content"]
    assert "Loop guard" in content
    assert "search_web" in content
    # Counter reset after the steer.
    assert result["tool_call_repeats"] == 0
    assert result["tool_call_signature"] is None


@pytest.mark.asyncio
async def test_different_call_resets_the_streak():
    registry = CountingRegistry()
    state = _state()
    for _ in range(TOOL_LOOP_GUARD_THRESHOLD - 1):
        _, state = await _call(registry, state)
    # A different tool breaks the streak.
    state["pending_tool_call"] = {"name": "get_current_time", "args": {}, "call_id": "c2"}
    result, state = await _call(registry, state)
    assert "Loop guard" not in result["messages"][0]["content"]
    assert result["tool_call_repeats"] == 1
    # And the original call starts a fresh streak afterwards.
    state["pending_tool_call"] = {"name": "search_web", "args": {"query": "x"}, "call_id": "c3"}
    result, _ = await _call(registry, state)
    assert result["tool_call_repeats"] == 1


@pytest.mark.asyncio
async def test_same_tool_different_args_is_a_different_signature():
    registry = CountingRegistry()
    state = _state()
    for i in range(TOOL_LOOP_GUARD_THRESHOLD + 2):
        state["pending_tool_call"] = {"name": "search_web", "args": {"query": f"q{i}"}, "call_id": f"c{i}"}
        result, state = await _call(registry, state)
        assert "Loop guard" not in result["messages"][0]["content"]
    assert registry.calls == TOOL_LOOP_GUARD_THRESHOLD + 2
