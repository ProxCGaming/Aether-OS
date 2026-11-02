"""PF-1 (Epic 8 programme follow-up, xref M16): steering messages drop
their attachments before the model call.

M16 (``6d10bc0``) fixed the INITIAL graph turn: attachments forwarded
by the websocket are built into the user message once, via the shared
``multimodal_user_content()`` builder in ``langgraph/state.py``. The
steer path was left as a separate seam: the websocket's STEER_TASK
handler reads the payload's attachments and persists them with the
saved message, but calls ``enqueue_steer`` with text only — and
``steer_message_dicts`` renders ``content`` as the bare string. An
image attached to a steering message is therefore stored, shown in
the transcript, and never seen by the model.

Pinned here:
  * a graph task steered WITH an image attachment shows the image in
    the specialist's next model call, in the same parts shape (and
    with the same data URL) an initial turn produces;
  * the same holds for a steer applied on the approval-resume path;
  * a text-only steer renders byte-for-byte as before (plain string
    content, no parts list);
  * a steer attachment whose file is missing degrades exactly as the
    initial-turn path does — an honest "could not be loaded" note, no
    bogus image part, no crash;
  * the queue itself carries attachments through enqueue/drain and
    through the executor's re-enqueue-on-repause hand-off;
  * the websocket hand-off: a STEER_TASK sent during an active task
    queues a Steer that still carries the payload's attachments.

All providers are scripted stubs (house pattern): no network.
"""
import asyncio
import base64
import copy
import threading
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType, TaskState
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.nodes import researcher as researcher_module
from aether_engine.langgraph.state import multimodal_user_content
from aether_engine.langgraph.steering import (
    STEERING_QUEUE,
    drain_steers,
    enqueue_steer,
    peek_steers,
    steer_message_dicts,
)
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.memory import session_store
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.routes import websocket as ws_module
from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk

# 1x1 transparent PNG (same fixture bytes as test_epic8_graph_attachments).
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwAD"
    "hgGAWjR9awAAAABJRU5ErkJggg=="
)
PNG_BYTES = base64.b64decode(PNG_B64)


def _materialised_shot(session_key: str) -> Path:
    """Write the fixture PNG where production puts steer attachments.

    The websocket materialises uploads (steers included) under
    ``~/.aether/attachments/<session_id>/`` before enqueueing, and
    since S1-M8-ADJ the shared resolver only loads file paths from
    the sanctioned roots (that attachments tree, or granted workspace
    roots). A bare tmp_path file is refused by design, so the fixture
    lives in the attachments tree (HOME is conftest-sandboxed)."""
    shot_dir = Path.home() / ".aether" / "attachments" / session_key
    shot_dir.mkdir(parents=True, exist_ok=True)
    shot = shot_dir / "shot.png"
    shot.write_bytes(PNG_BYTES)
    return shot


STEER_TEXT = "STEER-SENTINEL: look at this screenshot instead"
TOOL_NAME = "steer_probe"
TOOL_ARGS = {"q": "x"}


# ---------------------------------------------------------------------------
# Queue + rendering seams (unit level)
# ---------------------------------------------------------------------------

def test_queue_carries_attachments_through_enqueue_and_drain():
    STEERING_QUEUE.clear()
    atts = [{"name": "shot.png", "mime_type": "image/png",
             "path": "/tmp/shot.png"}]
    enqueue_steer("pf1-q", STEER_TEXT, "m1", atts)

    peeked = peek_steers("pf1-q")
    assert len(peeked) == 1
    assert [dict(a) for a in peeked[0].attachments] == atts

    drained = drain_steers("pf1-q")
    assert [dict(a) for a in drained[0].attachments] == atts
    STEERING_QUEUE.clear()


def test_reenqueue_preserves_attachments():
    """The executor's resume path re-enqueues a drained steer verbatim
    (text, message_id) when the task pauses again — attachments must
    survive that hand-off too."""
    STEERING_QUEUE.clear()
    atts = [{"name": "shot.png", "data": f"data:image/png;base64,{PNG_B64}"}]
    enqueue_steer("pf1-re", STEER_TEXT, "m-re", atts)
    steer = drain_steers("pf1-re")[0]

    enqueue_steer("pf1-re", steer.text, steer.message_id, steer.attachments)

    again = drain_steers("pf1-re")
    assert len(again) == 1
    assert [dict(a) for a in again[0].attachments] == atts
    STEERING_QUEUE.clear()


def test_steer_rendering_matches_initial_turn_builder():
    """steer_message_dicts must produce exactly what the shared
    multimodal_user_content() builder produces for the same inputs —
    one construction, not a parallel one."""
    shot = _materialised_shot("pf1-eq")
    atts = [{"name": "shot.png", "mime_type": "image/png", "path": str(shot)}]
    enqueue_steer("pf1-eq", STEER_TEXT, "m-eq", atts)
    steers = drain_steers("pf1-eq")

    rendered = steer_message_dicts(steers)

    assert rendered == [{
        "role": "user",
        "content": multimodal_user_content(STEER_TEXT, atts),
    }]
    content = rendered[0]["content"]
    assert isinstance(content, list)
    images = [p for p in content if p.get("type") == "image_url"]
    assert len(images) == 1
    assert images[0]["image_url"]["url"] == f"data:image/png;base64,{PNG_B64}"


def test_steer_rendering_text_only_is_byte_identical():
    enqueue_steer("pf1-txt", STEER_TEXT, "m-txt")
    rendered = steer_message_dicts(drain_steers("pf1-txt"))
    assert rendered == [{"role": "user", "content": STEER_TEXT}]
    assert isinstance(rendered[0]["content"], str)


# ---------------------------------------------------------------------------
# Executor seam: scripted supervisor -> researcher provider
# ---------------------------------------------------------------------------

class SteerImageProvider:
    """Supervisor always routes to the researcher. The researcher's
    first call can be gated so a test can enqueue a steer while the
    call is in flight (mid-task), or made to request the probe tool
    (approval-pause flow). Every call's messages are recorded as deep
    copies, M16-style, so tests see exactly what the specialist's own
    model was given."""

    def __init__(self, gate_first_researcher=False, researcher_tool=False):
        self.calls = []
        self.researcher_calls = 0
        self.gate_first_researcher = gate_first_researcher
        self.researcher_tool = researcher_tool
        self.first_researcher_started = asyncio.Event()
        self.release_first_researcher = asyncio.Event()

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({
            "node": node_name,
            "messages": copy.deepcopy(messages),
        })
        if node_name == "researcher":
            self.researcher_calls += 1
            if self.researcher_calls == 1:
                if self.gate_first_researcher:
                    self.first_researcher_started.set()
                    await self.release_first_researcher.wait()
                if self.researcher_tool:
                    yield StreamChunk(
                        tool_calls=[ToolCall(
                            name=TOOL_NAME, args=dict(TOOL_ARGS),
                            call_id="call-pf1-1")],
                        finish_reason="tool_calls",
                    )
                    return
            yield StreamChunk(text="Specialist answer.",
                              finish_reason="stop")
            return
        yield StreamChunk(
            text='{"next": "researcher", "reason": "needs eyes", '
                 '"briefing": ""}',
            finish_reason="stop",
        )


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(
        researcher_module, "save_artifact", lambda *a, **k: "research/out.txt")
    STEERING_QUEUE.clear()
    yield tmp_path
    STEERING_QUEUE.clear()


async def _collect(agen):
    return [event async for event in agen]


def _types(events):
    return [ev.type for ev in events]


def _researcher_user_messages(provider, call_index):
    calls = [c for c in provider.calls if c["node"] == "researcher"]
    assert len(calls) > call_index, (
        f"researcher called {len(calls)} times, wanted call {call_index}")
    return [m for m in calls[call_index]["messages"] if m.get("role") == "user"]


def _image_parts(message):
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [p for p in content
            if isinstance(p, dict) and p.get("type") == "image_url"]


def _text_parts(message):
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"]


def _steer_message(provider, call_index):
    """The user message carrying the steer text in a researcher call."""
    for message in _researcher_user_messages(provider, call_index):
        content = message.get("content")
        if isinstance(content, str) and STEER_TEXT in content:
            return message
        if isinstance(content, list) and STEER_TEXT in _text_parts(message):
            return message
    raise AssertionError(
        f"no steer message in researcher call {call_index}: "
        f"{_researcher_user_messages(provider, call_index)!r}")


# ---------------------------------------------------------------------------
# (a) Mid-task steer with an image reaches the specialist's next call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mid_task_steer_image_reaches_specialist(graph_env, tmp_path):
    shot = _materialised_shot("pf1-mid-task")
    provider = SteerImageProvider(gate_first_researcher=True)
    session_id = "pf1-mid-task"

    task = asyncio.create_task(_collect(executor_module.run_langgraph_task(
        "describe the diagram",
        provider,
        tools=ToolRegistry(),
        task_id="task-pf1-mid",
        request_id="req-pf1-mid",
        session_id=session_id,
        user_config=UserConfig(),
    )))
    await asyncio.wait_for(provider.first_researcher_started.wait(), timeout=10)
    enqueue_steer(session_id, STEER_TEXT, "steer-pf1-1", [
        {"name": "shot.png", "mime_type": "image/png", "path": str(shot)},
    ])
    provider.release_first_researcher.set()
    events = await asyncio.wait_for(task, timeout=30)

    assert EventType.TASK_COMPLETED in _types(events)
    assert EventType.TASK_FAILED not in _types(events)
    applied = [ev for ev in events if ev.type == EventType.STEERING_APPLIED]
    assert len(applied) == 1
    assert applied[0].payload["message_id"] == "steer-pf1-1"

    steer_msg = _steer_message(provider, 1)
    images = _image_parts(steer_msg)
    assert len(images) == 1, f"steer image never reached the model: {steer_msg!r}"
    assert images[0]["image_url"]["url"] == f"data:image/png;base64,{PNG_B64}"
    assert STEER_TEXT in _text_parts(steer_msg)


@pytest.mark.asyncio
async def test_resume_steer_image_reaches_specialist(graph_env, tmp_path):
    """Steer queued during an approval pause, applied on resume — the
    resume consumer renders through the same steer_message_dicts."""
    shot = _materialised_shot("pf1-resume")
    executed = []
    registry = ToolRegistry()

    def _fn(**kwargs):
        executed.append(kwargs)
        return "probe-result-42"

    registry.register(Tool(
        name=TOOL_NAME, description="PF-1 probe tool",
        execute_fn=_fn, risk=ToolRisk.READ_ONLY))
    provider = SteerImageProvider(researcher_tool=True)
    session_id = "pf1-resume"
    user_config = UserConfig(tool_policies={TOOL_NAME: "Require Approval"})

    events = await _collect(executor_module.run_langgraph_task(
        "run the probe",
        provider,
        tools=registry,
        task_id="task-pf1-resume",
        request_id="req-pf1-resume",
        session_id=session_id,
        user_config=user_config,
    ))
    assert EventType.TASK_INTERRUPTED in _types(events)
    assert executed == [], "tool must not execute before approval"

    enqueue_steer(session_id, STEER_TEXT, "steer-pf1-paused", [
        {"name": "shot.png", "mime_type": "image/png", "path": str(shot)},
    ])

    resume_events = await _collect(executor_module.resume_langgraph_task(
        session_id, True, provider, tools=registry))

    assert EventType.TASK_COMPLETED in _types(resume_events)
    applied = [ev for ev in resume_events
               if ev.type == EventType.STEERING_APPLIED]
    assert len(applied) == 1
    assert applied[0].payload["message_id"] == "steer-pf1-paused"
    assert executed == [TOOL_ARGS]

    # The specialist call after the resume must carry the steer image.
    steer_msg = None
    for idx in range(len([c for c in provider.calls
                          if c["node"] == "researcher"])):
        try:
            steer_msg = _steer_message(provider, idx)
        except AssertionError:
            continue
        if _image_parts(steer_msg):
            break
        steer_msg = None
    assert steer_msg is not None, "no post-resume researcher call saw the image"
    images = _image_parts(steer_msg)
    assert images[0]["image_url"]["url"] == f"data:image/png;base64,{PNG_B64}"


# ---------------------------------------------------------------------------
# (b) Text-only steer through the executor is unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mid_task_text_only_steer_content_unchanged(graph_env):
    provider = SteerImageProvider(gate_first_researcher=True)
    session_id = "pf1-text-only"

    task = asyncio.create_task(_collect(executor_module.run_langgraph_task(
        "describe the diagram",
        provider,
        tools=ToolRegistry(),
        task_id="task-pf1-text",
        request_id="req-pf1-text",
        session_id=session_id,
        user_config=UserConfig(),
    )))
    await asyncio.wait_for(provider.first_researcher_started.wait(), timeout=10)
    enqueue_steer(session_id, STEER_TEXT, "steer-pf1-text")
    provider.release_first_researcher.set()
    events = await asyncio.wait_for(task, timeout=30)

    assert EventType.TASK_COMPLETED in _types(events)
    steer_msg = _steer_message(provider, 1)
    assert steer_msg["content"] == STEER_TEXT
    assert isinstance(steer_msg["content"], str)


# ---------------------------------------------------------------------------
# (c) Missing attachment file degrades exactly like the initial turn
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mid_task_steer_missing_file_degrades_to_note(
        graph_env, tmp_path):
    provider = SteerImageProvider(gate_first_researcher=True)
    session_id = "pf1-missing"

    task = asyncio.create_task(_collect(executor_module.run_langgraph_task(
        "describe the diagram",
        provider,
        tools=ToolRegistry(),
        task_id="task-pf1-missing",
        request_id="req-pf1-missing",
        session_id=session_id,
        user_config=UserConfig(),
    )))
    await asyncio.wait_for(provider.first_researcher_started.wait(), timeout=10)
    enqueue_steer(session_id, STEER_TEXT, "steer-pf1-gone", [
        {"name": "gone.png", "mime_type": "image/png",
         "path": str(tmp_path / "does-not-exist.png")},
    ])
    provider.release_first_researcher.set()
    events = await asyncio.wait_for(task, timeout=30)

    assert EventType.TASK_COMPLETED in _types(events)
    assert EventType.TASK_FAILED not in _types(events)
    steer_msg = _steer_message(provider, 1)
    assert _image_parts(steer_msg) == [], (
        f"bogus image part emitted: {steer_msg!r}")
    notes = " ".join(_text_parts(steer_msg))
    assert "gone.png" in notes and "could not be loaded" in notes, (
        f"missing steer attachment must degrade to the M16 note, "
        f"got {steer_msg!r}")


# ---------------------------------------------------------------------------
# Websocket hand-off: STEER_TASK during an active task queues the
# attachments, not just the text
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


def test_ws_steer_during_active_task_queues_attachments(ws_env):
    release = threading.Event()

    def fake_run_langgraph_task(*args, **kwargs):
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

    attachments = [{
        "name": "shot.png", "mime_type": "image/png",
        "data": f"data:image/png;base64,{PNG_B64}",
    }]
    sid = session_store.create_session("Existing chat")
    with (
        _ws_stack(fake_run_langgraph_task),
        TestClient(app) as client,
        client.websocket_connect("/ws/tasks", headers={"Authorization": f"Bearer {engine_state.auth_token}"}) as ws,
    ):
        ws.receive_text()  # HELLO
        ws.send_text(Event(
            type=EventType.START_TASK,
            payload={
                "prompt": "Write a python file that prints hello",
                "session_id": sid,
                "message_id": "start-pf1",
            },
        ).to_json())
        _receive_until(ws, EventType.TASK_CREATED)

        ws.send_text(Event(
            type=EventType.STEER_TASK,
            payload={
                "prompt": STEER_TEXT,
                "session_id": sid,
                "message_id": "steer-pf1-ws",
                "attachments": attachments,
            },
        ).to_json())

        async def _wait_for_steer():
            for _ in range(200):
                if peek_steers(sid):
                    return True
                await asyncio.sleep(0.025)
            return False

        assert asyncio.run(_wait_for_steer())
        queued = peek_steers(sid)
        assert len(queued) == 1
        assert queued[0].message_id == "steer-pf1-ws"
        assert [dict(a) for a in queued[0].attachments] == attachments, (
            "the websocket dropped the steer attachments at enqueue")

        release.set()
        events = _receive_until(ws, EventType.TASK_COMPLETED)
        assert events[-1].payload["response"] == "graph answer"
