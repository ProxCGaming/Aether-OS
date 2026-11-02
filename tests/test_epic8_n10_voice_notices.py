"""Epic 8, N10: voice escalation announcements.

The audit found three gaps in how escalated (background) tasks talk
back through a live voice session:

  * Only completion/failure triggered speech — an approval pause
    (the task sitting in PENDING_APPROVAL waiting on the owner) was
    never announced, so a voice user could wait forever on a task
    that was actually waiting on THEM.
  * Announcements carried no identity: their VOICE_OUTPUT_CHUNKs
    were indistinguishable from a talker reply's, so the desktop
    folded them into the committable reply stream and later
    committed them as a second assistant message — duplicating the
    task's own persisted message.
  * A barge-in during an announcement likewise reported the heard
    prefix as committable text.

The fix pinned here: every announcement is spoken with a stable
announcement identity derived from the child task + event kind
(``voice-ann:{task_id}:{kind}[:...]``); completion announcements
also carry the id of the task's persisted chat message. Approval
announcements are factual — the task is paused, a decision is
waiting, and the decision happens in chat. An announcement is
never an approval channel: nothing in the voice path can grant
the pending approval.

All stages are scripted fakes, as in the 5.1a/5.1b suites.
"""
import asyncio
import threading
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient
from test_voice_barge_in import (
    ChunkDelayEmitter,
    QueueTransport,
    ScriptedTalker,
    ScriptedTransport,
    _fake_classify,
    _patch_factory,
    _receive_until,
    _start_voice_session,
    _stop_voice_session,
    _voice_stack,
    audio_chunks,
    start_pipeline,
    stop_pipeline,
    text_deltas,
    wait_until,
)
from test_voice_pipeline import (
    Emitter,
    FakeStt,
    FakeTransport,
    frame,
    make_pipeline,
    speech_then_silence,
)

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.memory import session_store
from aether_engine.routes import websocket as ws_module
from aether_engine.voice.pipeline import DEFAULT_ESCALATION_ACK


def _tagged(emitter, announcement_id=None):
    """VOICE_OUTPUT_CHUNK events carrying an announcement identity
    (or one specific identity)."""
    out = []
    for e in emitter.of_type(EventType.VOICE_OUTPUT_CHUNK):
        aid = e.payload.get("announcement_id")
        if aid is None:
            continue
        if announcement_id is not None and aid != announcement_id:
            continue
        out.append(e)
    return out


# ---------------------------------------------------------------------------
# Pipeline level: announcement identity on the wire
# ---------------------------------------------------------------------------

class TestAnnouncementIdentity:
    async def test_announcement_chunks_carry_the_stable_identity(self):
        transport = QueueTransport()
        emitter = Emitter()
        committed = []

        async def on_assistant_final(text):
            committed.append(text)

        pipeline = make_pipeline(
            transport, emitter=emitter,
            on_assistant_final=on_assistant_final)
        run_task = await start_pipeline(pipeline)
        try:
            spoken = await pipeline.speak_announcement(
                "Report finished: 3 files changed.",
                announcement_id="voice-ann:vtask-1:completed",
                kind="completed", message_id="msg-42")
            assert spoken is True
            tagged = _tagged(emitter, "voice-ann:vtask-1:completed")
            assert tagged, "announcement chunks must carry the identity"
            # Text AND audio chunks alike: the desktop dedupes on
            # either, and plays audio from the same event stream.
            assert any(e.payload.get("text_delta") for e in tagged)
            assert any(e.payload.get("audio_chunk") for e in tagged)
            for e in tagged:
                assert e.payload["announcement_kind"] == "completed"
                assert e.payload["message_id"] == "msg-42"
            # Announcements still never commit engine-side.
            assert committed == []
        finally:
            await stop_pipeline(pipeline, run_task)

    async def test_reply_turn_chunks_carry_no_announcement_identity(self):
        transport = QueueTransport()
        emitter = Emitter()
        pipeline = make_pipeline(
            transport, stt=FakeStt(["hi"] * 3),
            talker=ScriptedTalker([(["Hello there."], None)]),
            emitter=emitter)
        run_task = await start_pipeline(pipeline)
        try:
            await pipeline.speak_announcement(
                "A job landed.",
                announcement_id="voice-ann:t9:completed", kind="completed")
            transport.push_frames(speech_then_silence())
            await wait_until(lambda: "Hello there" in text_deltas(emitter))
            reply_chunks = [
                e for e in emitter.of_type(EventType.VOICE_OUTPUT_CHUNK)
                if e.payload.get("text_delta")
                and "Hello" in e.payload["text_delta"]
            ]
            assert reply_chunks
            for e in reply_chunks:
                assert "announcement_id" not in e.payload
                assert "announcement_kind" not in e.payload
        finally:
            await stop_pipeline(pipeline, run_task)

    async def test_barge_in_during_announcement_tags_the_barge_in(self):
        transport = QueueTransport()
        emitter = ChunkDelayEmitter(delay_after_audio_count=1)
        stt = FakeStt(["stop", "stop", "stop"])
        talker = ScriptedTalker([(["Okay."], None)])
        pipeline = make_pipeline(
            transport, stt=stt, talker=talker, emitter=emitter)
        run_task = await start_pipeline(pipeline)
        try:
            announce_task = asyncio.ensure_future(pipeline.speak_announcement(
                "First part here now. Second part follows next.",
                announcement_id="voice-ann:t7:completed", kind="completed"))
            await wait_until(lambda: len(audio_chunks(emitter)) >= 1)
            transport.push_frames([frame(1)] * 10)
            await wait_until(
                lambda: len(emitter.of_type(EventType.VOICE_BARGE_IN)) >= 1)
            barge = emitter.of_type(EventType.VOICE_BARGE_IN)[0]
            # Tagged, so the desktop can refuse to commit the heard
            # prefix as a new message — the task's own message
            # already stands in chat.
            assert barge.payload.get("announcement_id") == "voice-ann:t7:completed"
            assert await asyncio.wait_for(announce_task, timeout=5.0) is True
        finally:
            await stop_pipeline(pipeline, run_task)


# ---------------------------------------------------------------------------
# Websocket wiring: escalation notices end to end over a real socket.
# The graph is a scripted fake; the voice stages are the 5.1a fakes.
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
    return {"tmp_path": tmp_path}


def _spoken_with_id(events, announcement_id):
    """Joined announcement text for one identity."""
    return "".join(
        e.payload["text_delta"] for e in events
        if e.type == EventType.VOICE_OUTPUT_CHUNK
        and e.payload.get("announcement_id") == announcement_id
        and e.payload.get("text_delta"))


class TestVoiceEscalationNotices:
    def test_approval_pause_is_announced_once_and_never_voice_approved(
            self, ws_env):
        async def fake_graph(**kwargs):
            yield Event(type=EventType.TASK_CREATED, payload={
                "task_id": "vtask-2", "prompt": kwargs["prompt"],
                "path": "graph"})
            yield Event(type=EventType.TOOL_APPROVAL_REQUEST, payload={
                "task_id": "vtask-2", "tool_name": "file_write",
                "args": {"path": "/tmp/report.md"},
                "approval_key": "vtask-2"})
            # The same pause re-reported (the executor can surface
            # one pause more than once): it must be announced once.
            yield Event(type=EventType.TOOL_APPROVAL_REQUEST, payload={
                "task_id": "vtask-2", "tool_name": "file_write",
                "args": {"path": "/tmp/report.md"},
                "approval_key": "vtask-2"})
            yield Event(type=EventType.TASK_INTERRUPTED, payload={
                "task_id": "vtask-2", "state": "pending_approval"})

        ann_id = "voice-ann:vtask-2:approval:vtask-2:file_write"
        # Two utterances: the escalation, then a chat turn. The
        # announcement is scheduled at the pause and speaks at the
        # first quiet moment; the second turn bounds the stream so
        # the notice deterministically lands within it.
        transport = ScriptedTransport(
            speech_then_silence(), speech_then_silence())
        stt = FakeStt([
            "book", "book the background", "book the background job",
            "how", "how are", "how are you",
        ])
        sid = session_store.create_session("Voice chat")
        with ExitStack() as stack:
            _voice_stack(stack)
            _patch_factory(stack, transport, stt)
            stack.enter_context(patch.object(
                ws_module, "classify_intent", side_effect=_fake_classify))
            stack.enter_context(patch.object(
                ws_module, "run_langgraph_task", side_effect=fake_graph))
            with TestClient(app) as client, client.websocket_connect(
                    f"/ws/tasks?token={engine_state.auth_token}") as ws:
                ws.receive_text()  # HELLO
                _start_voice_session(ws, sid)
                events = _receive_until(ws, lambda evs: any(
                    e.type == EventType.VOICE_OUTPUT_CHUNK
                    and e.payload.get("announcement_id") == ann_id
                    and e.payload.get("audio_chunk")
                    for e in evs), max_events=300)
                # The approval request itself surfaced in chat,
                # exactly as for a typed task.
                assert any(
                    e.type == EventType.TOOL_APPROVAL_REQUEST for e in events)

                flushed = _stop_voice_session(ws, sid)
                all_events = events + flushed

        notice = _spoken_with_id(all_events, ann_id)
        # Announced exactly once (the re-reported pause did not
        # re-announce), factually: paused, waiting on approval of
        # the named tool, decision lives in chat.
        assert notice == (
            "That background task is paused, waiting for your approval "
            "to use file_write. You can review and decide in the chat.")
        tagged = [
            e for e in all_events
            if e.type == EventType.VOICE_OUTPUT_CHUNK
            and e.payload.get("announcement_id") == ann_id]
        assert all(
            e.payload.get("announcement_kind") == "approval" for e in tagged)
        # The announcement is not an approval channel: the engine
        # never granted anything, and nothing was committed to the
        # session beyond the user turn and the escalation ack.
        assert not any(
            e.type in (EventType.TOOL_APPROVAL_GRANTED,
                       EventType.PLUGIN_APPROVAL_GRANTED,
                       EventType.TASK_COMPLETED)
            for e in all_events)
        sess = session_store.get_session(sid)
        contents = [(m["role"], m["content"]) for m in sess["messages"]]
        assert contents[:2] == [
            ("user", "book the background job"),
            ("assistant", DEFAULT_ESCALATION_ACK),
        ]
        # The announcement itself is never committed as a message:
        # the pause stands in chat as the task's approval card,
        # not as an assistant utterance.
        assert all(
            "waiting for your approval" not in content
            for _, content in contents)

    def test_completion_announcement_carries_the_task_message_identity(
            self, ws_env):
        gate = threading.Event()

        async def fake_graph(**kwargs):
            yield Event(type=EventType.TASK_CREATED, payload={
                "task_id": "vtask-1", "prompt": kwargs["prompt"],
                "path": "graph"})
            await asyncio.to_thread(gate.wait)
            yield Event(type=EventType.TASK_COMPLETED, payload={
                "task_id": "vtask-1",
                "response": "Report finished: 3 files changed.",
                "latency_ms": 5})

        ann_id = "voice-ann:vtask-1:completed"
        transport = ScriptedTransport(
            speech_then_silence(), speech_then_silence())
        stt = FakeStt([
            "book", "book the background", "book the background job",
            "how", "how are", "how are you",
        ])
        sid = session_store.create_session("Voice chat")
        with ExitStack() as stack:
            _voice_stack(stack)
            _patch_factory(stack, transport, stt)
            stack.enter_context(patch.object(
                ws_module, "classify_intent", side_effect=_fake_classify))
            stack.enter_context(patch.object(
                ws_module, "run_langgraph_task", side_effect=fake_graph))
            with TestClient(app) as client, client.websocket_connect(
                    f"/ws/tasks?token={engine_state.auth_token}") as ws:
                ws.receive_text()  # HELLO
                _start_voice_session(ws, sid)
                _receive_until(ws, lambda evs: any(
                    e.type == EventType.TASK_CREATED for e in evs))
                gate.set()
                events = _receive_until(ws, lambda evs: any(
                    e.type == EventType.VOICE_OUTPUT_CHUNK
                    and e.payload.get("announcement_id") == ann_id
                    and e.payload.get("audio_chunk")
                    for e in evs), max_events=300)
                completed = next(
                    e for e in events if e.type == EventType.TASK_COMPLETED)
                _stop_voice_session(ws, sid)

        # The pump stamped the persisted message's identity onto
        # the completion event, and every announcement chunk
        # carries that same identity — the desktop can match the
        # spoken summary to the task's own message instead of
        # rendering it as a second assistant turn.
        sess = session_store.get_session(sid)
        stored = [
            m for m in sess["messages"]
            if m["role"] == "assistant"
            and m["content"] == "Report finished: 3 files changed."]
        assert len(stored) == 1
        assert completed.payload.get("message_id") == stored[0]["id"]
        tagged = [
            e for e in events
            if e.type == EventType.VOICE_OUTPUT_CHUNK
            and e.payload.get("announcement_id") == ann_id]
        assert tagged
        for e in tagged:
            assert e.payload.get("announcement_kind") == "completed"
            assert e.payload.get("message_id") == stored[0]["id"]
        assert _spoken_with_id(events, ann_id) == (
            "Report finished: 3 files changed.")

    def test_failure_is_announced_once_with_identity(self, ws_env):
        async def fake_graph(**kwargs):
            yield Event(type=EventType.TASK_CREATED, payload={
                "task_id": "vtask-3", "prompt": kwargs["prompt"],
                "path": "graph"})
            yield Event(type=EventType.TASK_FAILED, payload={
                "task_id": "vtask-3", "error": "boom"})

        ann_id = "voice-ann:vtask-3:failed"
        transport = FakeTransport(speech_then_silence())
        stt = FakeStt(
            ["book", "book the background", "book the background job"])
        sid = session_store.create_session("Voice chat")
        with ExitStack() as stack:
            _voice_stack(stack)
            _patch_factory(stack, transport, stt)
            stack.enter_context(patch.object(
                ws_module, "classify_intent", side_effect=_fake_classify))
            stack.enter_context(patch.object(
                ws_module, "run_langgraph_task", side_effect=fake_graph))
            with TestClient(app) as client, client.websocket_connect(
                    f"/ws/tasks?token={engine_state.auth_token}") as ws:
                ws.receive_text()  # HELLO
                _start_voice_session(ws, sid)
                events = _receive_until(ws, lambda evs: any(
                    e.type == EventType.VOICE_OUTPUT_CHUNK
                    and e.payload.get("announcement_id") == ann_id
                    and e.payload.get("audio_chunk")
                    for e in evs), max_events=300)
                flushed = _stop_voice_session(ws, sid)

        assert _spoken_with_id(events + flushed, ann_id) == (
            "That background task failed: boom")
        tagged = [
            e for e in events + flushed
            if e.type == EventType.VOICE_OUTPUT_CHUNK
            and e.payload.get("announcement_id") == ann_id]
        assert all(
            e.payload.get("announcement_kind") == "failed" for e in tagged)
        # A failure persists no assistant message to duplicate.
        sess = session_store.get_session(sid)
        assert [(m["role"], m["content"]) for m in sess["messages"]] == [
            ("user", "book the background job"),
            ("assistant", DEFAULT_ESCALATION_ACK),
        ]
