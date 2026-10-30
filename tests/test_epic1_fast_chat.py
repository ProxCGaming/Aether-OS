"""Epic 1, E1-04: fast-chat path.

Pins the START_TASK split: plain conversation streams straight from the
resolved chat provider (no supervisor graph, no tool loop), while
tool-shaped work keeps going to the graph exactly as before.

Layers covered:
  * routing.intent — deterministic work heuristics first, model verdict
    second, chat fallback last; ambiguous prompts bias to the graph.
  * langgraph.fast_chat — the runner yields the graph runner's event
    contract (TASK_CREATED -> TASK_PROGRESS -> TASK_COMPLETED/FAILED),
    loads prior turns from the session store, and propagates
    cancellation without persisting a partial reply.
  * routes.websocket — handler-level flows over a real socket: chat
    never invokes run_langgraph_task, work always does, the fast-chat
    exchange lands in the session store a later graph task reads, and
    CANCEL_TASK cancels a fast-chat stream.

All providers are scripted stubs (house pattern from test_epic1_parity):
no real provider calls, no network.
"""
import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType, TaskState
from aether_engine.app import app, engine_state
from aether_engine.langgraph.fast_chat import load_session_history, run_fast_chat
from aether_engine.memory import session_store
from aether_engine.providers.base import RateLimitError, StreamChunk
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.intent import classify_intent

# ---------------------------------------------------------------------------
# Stub providers
# ---------------------------------------------------------------------------

class ChunkProvider:
    """Yields fixed text chunks for any call; records what it was shown."""

    def __init__(self, chunks=("Hello ", "there", "!")):
        self.chunks = list(chunks)
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({
            "node": node_name,
            "tools": tools,
            "messages": [dict(m) for m in messages],
        })
        for text in self.chunks:
            yield StreamChunk(text=text)
        yield StreamChunk(finish_reason="stop")


class VerdictProvider:
    """Answers the intent classifier with a fixed verdict; any other
    call gets a plain chat answer."""

    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({"node": node_name})
        if node_name == "intent_classifier":
            yield StreamChunk(text=self.verdict, finish_reason="stop")
        else:
            yield StreamChunk(text="chat answer", finish_reason="stop")


class GarbageProvider:
    """Never produces a parseable verdict."""

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        yield StreamChunk(text="purple monkey dishwasher", finish_reason="stop")


class FailingProvider:
    """Raises a fixed provider error on stream."""

    def __init__(self, error):
        self.error = error

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        raise self.error
        yield  # pragma: no cover — makes this an async generator


class SlowChatProvider:
    """Classifier verdict is instant CHAT; the chat stream yields one
    chunk and then hangs — for cancellation tests."""

    def __init__(self):
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append(node_name)
        if node_name == "intent_classifier":
            yield StreamChunk(text="CHAT", finish_reason="stop")
            return
        yield StreamChunk(text="Once upon a time ")
        await asyncio.sleep(60)
        yield StreamChunk(text="the end", finish_reason="stop")


class SlowVerdictProvider:
    """Never answers the classifier in time."""

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        await asyncio.sleep(30)
        yield StreamChunk(text="CHAT", finish_reason="stop")


class WsChatProvider:
    """Handler-level stub: instant CHAT verdict; the chat stream yields
    fixed chunks."""

    def __init__(self, chunks=("Hi ", "there!")):
        self.chunks = list(chunks)
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append(node_name)
        if node_name == "intent_classifier":
            yield StreamChunk(text="CHAT", finish_reason="stop")
            return
        for text in self.chunks:
            yield StreamChunk(text=text)
        yield StreamChunk(finish_reason="stop")


# ---------------------------------------------------------------------------
# Classifier: deterministic heuristics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_greeting_without_provider_is_chat():
    decision = await classify_intent("hello there")
    assert decision.is_chat
    assert decision.source == "fallback"


@pytest.mark.asyncio
async def test_plain_question_without_provider_is_chat():
    decision = await classify_intent("What is the capital of France?")
    assert decision.is_chat
    assert decision.source == "fallback"


@pytest.mark.asyncio
async def test_imperative_file_request_is_work_without_provider():
    decision = await classify_intent("write a python file that prints the time")
    assert not decision.is_chat
    assert decision.source == "heuristic"


@pytest.mark.asyncio
async def test_code_fence_is_work():
    decision = await classify_intent("what does this do?\n```python\nprint(1)\n```")
    assert not decision.is_chat
    assert decision.source == "heuristic"


@pytest.mark.asyncio
async def test_file_reference_is_work():
    decision = await classify_intent(
        "please fix the bug in aether_engine/routes/websocket.py")
    assert not decision.is_chat
    assert decision.source == "heuristic"


@pytest.mark.asyncio
async def test_web_request_is_work():
    decision = await classify_intent("search the web for the latest news on ISRO")
    assert not decision.is_chat
    assert decision.source == "heuristic"


@pytest.mark.asyncio
async def test_work_heuristic_never_consults_the_model():
    provider = VerdictProvider("CHAT")
    decision = await classify_intent("create a file called notes.txt", provider)
    assert not decision.is_chat
    assert decision.source == "heuristic"
    assert provider.calls == [], "definite work must not spend a model call"


# ---------------------------------------------------------------------------
# Classifier: model verdict + fallback behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_verdict_chat_for_ambiguous_prompt():
    provider = VerdictProvider("CHAT")
    decision = await classify_intent("tell me a story about a dragon", provider)
    assert decision.is_chat
    assert decision.source == "model"
    assert provider.calls[0]["node"] == "intent_classifier"


@pytest.mark.asyncio
async def test_model_verdict_work_overrides_question_shape():
    provider = VerdictProvider("WORK")
    decision = await classify_intent("What is the history of Rome?", provider)
    assert not decision.is_chat
    assert decision.source == "model"


@pytest.mark.asyncio
async def test_unparseable_verdict_falls_back_to_heuristics():
    decision = await classify_intent("What is the history of Rome?", GarbageProvider())
    assert decision.is_chat
    assert decision.source == "fallback"


@pytest.mark.asyncio
async def test_failing_provider_falls_back_without_raising():
    decision = await classify_intent(
        "hello", FailingProvider(RuntimeError("provider down")))
    assert decision.is_chat
    assert decision.source == "fallback"


@pytest.mark.asyncio
async def test_slow_verdict_times_out_to_fallback():
    decision = await classify_intent(
        "blorp zqx wobble", SlowVerdictProvider(), model_timeout_s=0.2)
    assert not decision.is_chat, "ambiguous prompts bias to the graph"
    assert decision.source == "fallback"


@pytest.mark.asyncio
async def test_ambiguous_prompt_without_provider_biases_to_work():
    decision = await classify_intent("blorp zqx wobble")
    assert not decision.is_chat
    assert decision.source == "fallback"


# ---------------------------------------------------------------------------
# Fast-chat runner
# ---------------------------------------------------------------------------

async def _collect(agen):
    return [event async for event in agen]


@pytest.fixture
def session_db(tmp_path, monkeypatch):
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    return tmp_path


@pytest.mark.asyncio
async def test_fast_chat_streams_deltas_then_completes():
    provider = ChunkProvider()
    events = await _collect(run_fast_chat(
        "hello", provider, request_id="req-1", session_id="sess-1"))

    assert [ev.type for ev in events] == [
        EventType.TASK_CREATED,
        EventType.TASK_PROGRESS,
        EventType.TASK_PROGRESS,
        EventType.TASK_PROGRESS,
        EventType.TASK_COMPLETED,
    ]
    created = events[0]
    assert created.payload["path"] == "fast_chat"
    assert created.payload["state"] == TaskState.RUNNING.value

    progress = [ev for ev in events if ev.type == EventType.TASK_PROGRESS]
    assert [ev.payload["text_delta"] for ev in progress] == ["Hello ", "there", "!"]
    assert [ev.payload["full_text"] for ev in progress] == [
        "Hello ", "Hello there", "Hello there!"]
    assert all(ev.payload["node"] == "assistant" for ev in progress)

    completed = events[-1]
    assert completed.payload["state"] == TaskState.SUCCEEDED.value
    assert completed.payload["response"] == "Hello there!"
    assert completed.payload["session_id"] == "sess-1"

    # No tools are ever offered on the fast path.
    assert provider.calls[0]["tools"] is None
    assert provider.calls[0]["node"] == "fast_chat"


def test_load_session_history_excludes_just_saved_prompt(session_db):
    sid = session_store.create_session("Chat")
    session_store.add_message(sid, "user", "earlier question")
    session_store.add_message(sid, "assistant", "earlier answer")
    # The handler saves the current user message before the split.
    session_store.add_message(sid, "user", "hello again")

    history = load_session_history(sid, "hello again")

    assert history == [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]


def test_load_session_history_unknown_session_is_empty(session_db):
    assert load_session_history("no-such-session", "hi") == []
    assert load_session_history(None, "hi") == []


@pytest.mark.asyncio
async def test_fast_chat_provider_sees_history_then_prompt(session_db):
    sid = session_store.create_session("Chat")
    session_store.add_message(sid, "user", "earlier question")
    session_store.add_message(sid, "assistant", "earlier answer")
    session_store.add_message(sid, "user", "hello again")
    history = load_session_history(sid, "hello again")

    provider = ChunkProvider(["answer"])
    await _collect(run_fast_chat("hello again", provider, history))

    messages = provider.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1:] == [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": "hello again"},
    ]


@pytest.mark.asyncio
async def test_fast_chat_provider_error_becomes_task_failed():
    provider = FailingProvider(RateLimitError("slow down"))
    events = await _collect(run_fast_chat("hello", provider))

    assert [ev.type for ev in events] == [
        EventType.TASK_CREATED, EventType.TASK_FAILED]
    failed = events[-1]
    assert failed.payload["state"] == TaskState.FAILED.value
    assert "slow down" in failed.payload["error"]
    assert failed.payload["retriable"] is True


@pytest.mark.asyncio
async def test_fast_chat_cancellation_propagates_without_completion():
    provider = SlowChatProvider()
    agen = run_fast_chat("tell me a story", provider)
    task = asyncio.create_task(_collect(agen))
    # Let the stream start (TASK_CREATED + first TASK_PROGRESS).
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Websocket handler flows
# ---------------------------------------------------------------------------

@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    """Engine state + stores isolated to tmp, mirroring test_engine_ws.

    Note: the app lifespan calls engine_state.initialize() at TestClient
    startup, rotating the auth token — tests must read
    engine_state.auth_token *after* entering the TestClient context.
    """
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    engine_state.initialize_auth()
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))
    return {"tmp_path": tmp_path}


def _graph_spy(calls):
    """A run_langgraph_task stand-in: records the call, completes at once."""
    def fake_run_langgraph_task(*args, **kwargs):
        calls.append(kwargs)

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

    return fake_run_langgraph_task


def _ws_stack(provider, graph_calls):
    """Patch the websocket module's provider factory, secret store and
    graph entry point (the names the handler actually looks up)."""
    stack = ExitStack()
    mock_store = stack.enter_context(
        patch.object(ws_module, "SecretStore")).return_value
    mock_store.list_providers.return_value = ["google_gemini"]
    stack.enter_context(patch.object(
        ws_module, "_create_provider_instance", return_value=provider))
    stack.enter_context(patch.object(
        ws_module, "run_langgraph_task", _graph_spy(graph_calls)))
    return stack


def _receive_until(ws, wanted, max_events=12):
    events = []
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        events.append(ev)
        if ev.type == wanted:
            return events
    raise AssertionError(
        f"{wanted} not received; got {[ev.type for ev in events]}")


def _start_task(ws, prompt, session_id):
    ws.send_text(Event(
        type=EventType.START_TASK,
        payload={"prompt": prompt, "session_id": session_id},
    ).to_json())


def test_ws_chat_message_uses_fast_path_and_persists(ws_env):
    provider = WsChatProvider()
    graph_calls = []
    sid = session_store.create_session("Existing chat")

    with (
        _ws_stack(provider, graph_calls),
        TestClient(app) as client,
        client.websocket_connect(f"/ws/tasks?token={engine_state.auth_token}") as ws,
    ):
        ws.receive_text()  # HELLO
        _start_task(ws, "Hello, how are you today?", sid)
        events = _receive_until(ws, EventType.TASK_COMPLETED)

    assert graph_calls == [], "chat must never invoke the supervisor graph"
    assert events[0].type == EventType.TASK_CREATED
    assert events[0].payload["path"] == "fast_chat"
    assert EventType.TASK_PROGRESS in [ev.type for ev in events]
    assert events[-1].payload["response"] == "Hi there!"
    assert provider.calls == ["intent_classifier", "fast_chat"]

    # The exchange is in the session store — the history a later graph
    # task loads via its get_current_chat_history memory tool.
    sess = session_store.get_session(sid)
    assert [(m["role"], m["content"]) for m in sess["messages"]] == [
        ("user", "Hello, how are you today?"),
        ("assistant", "Hi there!"),
    ]

    # A follow-up work message in the same session takes the graph, and
    # its exchange joins the same history.
    with (
        _ws_stack(provider, graph_calls),
        TestClient(app) as client,
        client.websocket_connect(f"/ws/tasks?token={engine_state.auth_token}") as ws,
    ):
        ws.receive_text()  # HELLO
        _start_task(ws, "Write a python file that prints hello", sid)
        events = _receive_until(ws, EventType.TASK_COMPLETED)

    assert len(graph_calls) == 1, "work must route to the supervisor graph"
    assert events[-1].payload["response"] == "graph answer"
    sess = session_store.get_session(sid)
    assert [(m["role"], m["content"]) for m in sess["messages"]] == [
        ("user", "Hello, how are you today?"),
        ("assistant", "Hi there!"),
        ("user", "Write a python file that prints hello"),
        ("assistant", "graph answer"),
    ]


def test_ws_work_message_uses_graph_without_touching_chat_provider(ws_env):
    provider = ChunkProvider(["should not be used"])
    graph_calls = []
    sid = session_store.create_session("Existing chat")

    with (
        _ws_stack(provider, graph_calls),
        TestClient(app) as client,
        client.websocket_connect(f"/ws/tasks?token={engine_state.auth_token}") as ws,
    ):
        ws.receive_text()  # HELLO
        _start_task(
            ws,
            "Fix the bug in aether_engine/langgraph/executor.py and run the tests",
            sid)
        events = _receive_until(ws, EventType.TASK_COMPLETED)

    assert len(graph_calls) == 1
    assert events[-1].payload["response"] == "graph answer"
    assert provider.calls == [], (
        "definite work is classified heuristically — no verdict call, "
        "no chat stream")


def test_ws_cancel_stops_fast_chat_stream(ws_env):
    provider = SlowChatProvider()
    graph_calls = []
    sid = session_store.create_session("Existing chat")

    with (
        _ws_stack(provider, graph_calls),
        TestClient(app) as client,
        client.websocket_connect(f"/ws/tasks?token={engine_state.auth_token}") as ws,
    ):
        ws.receive_text()  # HELLO
        _start_task(ws, "hello, tell me a long story", sid)
        events = _receive_until(ws, EventType.TASK_PROGRESS)
        assert events[0].payload["path"] == "fast_chat"

        ws.send_text(Event(type=EventType.CANCEL_TASK).to_json())
        events = _receive_until(ws, EventType.TASK_CANCELLED)

    assert events[-1].type == EventType.TASK_CANCELLED
    assert graph_calls == []
    # No partial assistant reply was persisted for the cancelled chat.
    sess = session_store.get_session(sid)
    assert [(m["role"], m["content"]) for m in sess["messages"]] == [
        ("user", "hello, tell me a long story"),
    ]
