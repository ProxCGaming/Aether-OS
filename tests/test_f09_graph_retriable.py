"""F-09 (EGM Blocker C-B13): Epic 5 task-level fallback can never fire
for graph tasks.

The websocket pump (routes/websocket.py ``execute_task_generator``)
only rebuilds a failed task on a fallback provider when the
TASK_FAILED payload carries ``retriable`` — and the non-graph paths
have always set it: ``run_fast_chat`` (fast_chat.py) and
``resume_langgraph_task`` (executor.py) both compute
``retriable = _is_retriable_error(e)`` and include it (with ``state``
and ``latency_ms``) in the payload. The main graph path in
``run_langgraph_task`` emitted only ``{"task_id", "error"}``, so a
retriable graph failure was delivered as a plain failure and the
§6.4 fallback was unreachable for graph tasks.

These tests pin the graph path to the same contract:
  * a retriable graph failure yields TASK_FAILED with retriable=True;
  * a non-retriable graph failure yields retriable=False;
  * the payload the real executor produces drives the real websocket
    pump into its fallback retry (FALLBACK_TRIGGERED routing stamp,
    task rebuilt once, no TASK_FAILED delivered to the client).

All providers are scripted stubs (house pattern): no network.
"""
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType, TaskState
from aether_engine.app import app, engine_state
from aether_engine.langgraph import executor as executor_module
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.memory import session_store
from aether_engine.providers.base import AuthenticationError, RateLimitError
from aether_engine.routes import websocket as ws_module
from aether_engine.routing import fallback as fallback_module
from aether_engine.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Stub providers / fixtures
# ---------------------------------------------------------------------------


class FailingProvider:
    """Graph stub: every provider call raises the given error, so the
    graph run fails before any tool activity (the §6.4 window)."""

    def __init__(self, error):
        self.error = error

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        raise self.error
        yield  # pragma: no cover — makes this an async generator


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    """Isolate a full executor run (parity pattern): tmp checkpoint DB,
    no real memory writes on completion."""
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    return tmp_path


async def _collect(agen):
    return [event async for event in agen]


async def _run_failing_graph(error, task_id):
    return await _collect(executor_module.run_langgraph_task(
        "do some work",
        FailingProvider(error),
        tools=ToolRegistry(),
        task_id=task_id,
        request_id=f"req-{task_id}",
        session_id=None,
    ))


def _failed(events):
    failed = [ev for ev in events if ev.type == EventType.TASK_FAILED]
    assert len(failed) == 1, f"expected exactly one TASK_FAILED, got {[ev.type for ev in events]}"
    return failed[0]


# ---------------------------------------------------------------------------
# (a)/(b) — the graph TASK_FAILED payload carries retriable, computed
# exactly as the non-graph paths compute it (_is_retriable_error).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_retriable_failure_carries_retriable_true(graph_env):
    events = await _run_failing_graph(RateLimitError("slow down"), "f09-retriable")

    failed = _failed(events)
    assert failed.payload["task_id"] == "f09-retriable"
    assert failed.payload["retriable"] is True
    # Same payload shape as the resume / fast-chat TASK_FAILED events.
    assert failed.payload["state"] == TaskState.FAILED.value
    assert "slow down" in failed.payload["error"]


@pytest.mark.asyncio
async def test_graph_non_retriable_failure_carries_retriable_false(graph_env):
    events = await _run_failing_graph(
        AuthenticationError("401 unauthorized"), "f09-non-retriable")

    failed = _failed(events)
    assert failed.payload["task_id"] == "f09-non-retriable"
    assert failed.payload["retriable"] is False
    assert failed.payload["state"] == TaskState.FAILED.value


# ---------------------------------------------------------------------------
# (c) — the real executor's graph TASK_FAILED drives the websocket
# pump's Epic 5 fallback: the task is rebuilt once on the fallback
# candidate and completes; no TASK_FAILED reaches the client.
# ---------------------------------------------------------------------------


@pytest.fixture
def ws_env(graph_env, monkeypatch):
    """Engine state + stores isolated to tmp, mirroring
    test_epic1_fast_chat's ws_env (graph_env already isolated the
    checkpoint DB under the same tmp_path)."""
    engine_state.token_path = graph_env / "engine.token"
    engine_state.audit_logger.log_path = graph_env / "audit.log"
    engine_state.initialize_auth()
    monkeypatch.setattr(session_store, "DB_PATH", graph_env / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))
    return {"tmp_path": graph_env}


def _receive_until_terminal(ws, max_events=20):
    """Collect events until a terminal task event. Stopping on
    TASK_FAILED as well as TASK_COMPLETED keeps a broken build (which
    delivers the failure instead of retrying) from blocking forever
    on receive — the assertions below then fail on what arrived."""
    events = []
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        events.append(ev)
        if ev.type in (EventType.TASK_COMPLETED, EventType.TASK_FAILED):
            return events
    raise AssertionError(
        f"no terminal event received; got {[ev.type for ev in events]}")


def test_ws_pump_fallback_triggers_on_graph_retriable_failure(ws_env):
    import asyncio

    # The primary generator replays the events the REAL graph executor
    # produces for a retriable failure (captured here, same test).
    real_events = asyncio.run(
        _run_failing_graph(RateLimitError("slow down"), "graph-task-1"))
    assert _failed(real_events).payload["task_id"] == "graph-task-1"

    graph_calls = []

    def fake_run_langgraph_task(*args, **kwargs):
        graph_calls.append(kwargs)

        async def gen():
            if len(graph_calls) == 1:
                for ev in real_events:
                    yield ev
                return
            task_id = kwargs.get("task_id", "graph-task-1")
            yield Event(
                type=EventType.TASK_CREATED,
                request_id=kwargs.get("request_id"),
                payload={"task_id": task_id,
                         "prompt": kwargs.get("prompt", ""),
                         "state": TaskState.RUNNING.value},
            )
            yield Event(
                type=EventType.TASK_COMPLETED,
                request_id=kwargs.get("request_id"),
                payload={"task_id": task_id,
                         "state": TaskState.SUCCEEDED.value,
                         "response": "fallback answer",
                         "latency_ms": 1.0},
            )

        return gen()

    stack = ExitStack()
    mock_store = stack.enter_context(
        patch.object(ws_module, "SecretStore")).return_value
    mock_store.list_providers.return_value = ["google_gemini", "openai"]
    stack.enter_context(patch.object(
        ws_module, "_create_provider_instance", return_value=SimpleNamespace()))
    stack.enter_context(patch.object(
        ws_module, "run_langgraph_task", fake_run_langgraph_task))
    stack.enter_context(patch.object(
        fallback_module, "get_fallback_candidates",
        return_value=[SimpleNamespace(provider="openai", id="gpt-4o-mini")]))

    sid = session_store.create_session("F09 pump")
    with (
        stack,
        TestClient(app) as client,
        client.websocket_connect(f"/ws/tasks?token={engine_state.auth_token}") as ws,
    ):
        ws.receive_text()  # HELLO
        ws.send_text(Event(
            type=EventType.START_TASK,
            payload={
                "prompt": "Fix the bug in aether_engine/langgraph/executor.py and run the tests",
                "session_id": sid,
            },
        ).to_json())
        events = _receive_until_terminal(ws)

    # The pump saw retriable=True, rebuilt the task ONCE on the
    # fallback candidate (same task id), and the client never saw a
    # TASK_FAILED — the retry's TASK_CREATED carries the pump's
    # FALLBACK routing stamp.
    assert len(graph_calls) == 2
    assert graph_calls[1].get("task_id") == "graph-task-1"
    assert EventType.TASK_FAILED not in [ev.type for ev in events]
    created = [ev for ev in events if ev.type == EventType.TASK_CREATED]
    assert len(created) == 2
    assert created[1].payload["routing"]["provider"] == "openai"
    assert created[1].payload["routing"]["reason"].startswith("Fallback:")
    assert events[-1].payload["response"] == "fallback answer"
