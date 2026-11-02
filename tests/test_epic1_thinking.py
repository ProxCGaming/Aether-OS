"""Epic 1, E1-16 (engine side): the thinking stream.

Providers surface model reasoning on StreamChunk.thinking, strictly
apart from the answer text. Fast chat forwards it as THINKING events;
graph specialists publish it on the event bus for the executor to
forward. It never appears in the answer text or the saved response.
Models without thinking produce no THINKING events at all.
"""
import asyncio

import pytest

from aether_common.contracts import EventType
from aether_engine.event_bus import event_bus
from aether_engine.langgraph.fast_chat import run_fast_chat
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.nodes.planner import planner_node
from aether_engine.providers.base import StreamChunk

THINK = "let me think about this carefully"
ANSWER = "Here is the answer."


class _ThinkingProvider:
    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        yield StreamChunk(thinking=THINK, finish_reason=None)
        yield StreamChunk(text=ANSWER, finish_reason="stop")


@pytest.mark.asyncio
async def test_fast_chat_streams_thinking_apart_from_the_answer():
    events = [ev async for ev in run_fast_chat(
        "hard question", _ThinkingProvider(), [],
        request_id="req-think", session_id="sess-think")]
    thinking = [ev for ev in events if ev.type == EventType.THINKING]
    assert len(thinking) == 1
    assert thinking[0].payload["text_delta"] == THINK
    completed = [ev for ev in events if ev.type == EventType.TASK_COMPLETED]
    assert completed and completed[-1].payload["response"] == ANSWER
    progress_text = "".join(
        ev.payload.get("text_delta", "") for ev in events if ev.type == EventType.TASK_PROGRESS)
    assert THINK not in progress_text


@pytest.mark.asyncio
async def test_specialist_publishes_thinking_on_the_bus(monkeypatch):
    monkeypatch.setattr(planner_module, "save_artifact", lambda *a, **k: "think/plan.txt")
    queue: asyncio.Queue = asyncio.Queue()
    event_bus.subscribe("task-think", queue)
    try:
        state = {"messages": [{"role": "user", "content": "plan it"}],
                 "original_prompt": "plan it", "task_id": "task-think"}
        config = {"configurable": {"provider": _ThinkingProvider(),
                                   "tool_registry": None, "agent_models": {},
                                   "agent_tools": {}, "user_config": None}}
        result = await planner_node(state, config)
    finally:
        event_bus.unsubscribe("task-think", queue)
    assert result["messages"][0]["content"] == ANSWER  # thinking not in the answer
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    thinking = [ev for ev in events if getattr(ev, "type", None) == EventType.THINKING]
    assert len(thinking) == 1
    assert thinking[0].payload["text_delta"] == THINK
    assert thinking[0].payload["node"] == "planner"


@pytest.mark.asyncio
async def test_no_thinking_no_events():
    class _PlainProvider:
        async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
            yield StreamChunk(text=ANSWER, finish_reason="stop")

    events = [ev async for ev in run_fast_chat(
        "easy question", _PlainProvider(), [],
        request_id="req-plain", session_id="sess-plain")]
    assert not [ev for ev in events if ev.type == EventType.THINKING]
