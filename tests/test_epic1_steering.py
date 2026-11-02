"""Epic 1, E1-07: the steering queue.

Pins the behaviour: a message sent while a task is running does NOT
cancel the task and is NOT lost — it is queued and delivered into the
running task at the next step boundary; a steer sent during an
approval pause is applied when the task resumes; and a STEER_TASK for
a session with no live task behaves like a normal START_TASK.

Layers covered:
  * langgraph.steering — FIFO order, per-session isolation, drain
    empties (pure unit tests).
  * langgraph.executor — mid-task injection (a later provider call's
    input contains the steer; STEERING_APPLIED is emitted; the task
    completes) and the approval-pause resume path.
  * routes.websocket — STEER_TASK with no active task falls through to
    a normal task start; STEER_TASK during an active task enqueues and
    saves the message without touching the running task.

All providers are scripted stubs (house pattern from
test_epic1_parity): no real provider calls, no network.
"""
import asyncio
import threading
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType, TaskState
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.steering import (
    STEERING_QUEUE,
    SteeringQueue,
    drain_steers,
    enqueue_steer,
    has_pending_steers,
    peek_steers,
)
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.memory import session_store
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.routes import websocket as ws_module
from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk

TOOL_NAME = "steer_probe"
TOOL_ARGS = {"q": "x"}
STEER_TEXT = "STEER-SENTINEL: also check the second file"


# ---------------------------------------------------------------------------
# Queue module: pure semantics
# ---------------------------------------------------------------------------

def test_queue_fifo_order_and_drain_empties():
    queue = SteeringQueue()
    queue.enqueue("s1", "first", "m1")
    queue.enqueue("s1", "second", "m2")
    queue.enqueue("s1", "third", "m3")

    drained = queue.drain("s1")

    assert [s.text for s in drained] == ["first", "second", "third"]
    assert [s.message_id for s in drained] == ["m1", "m2", "m3"]
    assert queue.drain("s1") == []
    assert not queue.has_pending("s1")


def test_queue_sessions_are_isolated():
    queue = SteeringQueue()
    queue.enqueue("s1", "for one")
    queue.enqueue("s2", "for two")

    assert [s.text for s in queue.peek("s1")] == ["for one"]
    assert [s.text for s in queue.drain("s2")] == ["for two"]
    assert queue.has_pending("s1")
    assert not queue.has_pending("s2")


def test_queue_peek_does_not_consume():
    queue = SteeringQueue()
    queue.enqueue("s1", "still here")
    assert len(queue.peek("s1")) == 1
    assert queue.has_pending("s1")
    assert [s.text for s in queue.drain("s1")] == ["still here"]


def test_global_registry_helpers_roundtrip():
    STEERING_QUEUE.clear()
    enqueue_steer("gs", "hello", "gm1")
    assert has_pending_steers("gs")
    assert peek_steers("gs")[0].message_id == "gm1"
    assert [s.text for s in drain_steers("gs")] == ["hello"]
    assert not has_pending_steers("gs")


# ---------------------------------------------------------------------------
# Executor: stub provider + probe registry (parity house pattern)
# ---------------------------------------------------------------------------

class SteeringGraphProvider:
    """Supervisor routes to planner; the planner's first call requests
    the probe tool (optionally gated so a test can enqueue a steer
    while the call is in flight); later planner calls return final
    text. Every call's message contents are recorded."""

    def __init__(self, gate_first_planner=False):
        self.calls = []
        self.planner_calls = 0
        self.gate_first_planner = gate_first_planner
        self.first_planner_started = asyncio.Event()
        self.release_first_planner = asyncio.Event()

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
                if self.gate_first_planner:
                    self.first_planner_started.set()
                    await self.release_first_planner.wait()
                yield StreamChunk(
                    tool_calls=[ToolCall(name=TOOL_NAME, args=dict(TOOL_ARGS),
                                         call_id="call-steer-1")],
                    finish_reason="tool_calls",
                )
            else:
                yield StreamChunk(text="Specialist final answer.",
                                  finish_reason="stop")
            return
        yield StreamChunk(
            text='{"next": "planner", "reason": "steering routing", "briefing": ""}',
            finish_reason="stop",
        )


def _probe_registry(executed):
    registry = ToolRegistry()

    def _fn(**kwargs):
        executed.append(kwargs)
        return "probe-result-42"

    registry.register(Tool(
        name=TOOL_NAME,
        description="Epic 1 steering probe tool",
        execute_fn=_fn,
        risk=ToolRisk.READ_ONLY,
    ))
    return registry


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "steer/artifact.txt")
    STEERING_QUEUE.clear()
    yield tmp_path
    STEERING_QUEUE.clear()


async def _collect(agen):
    return [event async for event in agen]


def _types(events):
    return [ev.type for ev in events]


# ---------------------------------------------------------------------------
# Executor: mid-task steer reaches a later provider call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mid_task_steer_reaches_later_provider_call(graph_env):
    executed = []
    registry = _probe_registry(executed)
    provider = SteeringGraphProvider(gate_first_planner=True)
    session_id = "steer-mid-task"

    task = asyncio.create_task(_collect(executor_module.run_langgraph_task(
        "please run the steering probe",
        provider,
        tools=registry,
        task_id="task-steer-mid",
        request_id="req-steer-mid",
        session_id=session_id,
        user_config=UserConfig(),
    )))
    await asyncio.wait_for(provider.first_planner_started.wait(), timeout=10)
    enqueue_steer(session_id, STEER_TEXT, "steer-msg-1")
    provider.release_first_planner.set()
    events = await asyncio.wait_for(task, timeout=30)

    assert EventType.TASK_COMPLETED in _types(events)
    assert EventType.TASK_FAILED not in _types(events)
    applied = [ev for ev in events if ev.type == EventType.STEERING_APPLIED]
    assert len(applied) == 1
    assert applied[0].payload["message_id"] == "steer-msg-1"
    assert applied[0].payload["content"] == STEER_TEXT

    # The planner's post-tool call must have seen the steer in its input.
    planner_contents = [
        "\n".join(call["contents"]) for call in provider.calls
        if call["node"] == "planner"
    ]
    assert len(planner_contents) >= 2
    assert STEER_TEXT not in planner_contents[0]
    assert any(STEER_TEXT in text for text in planner_contents[1:])
    assert executed == [TOOL_ARGS]


# ---------------------------------------------------------------------------
# Executor: steer queued during the approval pause applies on resume
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_steer_during_approval_pause_applies_on_resume(graph_env):
    executed = []
    registry = _probe_registry(executed)
    provider = SteeringGraphProvider()
    session_id = "steer-approval"
    user_config = UserConfig(tool_policies={TOOL_NAME: "Require Approval"})

    events = await _collect(executor_module.run_langgraph_task(
        "please run the steering probe",
        provider,
        tools=registry,
        task_id="task-steer-approval",
        request_id="req-steer-approval",
        session_id=session_id,
        user_config=user_config,
    ))
    assert EventType.TASK_INTERRUPTED in _types(events)
    assert executed == [], "tool must not execute before approval"

    # The steer arrives while the task waits for approval.
    enqueue_steer(session_id, STEER_TEXT, "steer-msg-paused")
    calls_before_resume = len(provider.calls)

    resume_events = await _collect(executor_module.resume_langgraph_task(
        session_id, True, provider, tools=registry))

    assert EventType.TASK_COMPLETED in _types(resume_events)
    applied = [ev for ev in resume_events
               if ev.type == EventType.STEERING_APPLIED]
    assert len(applied) == 1
    assert applied[0].payload["message_id"] == "steer-msg-paused"
    assert executed == [TOOL_ARGS]

    post_resume_contents = [
        "\n".join(call["contents"]) for call in provider.calls[calls_before_resume:]
    ]
    assert any(STEER_TEXT in text for text in post_resume_contents), (
        "the resumed run's provider input must contain the steer")


# ---------------------------------------------------------------------------
# Websocket handler flows
# ---------------------------------------------------------------------------

@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    engine_state.initialize_auth()
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))
    STEERING_QUEUE.clear()
    yield {"tmp_path": tmp_path}
    STEERING_QUEUE.clear()


def _ws_stack(graph_fake):
    stack = ExitStack()
    mock_store = stack.enter_context(
        patch.object(ws_module, "SecretStore")).return_value
    mock_store.list_providers.return_value = ["google_gemini"]
    stack.enter_context(patch.object(
        ws_module, "_create_provider_instance", return_value=object()))
    stack.enter_context(patch.object(
        ws_module, "run_langgraph_task", graph_fake))
    return stack


def _receive_until(ws, wanted, max_events=14):
    events = []
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        events.append(ev)
        if ev.type == wanted:
            return events
    raise AssertionError(
        f"{wanted} not received; got {[ev.type for ev in events]}")


def test_ws_steer_without_active_task_starts_a_normal_task(ws_env):
    graph_calls = []

    def fake_run_langgraph_task(*args, **kwargs):
        graph_calls.append(kwargs)

        async def gen():
            yield Event(
                type=EventType.TASK_CREATED,
                request_id=kwargs.get("request_id"),
                payload={"task_id": "fake-graph-task",
                         "prompt": kwargs.get("prompt", ""),
                         "state": TaskState.RUNNING.value},
            )
            yield Event(
                type=EventType.TASK_COMPLETED,
                request_id=kwargs.get("request_id"),
                payload={"task_id": "fake-graph-task",
                         "state": TaskState.SUCCEEDED.value,
                         "response": "graph answer",
                         "latency_ms": 1.0},
            )

        return gen()

    sid = session_store.create_session("Existing chat")
    with (
        _ws_stack(fake_run_langgraph_task),
        TestClient(app) as client,
        client.websocket_connect(f"/ws/tasks?token={engine_state.auth_token}") as ws,
    ):
        ws.receive_text()  # HELLO
        ws.send_text(Event(
            type=EventType.STEER_TASK,
            payload={
                "prompt": "Write a python file that prints hello",
                "session_id": sid,
                "message_id": "steer-standalone-1",
            },
        ).to_json())
        events = _receive_until(ws, EventType.TASK_COMPLETED)

    assert len(graph_calls) == 1, "a steer with no live task must start one"
    assert events[-1].payload["response"] == "graph answer"
    sess = session_store.get_session(sid)
    assert ("user", "Write a python file that prints hello") in [
        (m["role"], m["content"]) for m in sess["messages"]]


def test_ws_steer_during_active_task_queues_without_cancelling(ws_env):
    release = threading.Event()
    graph_calls = []

    def fake_run_langgraph_task(*args, **kwargs):
        graph_calls.append(kwargs)

        async def gen():
            yield Event(
                type=EventType.TASK_CREATED,
                request_id=kwargs.get("request_id"),
                payload={"task_id": "fake-graph-task",
                         "prompt": kwargs.get("prompt", ""),
                         "state": TaskState.RUNNING.value},
            )
            while not release.is_set():
                await asyncio.sleep(0.02)
            yield Event(
                type=EventType.TASK_COMPLETED,
                request_id=kwargs.get("request_id"),
                payload={"task_id": "fake-graph-task",
                         "state": TaskState.SUCCEEDED.value,
                         "response": "graph answer",
                         "latency_ms": 1.0},
            )

        return gen()

    sid = session_store.create_session("Existing chat")
    with (
        _ws_stack(fake_run_langgraph_task),
        TestClient(app) as client,
        client.websocket_connect(f"/ws/tasks?token={engine_state.auth_token}") as ws,
    ):
        ws.receive_text()  # HELLO
        ws.send_text(Event(
            type=EventType.START_TASK,
            payload={
                "prompt": "Write a python file that prints hello",
                "session_id": sid,
                "message_id": "start-1",
            },
        ).to_json())
        _receive_until(ws, EventType.TASK_CREATED)

        ws.send_text(Event(
            type=EventType.STEER_TASK,
            payload={
                "prompt": STEER_TEXT,
                "session_id": sid,
                "message_id": "steer-active-1",
            },
        ).to_json())

        # The handler enqueues + saves the steer; the task keeps running.
        async def _wait_for_steer():
            for _ in range(200):
                if has_pending_steers(sid):
                    return True
                await asyncio.sleep(0.025)
            return False

        assert asyncio.run(_wait_for_steer())
        sess = session_store.get_session(sid)
        assert ("user", STEER_TEXT) in [
            (m["role"], m["content"]) for m in sess["messages"]]
        assert len(graph_calls) == 1, "steering must not start a second task"

        release.set()
        events = _receive_until(ws, EventType.TASK_COMPLETED)
        assert events[-1].payload["response"] == "graph answer"
