"""Epic 8, E8-5.1b: voice barge-in + supervisor escalation.

Builds on the E8-5.1a cascade (tests/test_voice_pipeline.py) and pins
the two behaviours this slice adds:

  * Full-duplex barge-in: frames keep flowing while a reply plays;
    sustained user speech (past the endpointer's min-speech guard,
    so a cough or a "mm-hmm" does not count) stops playback at
    once, and the committed assistant message holds only the words
    actually heard — word granularity: a sentence interrupted
    mid-playback contributes the chunk-proportional prefix of its
    words. The interrupting utterance is replayed into the main
    path and becomes the next turn.
  * Escalation: a spoken turn the intent classifier calls work
    launches a real supervisor task in the background (the talker
    lane never waits on it), a spoken acknowledgement is the turn's
    reply, the task's events stream to chat with full detail, an
    approval pauses it for the normal in-chat approval (nothing in
    the voice path can approve), and when the task lands its final
    summary is spoken as an announcement.

All stages are scripted fakes (as in 5.1a): no audio devices, no
models, no network. 16 kHz mono PCM16 frames of 32 ms throughout.
"""
import asyncio
import base64
import threading
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient
from test_voice_pipeline import (
    Emitter,
    FakeStt,
    FakeTransport,
    FakeTts,
    FakeVad,
    frame,
    make_pipeline,
    speech_then_silence,
)

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.memory import session_store
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.intent import IntentDecision
from aether_engine.voice.pipeline import (
    DEFAULT_ESCALATION_ACK,
    VoicePipeline,
    create_voice_pipeline,
)

FRAME_BYTES = 1024


# ---------------------------------------------------------------------------
# Extra fakes
# ---------------------------------------------------------------------------

class QueueTransport:
    """frames() pulls from a queue the test feeds frame by frame,
    so staged scenarios (reply first, interruption later) are
    deterministic. Idles like a live mic until closed."""

    def __init__(self):
        self._queue = asyncio.Queue()
        self.opened = False
        self.closed = False
        self.config = {"iceServers": [{"urls": "stun:fake.local"}]}

    def unavailable_reason(self):
        return None

    async def open(self):
        self.opened = True
        return self.config

    def push(self, data):
        self._queue.put_nowait(data)

    def push_frames(self, frames):
        for data in frames:
            self.push(data)

    async def frames(self):
        while not self.closed:
            try:
                data = await asyncio.wait_for(self._queue.get(), timeout=0.01)
            except TimeoutError:
                continue
            yield data

    async def close(self):
        self.closed = True


class ScriptedTalker:
    """Each call pops the next scripted reply: (deltas, gate_index).
    The gate (an asyncio.Event, initially unset) blocks delivery of
    the delta at gate_index until the test sets it."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []
        self.gates = []

    async def __call__(self, text):
        self.prompts.append(text)
        deltas, gate_index = self.replies.pop(0) if self.replies else ([], None)
        gate = asyncio.Event()
        self.gates.append(gate)
        for i, delta in enumerate(deltas):
            if gate_index is not None and i == gate_index:
                await gate.wait()
            yield delta


class ChunkDelayEmitter(Emitter):
    """Sleeps AFTER recording the Nth audio chunk — the send window
    between two chunks of one sentence, in which a barge-in must
    land to stop the following chunk."""

    def __init__(self, delay_after_audio_count, delay_s=0.25):
        super().__init__()
        self.delay_after = delay_after_audio_count
        self.delay_s = delay_s
        self.audio_seen = 0

    async def __call__(self, event):
        await super().__call__(event)
        if (event.type == EventType.VOICE_OUTPUT_CHUNK
                and event.payload.get("audio_chunk")):
            self.audio_seen += 1
            if self.audio_seen == self.delay_after:
                await asyncio.sleep(self.delay_s)


class ThreeChunkTts(FakeTts):
    """Yields three audio chunks per text instead of two."""

    async def synthesize(self, text, *, voice):
        self.requests.append((text, voice))
        yield b"AUD1:" + text.encode()
        yield b"AUD2:" + text.encode()
        yield b"AUD3:" + text.encode()


class FakeClassifier:
    """verdicts maps final transcript -> bool (escalate) or an
    Exception instance to raise."""

    def __init__(self, verdicts):
        self.verdicts = verdicts
        self.calls = []

    async def __call__(self, text):
        self.calls.append(text)
        verdict = self.verdicts.get(text, False)
        if isinstance(verdict, Exception):
            raise verdict
        return verdict


class FakeEscalation:
    """Stands in for the background-task launch: records the call
    and returns at once (launching, not running, the job)."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def __call__(self, text):
        self.calls.append(text)
        if self.error is not None:
            raise self.error


async def wait_until(predicate, timeout=5.0):
    for _ in range(int(timeout / 0.002)):
        if predicate():
            return
        await asyncio.sleep(0.002)
    raise AssertionError("condition not reached in time")


def audio_chunks(emitter):
    return [e for e in emitter.of_type(EventType.VOICE_OUTPUT_CHUNK)
            if e.payload.get("audio_chunk")]


def text_deltas(emitter):
    return "".join(
        e.payload["text_delta"]
        for e in emitter.of_type(EventType.VOICE_OUTPUT_CHUNK)
        if e.payload.get("text_delta"))


async def start_pipeline(pipeline):
    await pipeline.start()
    task = asyncio.ensure_future(pipeline.run())
    # Let run() take its first step while the session is started —
    # an immediate stop() would otherwise race its entry check.
    await asyncio.sleep(0)
    return task


async def stop_pipeline(pipeline, task):
    await pipeline.stop()
    await asyncio.wait_for(task, timeout=5.0)


# ---------------------------------------------------------------------------
# Barge-in: playback stop + heard-words commit
# ---------------------------------------------------------------------------

class TestBargeIn:
    async def test_interrupt_mid_reply_commits_only_heard_words(self):
        transport = QueueTransport()
        # Sentence 1: 2 audio chunks. Sentence 2: chunks 3 and 4 —
        # the emitter pauses after chunk 3, and the barge-in lands
        # in that window, so chunk 4 must never be emitted and
        # sentence 2 contributes only its first half of words.
        emitter = ChunkDelayEmitter(delay_after_audio_count=3)
        stt = FakeStt([
            "the red", "the red bicycle", "the red bicycle",
            "never mind", "never mind", "never mind",
        ])
        talker = ScriptedTalker([
            (["One two three four. ", "Five six seven eight. ",
              "Nine ten eleven twelve."], None),
            (["Okay, dropped it."], None),
        ])
        committed = []

        async def on_assistant_final(text):
            committed.append(text)

        pipeline = make_pipeline(
            transport, stt=stt, talker=talker, emitter=emitter,
            on_assistant_final=on_assistant_final)
        run_task = await start_pipeline(pipeline)
        try:
            transport.push_frames(speech_then_silence())
            await wait_until(lambda: len(audio_chunks(emitter)) >= 3)
            # The user talks over the reply: 10 speech frames
            # (320 ms, past the 240 ms min-speech guard).
            transport.push_frames([frame(1)] * 10)
            await wait_until(
                lambda: len(emitter.of_type(EventType.VOICE_BARGE_IN)) >= 1)

            barge = emitter.of_type(EventType.VOICE_BARGE_IN)[0]
            expected_heard = "One two three four. Five six "
            assert barge.payload["session_id"] == "sess-1"
            assert barge.payload["committed_text"] == expected_heard
            assert barge.payload["stopped_at_char"] == len(expected_heard)
            # History keeps exactly the heard words.
            assert committed == ["One two three four. Five six"]
            # Chunk 4 (the second half of sentence 2) was never
            # emitted, and sentence 3 never started.
            chunks = audio_chunks(emitter)
            assert len(chunks) == 3
            spoken = b"".join(
                base64.b64decode(c.payload["audio_chunk"]) for c in chunks)
            assert b"AUD2:Five six" not in spoken
            assert b"Nine ten" not in spoken

            # The interrupting utterance becomes the next turn.
            transport.push_frames([frame(0)] * 30)
            await wait_until(lambda: len(committed) >= 2)
            assert talker.prompts == ["the red bicycle", "never mind"]
            assert committed[1] == "Okay, dropped it."
        finally:
            await stop_pipeline(pipeline, run_task)

    async def test_heard_prefix_is_proportional_to_chunks_emitted(self):
        transport = QueueTransport()
        emitter = ChunkDelayEmitter(delay_after_audio_count=1)
        stt = FakeStt(["go", "go", "go"])
        talker = ScriptedTalker([
            (["Alpha beta gamma delta epsilon zeta. ",
              "More words follow here."], None),
        ])
        committed = []

        async def on_assistant_final(text):
            committed.append(text)

        pipeline = VoicePipeline(
            session_id="sess-1",
            vad=FakeVad({1: 0.9}),
            stt=stt,
            tts=ThreeChunkTts(),
            transport=transport,
            talker=talker,
            emit=emitter,
            on_assistant_final=on_assistant_final,
        )
        run_task = await start_pipeline(pipeline)
        try:
            transport.push_frames(speech_then_silence())
            await wait_until(lambda: len(audio_chunks(emitter)) >= 1)
            transport.push_frames([frame(1)] * 10)
            await wait_until(
                lambda: len(emitter.of_type(EventType.VOICE_BARGE_IN)) >= 1)
            barge = emitter.of_type(EventType.VOICE_BARGE_IN)[0]
            # 1 of 3 chunks of a 6-word sentence heard -> 2 words.
            assert barge.payload["committed_text"] == "Alpha beta "
            assert committed == ["Alpha beta"]
            assert len(audio_chunks(emitter)) == 1
        finally:
            await stop_pipeline(pipeline, run_task)

    async def test_blip_during_reply_does_not_interrupt(self):
        transport = QueueTransport()
        emitter = Emitter()
        stt = FakeStt(["the red", "the red bicycle", "the red bicycle"])
        talker = ScriptedTalker([
            (["First part here. ", "Second part follows."], None),
        ])
        committed = []

        async def on_assistant_final(text):
            committed.append(text)

        pipeline = make_pipeline(
            transport, stt=stt, talker=talker, emitter=emitter,
            on_assistant_final=on_assistant_final)
        run_task = await start_pipeline(pipeline)
        try:
            transport.push_frames(speech_then_silence())
            await wait_until(lambda: len(audio_chunks(emitter)) >= 2)
            # A 96 ms blip (3 frames) plus a long silence tail: a
            # cough/backchannel, below the min-speech guard.
            transport.push_frames([frame(1)] * 3 + [frame(0)] * 30)
            await wait_until(lambda: len(committed) >= 1)
            assert emitter.of_type(EventType.VOICE_BARGE_IN) == []
            assert committed == ["First part here. Second part follows."]
        finally:
            await stop_pipeline(pipeline, run_task)


# ---------------------------------------------------------------------------
# Escalation: talker lane / task lane split
# ---------------------------------------------------------------------------

class TestEscalation:
    def _pipeline(self, transport, *, classifier, escalation, talker,
                  emitter, committed, stt=None):
        async def on_assistant_final(text):
            committed.append(text)

        return make_pipeline(
            transport,
            stt=stt if stt is not None else FakeStt(["do the thing"] * 6),
            talker=talker, emitter=emitter,
            on_assistant_final=on_assistant_final,
            escalation_classifier=classifier, on_escalation=escalation)

    async def test_work_turn_escalates_and_ack_is_the_reply(self):
        transport = FakeTransport(speech_then_silence())
        emitter = Emitter()
        committed = []
        classifier = FakeClassifier({"do the thing": True})
        escalation = FakeEscalation()
        talker = ScriptedTalker([(["Chat answer."], None)])
        pipeline = self._pipeline(
            transport, classifier=classifier, escalation=escalation,
            talker=talker, emitter=emitter, committed=committed)
        await pipeline.start()
        task = asyncio.ensure_future(pipeline.run())
        try:
            await wait_until(lambda: len(committed) >= 1)
        finally:
            await pipeline.stop()
            await asyncio.wait_for(task, timeout=5.0)

        assert classifier.calls == ["do the thing"]
        assert escalation.calls == ["do the thing"]
        # The talker never saw the work turn.
        assert talker.prompts == []
        # The acknowledgement streamed word by word, was spoken,
        # and is the committed assistant message for the turn.
        assert text_deltas(emitter) == DEFAULT_ESCALATION_ACK
        assert audio_chunks(emitter), "the ack must be spoken, not just shown"
        assert committed == [DEFAULT_ESCALATION_ACK]

    async def test_chat_turn_stays_with_the_talker(self):
        transport = FakeTransport(speech_then_silence())
        emitter = Emitter()
        committed = []
        classifier = FakeClassifier({})  # everything is chat
        escalation = FakeEscalation()
        talker = ScriptedTalker([(["Chat answer."], None)])
        pipeline = self._pipeline(
            transport, classifier=classifier, escalation=escalation,
            talker=talker, emitter=emitter, committed=committed)
        await pipeline.start()
        task = asyncio.ensure_future(pipeline.run())
        try:
            await wait_until(lambda: len(committed) >= 1)
        finally:
            await pipeline.stop()
            await asyncio.wait_for(task, timeout=5.0)

        assert talker.prompts == ["do the thing"]
        assert escalation.calls == []
        assert committed == ["Chat answer."]

    async def test_no_escalation_without_the_seams(self):
        # 5.1a configuration: no classifier/launch wired -> every
        # turn goes to the talker, exactly as before.
        transport = FakeTransport(speech_then_silence())
        committed = []

        async def on_assistant_final(text):
            committed.append(text)

        talker = ScriptedTalker([(["Chat answer."], None)])
        pipeline = make_pipeline(
            transport, stt=FakeStt(["do the thing"] * 3), talker=talker,
            emitter=Emitter(), on_assistant_final=on_assistant_final)
        await pipeline.start()
        task = asyncio.ensure_future(pipeline.run())
        try:
            await wait_until(lambda: len(committed) >= 1)
        finally:
            await pipeline.stop()
            await asyncio.wait_for(task, timeout=5.0)
        assert talker.prompts == ["do the thing"]
        assert committed == ["Chat answer."]

    async def test_launch_failure_falls_back_to_the_talker(self):
        transport = FakeTransport(speech_then_silence())
        emitter = Emitter()
        committed = []
        classifier = FakeClassifier({"do the thing": True})
        escalation = FakeEscalation(error=RuntimeError("no task lane"))
        talker = ScriptedTalker([(["Chat answer."], None)])
        pipeline = self._pipeline(
            transport, classifier=classifier, escalation=escalation,
            talker=talker, emitter=emitter, committed=committed)
        await pipeline.start()
        task = asyncio.ensure_future(pipeline.run())
        try:
            await wait_until(lambda: len(committed) >= 1)
        finally:
            await pipeline.stop()
            await asyncio.wait_for(task, timeout=5.0)

        assert escalation.calls == ["do the thing"]  # launch was tried
        assert talker.prompts == ["do the thing"]    # talker answered
        assert committed == ["Chat answer."]

    async def test_classifier_failure_degrades_to_the_talker(self):
        transport = FakeTransport(speech_then_silence())
        emitter = Emitter()
        committed = []
        classifier = FakeClassifier(
            {"do the thing": RuntimeError("classifier down")})
        escalation = FakeEscalation()
        talker = ScriptedTalker([(["Chat answer."], None)])
        pipeline = self._pipeline(
            transport, classifier=classifier, escalation=escalation,
            talker=talker, emitter=emitter, committed=committed)
        await pipeline.start()
        task = asyncio.ensure_future(pipeline.run())
        try:
            await wait_until(lambda: len(committed) >= 1)
        finally:
            await pipeline.stop()
            await asyncio.wait_for(task, timeout=5.0)

        assert escalation.calls == []
        assert talker.prompts == ["do the thing"]
        assert committed == ["Chat answer."]

    async def test_talker_lane_is_free_right_after_an_escalation(self):
        # Two utterances back to back: the first escalates, the
        # second is chat and is answered normally — the escalation
        # turn ends with its ack instead of waiting on any job.
        transport = FakeTransport(speech_then_silence() + speech_then_silence())
        emitter = Emitter()
        committed = []
        classifier = FakeClassifier({"do the thing": True})
        escalation = FakeEscalation()
        talker = ScriptedTalker([(["Chat answer."], None)])
        stt = FakeStt(["do the thing"] * 3 + ["just chatting"] * 3)
        pipeline = self._pipeline(
            transport, classifier=classifier, escalation=escalation,
            talker=talker, emitter=emitter, committed=committed, stt=stt)
        await pipeline.start()
        task = asyncio.ensure_future(pipeline.run())
        try:
            await wait_until(lambda: len(committed) >= 2)
        finally:
            await pipeline.stop()
            await asyncio.wait_for(task, timeout=5.0)

        assert escalation.calls == ["do the thing"]
        assert talker.prompts == ["just chatting"]
        assert committed == [DEFAULT_ESCALATION_ACK, "Chat answer."]


# ---------------------------------------------------------------------------
# Announcements: background results spoken out of turn
# ---------------------------------------------------------------------------

class TestAnnouncements:
    async def test_announcement_speaks_without_committing(self):
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
            spoken = await pipeline.speak_announcement("The report is ready.")
            assert spoken is True
            assert text_deltas(emitter) == "The report is ready."
            assert audio_chunks(emitter), "the summary must be spoken"
            # The task machinery already persisted the summary as
            # the task's own message: no voice-side commit.
            assert committed == []
        finally:
            await stop_pipeline(pipeline, run_task)

    async def test_announcement_waits_for_the_active_turn(self):
        transport = QueueTransport()
        emitter = Emitter()
        committed = []

        async def on_assistant_final(text):
            committed.append(text)

        talker = ScriptedTalker([
            (["First part. ", "Second part."], 1),  # gate before delta 1
        ])
        pipeline = make_pipeline(
            transport, stt=FakeStt(["hi"] * 3), talker=talker,
            emitter=emitter, on_assistant_final=on_assistant_final)
        run_task = await start_pipeline(pipeline)
        try:
            transport.push_frames(speech_then_silence())
            await wait_until(lambda: len(audio_chunks(emitter)) >= 2)
            announce_task = asyncio.ensure_future(
                pipeline.speak_announcement("Your job finished."))
            await asyncio.sleep(0.15)
            # The turn is still gated: nothing of the announcement
            # may leak into the output stream yet.
            assert "Your job finished." not in text_deltas(emitter)
            talker.gates[0].set()
            assert await asyncio.wait_for(announce_task, timeout=5.0) is True
            assert committed == ["First part. Second part."]
            assert "Your job finished." in text_deltas(emitter)
        finally:
            await stop_pipeline(pipeline, run_task)

    async def test_announcement_dropped_when_session_not_live(self):
        emitter = Emitter()
        pipeline = make_pipeline(QueueTransport(), emitter=emitter)
        # Never started: no device, no speech.
        assert await pipeline.speak_announcement("Too late.") is False
        assert emitter.events == []
        # Started then stopped: same honest drop.
        run_task = await start_pipeline(pipeline)
        await pipeline.stop()
        assert await pipeline.speak_announcement("Too late.") is False
        assert emitter.events == []
        await asyncio.wait_for(run_task, timeout=5.0)

    async def test_barge_in_stops_an_announcement(self):
        transport = QueueTransport()
        emitter = ChunkDelayEmitter(delay_after_audio_count=1)
        stt = FakeStt(["stop", "stop", "stop"])
        talker = ScriptedTalker([(["Okay."], None)])
        committed = []

        async def on_assistant_final(text):
            committed.append(text)

        pipeline = make_pipeline(
            transport, stt=stt, talker=talker, emitter=emitter,
            on_assistant_final=on_assistant_final)
        run_task = await start_pipeline(pipeline)
        try:
            announce_task = asyncio.ensure_future(pipeline.speak_announcement(
                "First part here now. Second part follows next. "
                "Third part ends it."))
            await wait_until(lambda: len(audio_chunks(emitter)) >= 1)
            transport.push_frames([frame(1)] * 10)
            await wait_until(
                lambda: len(emitter.of_type(EventType.VOICE_BARGE_IN)) >= 1)
            barge = emitter.of_type(EventType.VOICE_BARGE_IN)[0]
            # 1 of 2 chunks of a 4-word sentence heard -> 2 words.
            assert barge.payload["committed_text"] == "First part "
            assert await asyncio.wait_for(announce_task, timeout=5.0) is True
            spoken = b"".join(
                base64.b64decode(c.payload["audio_chunk"])
                for c in audio_chunks(emitter))
            assert b"Second part" not in spoken
            assert b"Third part" not in spoken
            # The session survives: the interrupting utterance ("stop")
            # becomes a normal talker turn.
            transport.push_frames([frame(0)] * 30)
            await wait_until(lambda: talker.prompts == ["stop"])
            await wait_until(lambda: committed == ["Okay."])
        finally:
            await stop_pipeline(pipeline, run_task)


# ---------------------------------------------------------------------------
# Websocket wiring: escalation end to end over a real socket.
# The graph is a scripted fake; the voice stages are the 5.1a fakes;
# classify_intent is patched with the same verdicts the typed path
# would produce.
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


def _receive_until(ws, predicate, max_events=120):
    events = []
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        events.append(ev)
        if predicate(events):
            return events
    raise AssertionError(
        f"predicate not reached; got {[ev.type for ev in events]}")


def _voice_stack(stack: ExitStack):
    from test_voice_pipeline import _ChunkProvider

    mock_store = stack.enter_context(
        patch.object(ws_module, "SecretStore")).return_value
    mock_store.list_providers.return_value = ["google_gemini"]
    stack.enter_context(patch.object(
        ws_module, "_create_provider_instance",
        return_value=_ChunkProvider()))
    return stack


def _patch_factory(stack: ExitStack, transport, stt):
    real_factory = create_voice_pipeline

    def factory(**kwargs):
        return real_factory(
            vad=FakeVad({1: 0.9}),
            stt=stt,
            tts=FakeTts(),
            transport=transport,
            **kwargs,
        )

    stack.enter_context(patch(
        "aether_engine.voice.pipeline.create_voice_pipeline",
        side_effect=factory))


async def _fake_classify(prompt, provider=None):
    route = "work" if "background" in prompt else "chat"
    return IntentDecision(route=route, reason="test verdict", source="model")


class ScriptedTransport(FakeTransport):
    """Utterance 1, a real pause (the ack + launch land), then
    utterance 2, then idle — the 5.1a frame script with wall-clock
    staging between the turns."""

    def __init__(self, first, second, pause_s=0.4):
        super().__init__(())
        self._first = list(first)
        self._second = list(second)
        self._pause_s = pause_s

    async def frames(self):
        for data in self._first:
            yield data
            await asyncio.sleep(0)
        await asyncio.sleep(self._pause_s)
        for data in self._second:
            yield data
            await asyncio.sleep(0)
        while not self.closed:
            await asyncio.sleep(0.005)


def _start_voice_session(ws, sid):
    ws.send_text(Event(
        type=EventType.VOICE_SESSION_START_REQUEST,
        payload={"session_id": sid},
    ).to_json())
    events = _receive_until(
        ws, lambda evs: evs[-1].type == EventType.VOICE_SESSION_START_RESPONSE)
    assert events[-1].payload["ok"] is True


def _stop_voice_session(ws, sid):
    ws.send_text(Event(
        type=EventType.VOICE_SESSION_STOP_REQUEST,
        payload={"session_id": sid},
    ).to_json())
    return _receive_until(
        ws, lambda evs: evs[-1].type == EventType.VOICE_SESSION_STOP_RESPONSE)


class TestVoiceEscalationWebsocket:
    def test_escalation_end_to_end_and_summary_spoken_on_landing(self, ws_env):
        gate = threading.Event()
        graph_prompts = []

        async def fake_graph(**kwargs):
            graph_prompts.append(kwargs["prompt"])
            yield Event(type=EventType.TASK_CREATED, payload={
                "task_id": "vtask-1", "prompt": kwargs["prompt"],
                "path": "graph"})
            await asyncio.to_thread(gate.wait)
            yield Event(type=EventType.TASK_COMPLETED, payload={
                "task_id": "vtask-1",
                "response": "Report finished: 3 files changed.",
                "latency_ms": 5})

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

                # Phase 1: the escalation launches, the ack is
                # spoken, the task card streams in, and — while the
                # job is still gated — the second (chat) utterance
                # is answered by the talker. Lane separation, live.
                phase1 = _receive_until(ws, lambda evs: any(
                    e.type == EventType.VOICE_OUTPUT_CHUNK
                    and e.payload.get("audio_chunk")
                    and base64.b64decode(e.payload["audio_chunk"])
                    == b"AUD1:Hello there"
                    for e in evs))
                types1 = [e.type for e in phase1]
                assert EventType.TASK_CREATED in types1
                created = next(
                    e for e in phase1 if e.type == EventType.TASK_CREATED)
                assert created.payload["prompt"] == "book the background job"
                assert graph_prompts == ["book the background job"]
                spoken_text = "".join(
                    e.payload["text_delta"] for e in phase1
                    if e.type == EventType.VOICE_OUTPUT_CHUNK
                    and e.payload.get("text_delta"))
                assert DEFAULT_ESCALATION_ACK in spoken_text
                assert "Hello " in spoken_text  # the talker's chat reply
                # The job has NOT completed: the gate is still shut.
                assert EventType.TASK_COMPLETED not in types1

                # Phase 2: the job lands — its summary streams as a
                # task completion AND is spoken as an announcement.
                gate.set()
                phase2 = _receive_until(ws, lambda evs: any(
                    e.type == EventType.VOICE_OUTPUT_CHUNK
                    and e.payload.get("audio_chunk")
                    and b"Report finished" in base64.b64decode(
                        e.payload["audio_chunk"])
                    for e in evs))
                types2 = [e.type for e in phase2]
                completed_at = types2.index(EventType.TASK_COMPLETED)
                # Announcement deltas are word-sized: track the
                # cumulative text to find where the summary lands.
                joined = ""
                announcement_at = None
                for i, e in enumerate(phase2):
                    if (e.type == EventType.VOICE_OUTPUT_CHUNK
                            and e.payload.get("text_delta")):
                        joined += e.payload["text_delta"]
                        if "Report finished" in joined:
                            announcement_at = i
                            break
                assert announcement_at is not None
                assert announcement_at > completed_at

                _stop_voice_session(ws, sid)

        assert transport.closed
        sess = session_store.get_session(sid)
        assert [(m["role"], m["content"]) for m in sess["messages"]] == [
            ("user", "book the background job"),
            ("assistant", DEFAULT_ESCALATION_ACK),
            ("user", "how are you"),
            ("assistant", "Hello there"),
            ("assistant", "Report finished: 3 files changed."),
        ]

    def test_approval_pauses_for_chat_and_is_never_voice_approved(self, ws_env):
        async def fake_graph(**kwargs):
            yield Event(type=EventType.TASK_CREATED, payload={
                "task_id": "vtask-2", "prompt": kwargs["prompt"],
                "path": "graph"})
            yield Event(type=EventType.TOOL_APPROVAL_REQUEST, payload={
                "task_id": "vtask-2", "tool_name": "file_write",
                "args": {"path": "/tmp/report.md"}, "approval_key": "vtask-2"})
            yield Event(type=EventType.TASK_INTERRUPTED, payload={
                "task_id": "vtask-2", "state": "pending_approval"})

        transport = FakeTransport(speech_then_silence())
        stt = FakeStt(["book", "book the background", "book the background job"])
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
                # The ack stream and the task stream interleave on
                # the socket: collect until BOTH have fully landed.
                events = _receive_until(ws, lambda evs: any(
                    e.type == EventType.TASK_INTERRUPTED for e in evs)
                    and DEFAULT_ESCALATION_ACK in "".join(
                        e.payload["text_delta"] for e in evs
                        if e.type == EventType.VOICE_OUTPUT_CHUNK
                        and e.payload.get("text_delta")))
                types = [e.type for e in events]
                # The approval request surfaced in chat, exactly as
                # for a typed task; the ack was still spoken.
                assert EventType.TOOL_APPROVAL_REQUEST in types
                approval = next(
                    e for e in events
                    if e.type == EventType.TOOL_APPROVAL_REQUEST)
                assert approval.payload["tool_name"] == "file_write"
                spoken_text = "".join(
                    e.payload["text_delta"] for e in events
                    if e.type == EventType.VOICE_OUTPUT_CHUNK
                    and e.payload.get("text_delta"))
                assert DEFAULT_ESCALATION_ACK in spoken_text

                # Flush: the stop round-trip bounds everything the
                # engine sends about this task. No completion is
                # announced and nothing approves on the user's
                # behalf — the task stays paused for the chat UI.
                flushed = _stop_voice_session(ws, sid)
                flushed_text = "".join(
                    e.payload["text_delta"] for e in flushed
                    if e.type == EventType.VOICE_OUTPUT_CHUNK
                    and e.payload.get("text_delta"))
                assert "Report finished" not in flushed_text
                assert not any(
                    e.type in (EventType.TOOL_APPROVAL_GRANTED,
                               EventType.TASK_COMPLETED)
                    for e in flushed)

        sess = session_store.get_session(sid)
        assert [(m["role"], m["content"]) for m in sess["messages"]] == [
            ("user", "book the background job"),
            ("assistant", DEFAULT_ESCALATION_ACK),
        ]
