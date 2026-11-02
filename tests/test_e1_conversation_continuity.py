"""Astra full-product E1-F05 / E1-F06: conversation continuity.

The family rule: conversation state — history and steering — must
survive mode switches (fast chat <-> graph task), approval pauses,
resumes, and disconnects. Nothing the user said may silently vanish
at any of those boundaries.

  * E1-F05 — graph -> fast chat -> graph in one session: the fast-chat
    turns are persisted to the session store (the websocket wrapper
    saves both sides of every exchange, whichever path produced it),
    but a later graph run seeded its context from the graph checkpoint
    alone once one existed, so the fast-chat turns never reached the
    work model's input.
  * E1-F06 — a steer enqueued WHILE an approval resume is running was
    stranded: the resume path drained the queue once at entry and
    never again, so when the resumed task completed the steer was
    still queued, unapplied, with no STEERING_APPLIED.

Providers are scripted stubs (house pattern from test_epic1_steering);
the session store, the fast-chat runner, the graph executor and the
steering queue are the real ones.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from aether_common.contracts import EventType
from aether_engine.config import UserConfig
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.fast_chat import load_session_history, run_fast_chat
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.steering import (
    STEERING_QUEUE,
    enqueue_steer,
    has_pending_steers,
)
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.memory import session_store
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk

TOOL_NAME = "continuity_probe"
TOOL_ARGS = {"q": "x"}
STEER_TEXT = "STEER-DURING-RESUME-SENTINEL: prefer the second file"

GRAPH_PROMPT_1 = "GRAPH-ONE-SENTINEL: run the probe on the first file"
FAST_USER = "FAST-USER-SENTINEL: the code word is kumquat"
FAST_REPLY = "FAST-REPLY-SENTINEL: kumquat, noted"
GRAPH_PROMPT_2 = "GRAPH-TWO-SENTINEL: continue from the probe results"


# ---------------------------------------------------------------------------
# Scripted providers
# ---------------------------------------------------------------------------

class GraphProvider:
    """Supervisor routes to planner; the planner's first call requests
    the probe tool, later planner calls return final text. Every
    call's message contents are recorded."""

    def __init__(self):
        self.calls = []
        self.planner_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        contents = []
        for m in messages:
            if isinstance(m, dict):
                contents.append(str(m.get("content", "")))
            else:
                contents.append(str(getattr(m, "content", "")))
        self.calls.append({"node": node_name, "contents": contents})
        if node_name == "planner":
            self.planner_calls += 1
            if self.planner_calls == 1:
                yield StreamChunk(
                    tool_calls=[ToolCall(name=TOOL_NAME, args=dict(TOOL_ARGS),
                                         call_id="call-cont-1")],
                    finish_reason="tool_calls",
                )
            else:
                yield StreamChunk(text="Specialist final answer.",
                                  finish_reason="stop")
            return
        yield StreamChunk(
            text='{"next": "planner", "reason": "continuity routing", "briefing": ""}',
            finish_reason="stop",
        )


class ResumeGateProvider(GraphProvider):
    """GraphProvider whose SECOND planner call — the first one inside
    the resume segment — can be held open so a test can enqueue a
    steer while the resumed run is genuinely in flight, and can
    optionally request the probe tool again (a second approval pause)
    instead of returning final text."""

    def __init__(self, *, resumed_call_requests_tool=False):
        super().__init__()
        self.resumed_call_requests_tool = resumed_call_requests_tool
        self.resumed_call_started = asyncio.Event()
        self.release_resumed_call = asyncio.Event()

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "planner" and self.planner_calls == 1:
            # planner_calls is incremented inside super(); at entry it
            # still holds the count of completed planner calls, so 1
            # means the next call is the second — the resumed one.
            self.resumed_call_started.set()
            await self.release_resumed_call.wait()
            if self.resumed_call_requests_tool:
                contents = []
                for m in messages:
                    if isinstance(m, dict):
                        contents.append(str(m.get("content", "")))
                    else:
                        contents.append(str(getattr(m, "content", "")))
                self.calls.append({"node": node_name, "contents": contents})
                self.planner_calls += 1
                yield StreamChunk(
                    tool_calls=[ToolCall(name=TOOL_NAME, args=dict(TOOL_ARGS),
                                         call_id="call-cont-2")],
                    finish_reason="tool_calls",
                )
                return
        async for chunk in super().call_stream(
                messages, tools=tools, node_name=node_name, **kwargs):
            yield chunk


class FixedReplyChatProvider:
    """Fast-chat stub: one fixed reply, records its input."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({
            "node": node_name,
            "messages": [dict(m) for m in messages],
        })
        yield StreamChunk(text=self.reply)
        yield StreamChunk(finish_reason="stop")


def _probe_registry(executed):
    registry = ToolRegistry()

    def _fn(**kwargs):
        executed.append(kwargs)
        return "probe-result-42"

    registry.register(Tool(
        name=TOOL_NAME,
        description="Conversation continuity probe tool",
        execute_fn=_fn,
        risk=ToolRisk.READ_ONLY,
    ))
    return registry


@pytest.fixture
def continuity_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "continuity/artifact.txt")
    STEERING_QUEUE.clear()
    yield tmp_path
    STEERING_QUEUE.clear()


async def _collect(agen):
    return [event async for event in agen]


def _types(events):
    return [ev.type for ev in events]


def _planner_inputs(provider):
    return [
        "\n".join(call["contents"]) for call in provider.calls
        if call["node"] == "planner"
    ]


# ---------------------------------------------------------------------------
# E1-F05: fast-chat turns must reach the next graph run's context
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fast_chat_turns_reach_next_graph_run(continuity_env):
    executed = []
    registry = _probe_registry(executed)
    sid = session_store.create_session("Continuity")

    # Graph run 1. The websocket handler saves the user message to the
    # session store before the run starts and the wrapper persists the
    # assistant reply when it completes; mirror both writes with the
    # same store calls.
    session_store.add_message(sid, "user", GRAPH_PROMPT_1)
    provider1 = GraphProvider()
    events1 = await _collect(executor_module.run_langgraph_task(
        GRAPH_PROMPT_1,
        provider1,
        tools=registry,
        task_id="task-cont-graph-1",
        request_id="req-cont-graph-1",
        session_id=sid,
        user_config=UserConfig(),
    ))
    assert EventType.TASK_COMPLETED in _types(events1)
    response1 = events1[-1].payload["response"]
    session_store.add_message(sid, "assistant", response1)

    # Fast-chat turn in the same session, through the real fast-chat
    # runner; the wrapper persists both sides exactly as above.
    session_store.add_message(sid, "user", FAST_USER)
    chat_provider = FixedReplyChatProvider(FAST_REPLY)
    chat_events = await _collect(run_fast_chat(
        FAST_USER,
        chat_provider,
        load_session_history(sid, FAST_USER),
        request_id="req-cont-fast",
        session_id=sid,
    ))
    assert EventType.TASK_COMPLETED in _types(chat_events)
    assert chat_events[-1].payload["response"] == FAST_REPLY
    session_store.add_message(sid, "assistant", FAST_REPLY)

    # Graph run 2: the checkpoint from run 1 exists now. The fast-chat
    # exchange must be part of this run's model context.
    session_store.add_message(sid, "user", GRAPH_PROMPT_2)
    provider2 = GraphProvider()
    events2 = await _collect(executor_module.run_langgraph_task(
        GRAPH_PROMPT_2,
        provider2,
        tools=registry,
        task_id="task-cont-graph-2",
        request_id="req-cont-graph-2",
        session_id=sid,
        user_config=UserConfig(),
    ))
    assert EventType.TASK_COMPLETED in _types(events2)

    first_planner_input = _planner_inputs(provider2)[0]
    assert FAST_USER in first_planner_input, (
        "the fast-chat user turn vanished from the graph run's context")
    assert FAST_REPLY in first_planner_input, (
        "the fast-chat reply vanished from the graph run's context")
    # The merge must not duplicate turns: each fast turn lands once,
    # in conversation order, ahead of the new prompt.
    assert first_planner_input.count(FAST_USER) == 1
    assert first_planner_input.count(FAST_REPLY) == 1
    assert (first_planner_input.index(FAST_USER)
            < first_planner_input.index(FAST_REPLY)
            < first_planner_input.index(GRAPH_PROMPT_2))
    # The earlier graph turn is still there, also exactly once.
    assert first_planner_input.count(GRAPH_PROMPT_1) == 1


# ---------------------------------------------------------------------------
# E1-F06: a steer arriving during the resume must be applied by it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_steer_arriving_during_resume_is_applied(continuity_env):
    executed = []
    registry = _probe_registry(executed)
    provider = ResumeGateProvider()
    session_id = "continuity-resume-live"
    user_config = UserConfig(tool_policies={TOOL_NAME: "Require Approval"})

    events = await _collect(executor_module.run_langgraph_task(
        "please run the continuity probe",
        provider,
        tools=registry,
        task_id="task-cont-resume",
        request_id="req-cont-resume",
        session_id=session_id,
        user_config=user_config,
    ))
    assert EventType.TASK_INTERRUPTED in _types(events)
    assert executed == [], "tool must not execute before approval"

    resume_task = asyncio.create_task(_collect(
        executor_module.resume_langgraph_task(
            session_id, True, provider, tools=registry,
            user_config=user_config)))
    # The resumed run is in flight (its first planner call is open)
    # when the steer arrives — not before the resume started.
    await asyncio.wait_for(provider.resumed_call_started.wait(), timeout=10)
    enqueue_steer(session_id, STEER_TEXT, "steer-during-resume")
    provider.release_resumed_call.set()
    resume_events = await asyncio.wait_for(resume_task, timeout=30)

    assert EventType.TASK_COMPLETED in _types(resume_events)
    applied = [ev for ev in resume_events
               if ev.type == EventType.STEERING_APPLIED]
    assert len(applied) == 1, (
        "steer sent during the resume was never applied")
    assert applied[0].payload["message_id"] == "steer-during-resume"
    assert executed == [TOOL_ARGS]
    assert not has_pending_steers(session_id), (
        "the steer must not be left stranded in the queue")

    post_resume_inputs = _planner_inputs(provider)
    assert any(STEER_TEXT in text for text in post_resume_inputs[1:]), (
        "a provider call after the steer must have seen it")


@pytest.mark.asyncio
async def test_steer_arriving_during_resume_survives_a_second_pause(
        continuity_env):
    """Family rule at the pause boundary: if the resumed run pauses
    again before the steer can apply, the steer stays queued — never
    applied early, never dropped — and the next resume applies it."""
    executed = []
    registry = _probe_registry(executed)
    provider = ResumeGateProvider(resumed_call_requests_tool=True)
    session_id = "continuity-resume-repause"
    user_config = UserConfig(tool_policies={TOOL_NAME: "Require Approval"})

    events = await _collect(executor_module.run_langgraph_task(
        "please run the continuity probe",
        provider,
        tools=registry,
        task_id="task-cont-repause",
        request_id="req-cont-repause",
        session_id=session_id,
        user_config=user_config,
    ))
    assert EventType.TASK_INTERRUPTED in _types(events)

    resume_task = asyncio.create_task(_collect(
        executor_module.resume_langgraph_task(
            session_id, True, provider, tools=registry,
            user_config=user_config)))
    await asyncio.wait_for(provider.resumed_call_started.wait(), timeout=10)
    enqueue_steer(session_id, STEER_TEXT, "steer-during-repause")
    provider.release_resumed_call.set()
    resume_events = await asyncio.wait_for(resume_task, timeout=30)

    # The resumed run requested the tool again and paused again: the
    # steer must still be queued, unapplied.
    assert EventType.TASK_INTERRUPTED in _types(resume_events)
    assert not [ev for ev in resume_events
                if ev.type == EventType.STEERING_APPLIED]
    assert has_pending_steers(session_id), (
        "the steer vanished at the second pause")

    final_events = await _collect(executor_module.resume_langgraph_task(
        session_id, True, provider, tools=registry,
        user_config=user_config))
    assert EventType.TASK_COMPLETED in _types(final_events)
    applied = [ev for ev in final_events
               if ev.type == EventType.STEERING_APPLIED]
    assert len(applied) == 1
    assert applied[0].payload["message_id"] == "steer-during-repause"
    assert executed == [TOOL_ARGS, TOOL_ARGS]
    assert not has_pending_steers(session_id)
