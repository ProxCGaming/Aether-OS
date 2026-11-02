"""S1-M5: specialists must not silently discard parallel tool calls.

Before the fix, each specialist node accumulated every streamed tool
call and then took ONLY the first ("# Take the first tool call") — the
rest vanished with no trace: never executed, never logged, and the
assistant message recorded only the one call.

The fix keeps the first call as ``pending_tool_call`` (the tool loop
executes one call per round, as before) and checkpoints the remaining
calls in the ``queued_tool_calls`` state channel. On the next tool-loop
round the specialist node drains the queue head into
``pending_tool_call`` WITHOUT calling the model again — the assistant
message carrying all the calls was already recorded when they were
emitted. Only once the queue is empty does the node call the model
again.
"""
import json

import pytest

from aether_engine.langgraph.nodes import coder as coder_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.nodes import researcher as researcher_module
from aether_engine.langgraph.nodes.tool_calls import (
    assistant_tool_calls,
    drain_queued_tool_call,
    pending_and_queued,
)
from aether_engine.providers.base import StreamChunk, ToolCall

SPECIALISTS = [
    pytest.param(coder_module, coder_module.coder_node, "coder", id="coder"),
    pytest.param(planner_module, planner_module.planner_node, "planner", id="planner"),
    pytest.param(researcher_module, researcher_module.researcher_node, "researcher", id="researcher"),
]

CALLS = [
    ("read_file", {"path": "a.py"}, "call-1"),
    ("search_text", {"pattern": "TODO"}, "call-2"),
    ("get_current_time", {}, "call-3"),
]


class ParallelProvider:
    """First stream emits 3 parallel tool calls; later streams return
    plain text. Records how often the model was actually called."""

    def __init__(self):
        self.call_count = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.call_count += 1
        if self.call_count == 1:
            yield StreamChunk(
                tool_calls=[
                    ToolCall(name=name, args=dict(args), call_id=call_id)
                    for name, args, call_id in CALLS
                ],
                finish_reason="tool_calls",
            )
        else:
            yield StreamChunk(text="all done", finish_reason="stop")


def _state(**overrides):
    state = {
        "original_prompt": "Do three things",
        "messages": [{"role": "user", "content": "Do three things"}],
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
        "task_id": "s1m5-test",
        "session_id": "s1m5-session",
        "context_briefing": None,
        "active_skills": [],
        "blackboard": {},
        "plan_mode": False,
    }
    state.update(overrides)
    return state


def _config(provider):
    return {"configurable": {"provider": provider}}


# ── Helper units ─────────────────────────────────────────────────────

def test_pending_and_queued_splits_first_from_rest():
    accumulated = [
        ToolCall(name=name, args=dict(args), call_id=call_id)
        for name, args, call_id in CALLS
    ]
    pending, queued = pending_and_queued(accumulated)
    assert pending == {"name": "read_file", "args": {"path": "a.py"}, "call_id": "call-1"}
    assert queued == [
        {"name": "search_text", "args": {"pattern": "TODO"}, "call_id": "call-2"},
        {"name": "get_current_time", "args": {}, "call_id": "call-3"},
    ]


def test_pending_and_queued_non_dict_args_become_empty_dict():
    pending, queued = pending_and_queued(
        [ToolCall(name="x", args="not-a-dict", call_id="c9")])
    assert pending == {"name": "x", "args": {}, "call_id": "c9"}
    assert queued == []


def test_assistant_tool_calls_carries_every_call():
    accumulated = [
        ToolCall(name=name, args=dict(args), call_id=call_id)
        for name, args, call_id in CALLS
    ]
    shaped = assistant_tool_calls(accumulated)
    assert [tc["id"] for tc in shaped] == ["call-1", "call-2", "call-3"]
    assert all(tc["type"] == "function" for tc in shaped)
    assert shaped[1]["function"]["name"] == "search_text"
    assert json.loads(shaped[1]["function"]["arguments"]) == {"pattern": "TODO"}


def test_drain_queued_tool_call_pops_head_and_keeps_rest():
    state = _state(queued_tool_calls=[
        {"name": "search_text", "args": {"pattern": "TODO"}, "call_id": "call-2"},
        {"name": "get_current_time", "args": {}, "call_id": "call-3"},
    ])
    drained = drain_queued_tool_call(state)
    assert drained == {
        "pending_tool_call": {"name": "search_text", "args": {"pattern": "TODO"}, "call_id": "call-2"},
        "queued_tool_calls": [{"name": "get_current_time", "args": {}, "call_id": "call-3"}],
    }


def test_drain_queued_tool_call_empty_queue_is_none():
    assert drain_queued_tool_call(_state()) is None
    assert drain_queued_tool_call({}) is None


# ── Node behaviour ───────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("module,node,agent", SPECIALISTS)
async def test_parallel_calls_are_queued_not_discarded(module, node, agent, monkeypatch):
    monkeypatch.setattr(module, "save_artifact", lambda *a, **k: "/tmp/out.txt")
    provider = ParallelProvider()
    result = await node(_state(), _config(provider))

    # First call is pending, as before …
    assert result["pending_tool_call"] == {
        "name": "read_file", "args": {"path": "a.py"}, "call_id": "call-1"}
    # … but the other two are queued, not discarded.
    assert result["queued_tool_calls"] == [
        {"name": "search_text", "args": {"pattern": "TODO"}, "call_id": "call-2"},
        {"name": "get_current_time", "args": {}, "call_id": "call-3"},
    ]
    # The recorded assistant message carries ALL three calls.
    tool_calls = result["messages"][0]["tool_calls"]
    assert [tc["id"] for tc in tool_calls] == ["call-1", "call-2", "call-3"]
    assert [tc["function"]["name"] for tc in tool_calls] == [
        "read_file", "search_text", "get_current_time"]


@pytest.mark.asyncio
@pytest.mark.parametrize("module,node,agent", SPECIALISTS)
async def test_queued_calls_drain_one_per_round_without_model_calls(module, node, agent, monkeypatch):
    monkeypatch.setattr(module, "save_artifact", lambda *a, **k: "/tmp/out.txt")
    provider = ParallelProvider()
    config = _config(provider)

    first = await node(_state(), config)
    assert provider.call_count == 1

    # Round 2: the queue head becomes pending; the model is NOT called.
    state = _state(queued_tool_calls=first["queued_tool_calls"])
    second = await node(state, config)
    assert provider.call_count == 1
    assert second["pending_tool_call"]["call_id"] == "call-2"
    assert second["queued_tool_calls"] == [
        {"name": "get_current_time", "args": {}, "call_id": "call-3"}]
    assert "messages" not in second  # no new assistant message on a drain

    # Round 3: last queued call drains, still no model call.
    state = _state(queued_tool_calls=second["queued_tool_calls"])
    third = await node(state, config)
    assert provider.call_count == 1
    assert third["pending_tool_call"]["call_id"] == "call-3"
    assert third["queued_tool_calls"] == []

    # Round 4: queue empty — the model IS called again.
    state = _state(queued_tool_calls=third["queued_tool_calls"])
    fourth = await node(state, config)
    assert provider.call_count == 2
    assert fourth["messages"][0]["content"] == "all done"
