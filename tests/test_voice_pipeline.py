"""Epic 8, E8-5.1a: streaming voice conversation — core loop.

Pins the owned cascade (VAD endpointing -> local STT -> fast-chat
talker -> local TTS) behind stage protocols, the session lifecycle,
and the device discipline from the grill (Q26-Q30):

  * partial transcripts stream as VOICE_PARTIAL_TRANSCRIPT pushes and
    the finalised user transcript is what gets committed;
  * the talker's reply streams as VOICE_OUTPUT_CHUNK text deltas and
    is spoken through the TTS stage (default voice af_heart);
  * NO stage opens anything before a session starts, and stopping the
    session closes the transport and every stage;
  * missing optional dependencies (Silero/onnxruntime, faster-whisper,
    Kokoro, aiortc) fail closed with an honest
    "voice unavailable: <reason>" — never a crash, never a silently
    opened device;
  * the barge-in seam (E8-5.1b builds the behaviour on it):
    interrupt_output() stops the in-flight reply and reports the
    committed (actually-spoken) text.

All stages are scripted fakes: no audio devices, no models, no
network. 16 kHz mono PCM16 frames of 32 ms (512 samples, 1024 bytes)
throughout, matching Silero's native frame size.
"""
import asyncio
import base64
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.memory import session_store
from aether_engine.routes import websocket as ws_module
from aether_engine.voice import VoiceUnavailableError
from aether_engine.voice.pipeline import (
    VoicePipeline,
    create_voice_pipeline,
    fast_chat_talker,
)
from aether_engine.voice.vad import Endpointer, EndpointerConfig

FRAME_BYTES = 1024  # 512 samples * 2 bytes, 32 ms @ 16 kHz
SAMPLE_RATE = 16000


def frame(prob_marker: int = 0) -> bytes:
    """One PCM frame; the marker byte lets FakeStt tell frames apart."""
    return bytes([prob_marker]) + b"\x00" * (FRAME_BYTES - 1)


# ---------------------------------------------------------------------------
# Scripted fakes
# ---------------------------------------------------------------------------

class FakeVad:
    """probability() replays a script keyed by frame marker byte."""

    def __init__(self, probs=None, reason=None):
        self.probs = probs or {}
        self.reason = reason
        self.opened = False
        self.closed = False
        self.frames_seen = 0

    def unavailable_reason(self):
        return self.reason

    async def open(self):
        self.opened = True

    async def close(self):
        self.closed = True

    async def probability(self, data, sample_rate):
        self.frames_seen += 1
        return self.probs.get(data[0], 0.0)


class FakeStt:
    """transcribe() returns the next scripted text per call."""

    def __init__(self, script=(), reason=None):
        self.script = list(script)
        self.reason = reason
        self.opened = False
        self.closed = False
        self.calls = []

    def unavailable_reason(self):
        return self.reason

    async def open(self):
        self.opened = True

    async def close(self):
        self.closed = True

    async def transcribe(self, audio, *, sample_rate):
        self.calls.append(len(audio))
        if self.script:
            return self.script.pop(0)
        return ""


class FakeTts:
    """synthesize() yields two deterministic audio chunks per text."""

    def __init__(self, reason=None):
        self.reason = reason
        self.opened = False
        self.closed = False
        self.requests = []  # (text, voice)

    def unavailable_reason(self):
        return self.reason

    async def open(self):
        self.opened = True

    async def close(self):
        self.closed = True

    async def synthesize(self, text, *, voice):
        self.requests.append((text, voice))
        yield b"AUD1:" + text.encode()
        yield b"AUD2:" + text.encode()


class FakeTransport:
    """frames() replays a fixed frame list, then idles until closed."""

    def __init__(self, frames=(), reason=None):
        self._frames = list(frames)
        self.reason = reason
        self.opened = False
        self.closed = False
        self.config = {"iceServers": [{"urls": "stun:fake.local"}]}

    def unavailable_reason(self):
        return self.reason

    async def open(self):
        if self.reason:
            raise VoiceUnavailableError(self.reason)
        self.opened = True
        return self.config

    async def frames(self):
        for data in self._frames:
            yield data
            await asyncio.sleep(0)
        # Idle like a live mic with no input until stop() closes us.
        while not self.closed:
            await asyncio.sleep(0.005)

    async def close(self):
        self.closed = True


class FakeTalker:
    """Yields scripted reply deltas; records the prompts it was given."""

    def __init__(self, deltas=("The bicycle ", "is red."), error=None):
        self.deltas = list(deltas)
        self.error = error
        self.prompts = []

    async def __call__(self, text):
        self.prompts.append(text)
        if self.error is not None:
            raise self.error
        for delta in self.deltas:
            yield delta
            await asyncio.sleep(0)


class Emitter:
    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)

    def of_type(self, event_type):
        return [e for e in self.events if e.type == event_type]


def make_pipeline(transport, *, vad=None, stt=None, tts=None, talker=None,
                  emitter=None, **kwargs):
    return VoicePipeline(
        session_id="sess-1",
        vad=vad if vad is not None else FakeVad({1: 0.9}),
        stt=stt if stt is not None else FakeStt(["the red bicycle"] * 3),
        tts=tts if tts is not None else FakeTts(),
        transport=transport,
        talker=talker if talker is not None else FakeTalker(),
        emit=emitter if emitter is not None else Emitter(),
        **kwargs,
    )


def speech_then_silence(speech_frames=10, silence_frames=30):
    """Marker 1 = speech (prob .9 via FakeVad default map), 0 = silence.

    10 speech frames = 320 ms; 30 silence frames = 960 ms, past the
    700 ms endpointing silence window.
    """
    return [frame(1)] * speech_frames + [frame(0)] * silence_frames


async def run_until(transport, pipeline, predicate, timeout=5.0):
    task = asyncio.ensure_future(pipeline.run())
    try:
        for _ in range(int(timeout / 0.005)):
            if predicate():
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("condition not reached in time")
    finally:
        await pipeline.stop()
        await asyncio.wait_for(task, timeout=5.0)
    return task


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

class TestVoiceContracts:
    def test_event_types_exist_with_exact_wire_names(self):
        expected = {
            "VOICE_SESSION_START_REQUEST": "VOICE_SESSION_START_REQUEST",
            "VOICE_SESSION_START_RESPONSE": "VOICE_SESSION_START_RESPONSE",
            "VOICE_SESSION_STOP_REQUEST": "VOICE_SESSION_STOP_REQUEST",
            "VOICE_SESSION_STOP_RESPONSE": "VOICE_SESSION_STOP_RESPONSE",
            "VOICE_PARTIAL_TRANSCRIPT": "VOICE_PARTIAL_TRANSCRIPT",
            "VOICE_OUTPUT_CHUNK": "VOICE_OUTPUT_CHUNK",
            "VOICE_BARGE_IN": "VOICE_BARGE_IN",
        }
        for name, wire in expected.items():
            assert getattr(EventType, name).value == wire


# ---------------------------------------------------------------------------
# Endpointing (VAD script -> turn decisions)
# ---------------------------------------------------------------------------

class TestEndpointer:
    def test_speech_start_then_utterance_end_after_silence_window(self):
        ep = Endpointer(EndpointerConfig(silence_duration_ms=700))
        events = []
        # 320 ms of speech (past the 240 ms min-speech guard), then
        # 22*32 = 704 ms of silence (past the 700 ms window).
        for prob in [0.0] + [0.9] * 10 + [0.0] * 22:
            events.append(ep.feed(prob, 32))
        assert events[1] == "speech_started"
        assert events[-1] == "utterance_end"
        assert events.count("utterance_end") == 1

    def test_short_silence_does_not_end_the_utterance(self):
        ep = Endpointer(EndpointerConfig(silence_duration_ms=700))
        assert ep.feed(0.9, 32) == "speech_started"
        for _ in range(10):  # 320 ms of silence — a pause, not the end
            assert ep.feed(0.0, 32) is None
        assert ep.feed(0.9, 32) is None  # still the same utterance

    def test_blip_below_min_speech_is_discarded_not_ended(self):
        ep = Endpointer(EndpointerConfig(
            silence_duration_ms=320, min_speech_ms=240))
        assert ep.feed(0.9, 32) == "speech_started"
        assert ep.feed(0.9, 32) is None  # 64 ms of speech total
        results = [ep.feed(0.0, 32) for _ in range(10)]
        assert "utterance_discarded" in results
        assert "utterance_end" not in results

    def test_max_utterance_forces_an_end(self):
        ep = Endpointer(EndpointerConfig(max_utterance_ms=320))
        assert ep.feed(0.9, 32) == "speech_started"
        results = [ep.feed(0.9, 32) for _ in range(9)]  # 288 ms more
        assert "utterance_end" in results


# ---------------------------------------------------------------------------
# The core loop
# ---------------------------------------------------------------------------

class TestVoiceLoop:
    async def test_partials_stream_then_final_then_spoken_reply(self):
        transport = FakeTransport(speech_then_silence())
        stt = FakeStt(["the", "the red", "the red bicycle"])
        tts = FakeTts()
        talker = FakeTalker()
        emitter = Emitter()
        pipeline = make_pipeline(
            transport, stt=stt, tts=tts, talker=talker, emitter=emitter)
        await pipeline.start()
        await run_until(transport, pipeline,
                        lambda: len(emitter.of_type(EventType.VOICE_OUTPUT_CHUNK)) >= 4)

        partials = emitter.of_type(EventType.VOICE_PARTIAL_TRANSCRIPT)
        assert partials, "expected streamed transcripts"
        assert all(p.payload["session_id"] == "sess-1" for p in partials)
        assert all(p.payload["speaker"] == "user" for p in partials)
        non_final = [p for p in partials if not p.payload["is_final"]]
        final = [p for p in partials if p.payload["is_final"]]
        assert non_final, "expected provisional partials before the final"
        assert len(final) == 1
        assert final[0].payload["text"] == "the red bicycle"
        # Partials are provisional display: texts grow towards the final.
        assert non_final[0].payload["text"] == "the"

        assert talker.prompts == ["the red bicycle"]

        chunks = emitter.of_type(EventType.VOICE_OUTPUT_CHUNK)
        text_deltas = [c.payload["text_delta"] for c in chunks
                       if c.payload["text_delta"]]
        assert text_deltas == ["The bicycle ", "is red."]
        assert all(c.payload["speaker"] == "assistant" for c in chunks)
        audio_chunks = [c for c in chunks if c.payload["audio_chunk"]]
        assert audio_chunks, "expected synthesized audio chunks"
        decoded = base64.b64decode(audio_chunks[0].payload["audio_chunk"])
        assert decoded.startswith(b"AUD1:")
        # The whole reply went to the TTS stage, default voice af_heart.
        assert tts.requests == [("The bicycle is red.", "af_heart")]

    async def test_final_transcript_and_reply_are_committed(self):
        committed = []

        async def on_user_final(text):
            committed.append(("user", text))

        async def on_assistant_final(text):
            committed.append(("assistant", text))

        transport = FakeTransport(speech_then_silence())
        pipeline = make_pipeline(
            transport, on_user_final=on_user_final,
            on_assistant_final=on_assistant_final, emitter=Emitter())
        await pipeline.start()
        await run_until(transport, pipeline,
                        lambda: len(committed) >= 2)
        assert committed == [
            ("user", "the red bicycle"),
            ("assistant", "The bicycle is red."),
        ]

    async def test_empty_transcript_never_reaches_the_talker(self):
        transport = FakeTransport(speech_then_silence())
        talker = FakeTalker()
        emitter = Emitter()
        pipeline = make_pipeline(
            transport, stt=FakeStt(["", ""]), talker=talker, emitter=emitter)
        await pipeline.start()
        task = asyncio.ensure_future(pipeline.run())
        await asyncio.sleep(0.3)
        await pipeline.stop()
        await asyncio.wait_for(task, timeout=5.0)
        assert talker.prompts == []
        assert emitter.of_type(EventType.VOICE_OUTPUT_CHUNK) == []

    async def test_talker_failure_is_reported_and_listening_continues(self):
        errors = []

        async def on_turn_error(message):
            errors.append(message)

        transport = FakeTransport(speech_then_silence() + speech_then_silence())
        talker = FakeTalker(error=RuntimeError("brain exploded"))
        # Partials consume script entries too: each utterance is
        # transcribed for two partials and one final.
        pipeline = make_pipeline(
            transport, stt=FakeStt(["one", "one", "one", "two", "two", "two"]),
            talker=talker, on_turn_error=on_turn_error, emitter=Emitter())
        await pipeline.start()
        await run_until(transport, pipeline, lambda: len(errors) >= 1)
        assert "brain exploded" in errors[0]


# ---------------------------------------------------------------------------
# Lifecycle / device discipline
# ---------------------------------------------------------------------------

class TestLifecycle:
    async def test_nothing_opens_until_start_and_everything_closes_on_stop(self):
        transport = FakeTransport(speech_then_silence())
        vad, stt, tts = FakeVad({1: 0.9}), FakeStt(["hi"]), FakeTts()
        pipeline = make_pipeline(transport, vad=vad, stt=stt, tts=tts)
        # Construction alone must not touch a device or a model.
        assert not (transport.opened or vad.opened or stt.opened or tts.opened)

        await pipeline.start()
        assert transport.opened and vad.opened and stt.opened and tts.opened

        task = asyncio.ensure_future(pipeline.run())
        await asyncio.sleep(0.05)
        await pipeline.stop()
        await asyncio.wait_for(task, timeout=5.0)
        assert transport.closed and vad.closed and stt.closed and tts.closed
        # stop() is idempotent.
        await pipeline.stop()

    async def test_start_reports_webrtc_config_from_transport(self):
        transport = FakeTransport(())
        pipeline = make_pipeline(transport)
        config = await pipeline.start()
        assert config == {"iceServers": [{"urls": "stun:fake.local"}]}
        await pipeline.stop()


class TestFailClosed:
    async def test_missing_stage_fails_start_without_opening_anything(self):
        transport = FakeTransport(speech_then_silence())
        vad = FakeVad({1: 0.9})
        stt = FakeStt(["hi"], reason="faster-whisper is not installed")
        pipeline = make_pipeline(transport, vad=vad, stt=stt)
        with pytest.raises(VoiceUnavailableError) as excinfo:
            await pipeline.start()
        assert "voice unavailable" in str(excinfo.value)
        assert "faster-whisper is not installed" in str(excinfo.value)
        assert not transport.opened and not vad.opened and not stt.opened

    async def test_factory_returns_honest_error_for_unavailable_stage(self):
        pipeline, error = create_voice_pipeline(
            session_id="s",
            talker=FakeTalker(),
            emit=Emitter(),
            vad=FakeVad({1: 0.9}),
            stt=FakeStt(["hi"]),
            tts=FakeTts(),
            transport=FakeTransport((), reason="aiortc is not installed"),
        )
        assert pipeline is None
        assert error is not None and error.startswith("voice unavailable:")
        assert "aiortc is not installed" in error

    async def test_factory_builds_pipeline_when_all_stages_available(self):
        pipeline, error = create_voice_pipeline(
            session_id="s",
            talker=FakeTalker(),
            emit=Emitter(),
            vad=FakeVad({1: 0.9}),
            stt=FakeStt(["hi"]),
            tts=FakeTts(),
            transport=FakeTransport(()),
        )
        assert error is None and pipeline is not None

    def test_real_stages_report_unavailable_when_deps_missing(self, monkeypatch):
        """With the voice runtime deps missing, every real stage must
        say so — the fail-closed contract the UI shows. Absence is
        simulated by hiding the packages from find_spec (the stages'
        only probe), so the test holds whether or not the packages
        are installed in the environment — they are, since the voice
        packaging slice declared them in pyproject."""
        import importlib.util

        from aether_engine.voice.stt import FasterWhisperStt
        from aether_engine.voice.tts import KokoroTts
        from aether_engine.voice.vad import SileroVad

        voice_imports = {
            "faster_whisper", "kokoro", "onnxruntime", "numpy", "silero_vad",
        }
        real_find_spec = importlib.util.find_spec

        def hidden_find_spec(name, package=None):
            if name in voice_imports:
                return None
            return real_find_spec(name, package)

        monkeypatch.setattr(importlib.util, "find_spec", hidden_find_spec)

        for stage in (SileroVad(), FasterWhisperStt(), KokoroTts()):
            reason = stage.unavailable_reason()
            assert reason is not None and "not installed" in reason

    async def test_default_factory_fails_closed_in_this_environment(self):
        pipeline, error = create_voice_pipeline(
            session_id="s", talker=FakeTalker(), emit=Emitter())
        assert pipeline is None
        assert error is not None and error.startswith("voice unavailable:")


# ---------------------------------------------------------------------------
# Barge-in seam (behaviour lands in E8-5.1b; the pipeline support is here)
# ---------------------------------------------------------------------------

class TestBargeInSeam:
    async def test_interrupt_output_reports_committed_text_and_stops(self):
        transport = FakeTransport(speech_then_silence())
        committed = []

        async def on_assistant_final(text):
            committed.append(text)

        gate = asyncio.Event()

        class SlowTalker:
            async def __call__(self, text):
                yield "First sentence here. "
                await gate.wait()  # held until the test interrupts
                yield "Second sentence never heard."

        emitter = Emitter()
        pipeline = make_pipeline(
            transport, talker=SlowTalker(), emitter=emitter,
            on_assistant_final=on_assistant_final)
        await pipeline.start()
        task = asyncio.ensure_future(pipeline.run())
        # Wait until the first sentence has actually been spoken (its
        # audio chunks emitted), then interrupt.
        for _ in range(1000):
            chunks = emitter.of_type(EventType.VOICE_OUTPUT_CHUNK)
            if any(c.payload["audio_chunk"] for c in chunks):
                break
            await asyncio.sleep(0.005)
        # Queue the interrupt BEFORE releasing the talker, so the
        # flag is set when the next delta is considered.
        interrupt_task = asyncio.ensure_future(pipeline.interrupt_output())
        gate.set()
        result = await interrupt_task
        await asyncio.sleep(0.1)
        await pipeline.stop()
        await asyncio.wait_for(task, timeout=5.0)

        assert result["committed_text"] == "First sentence here. "
        assert result["stopped_at_char"] == len("First sentence here. ")
        # Only the heard words are committed to history.
        assert committed == ["First sentence here."]
        spoken = b"".join(
            base64.b64decode(c.payload["audio_chunk"])
            for c in emitter.of_type(EventType.VOICE_OUTPUT_CHUNK)
            if c.payload["audio_chunk"])
        assert b"Second sentence" not in spoken


# ---------------------------------------------------------------------------
# Fast-chat talker adapter
# ---------------------------------------------------------------------------

class _ChunkProvider:
    def __init__(self, chunks=("Hello ", "there")):
        self.chunks = list(chunks)

    async def call_stream(self, messages, tools=None, node_name=None, **kw):
        from aether_engine.providers.base import StreamChunk
        for text in self.chunks:
            yield StreamChunk(text=text)
        yield StreamChunk(finish_reason="stop")


class TestFastChatTalker:
    async def test_talker_streams_fast_chat_deltas(self):
        talker = fast_chat_talker(_ChunkProvider(), session_id=None)
        deltas = [d async for d in talker("hi there")]
        assert deltas == ["Hello ", "there"]

    async def test_talker_raises_on_fast_chat_failure(self):
        class BadProvider:
            async def call_stream(self, messages, tools=None, node_name=None, **kw):
                raise RuntimeError("provider down")
                yield  # pragma: no cover — makes this an async generator

        talker = fast_chat_talker(BadProvider(), session_id=None)
        with pytest.raises(Exception, match="provider down"):
            [d async for d in talker("hi")]


# ---------------------------------------------------------------------------
# Websocket handlers (over a real socket, house pattern from
# test_epic1_fast_chat): start/stop wiring + fail-closed start.
# ---------------------------------------------------------------------------

@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    """Engine state + stores isolated to tmp, mirroring
    test_epic1_fast_chat. The app lifespan rotates the auth token at
    TestClient startup — read it after entering the context."""
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    engine_state.initialize_auth()
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))
    return {"tmp_path": tmp_path}


def _receive_until(ws, wanted, max_events=40):
    events = []
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        events.append(ev)
        if ev.type == wanted:
            return events
    raise AssertionError(
        f"{wanted} not received; got {[ev.type for ev in events]}")


def _voice_stack(stack: ExitStack):
    """Patch the provider factory + secret store the handler uses."""
    mock_store = stack.enter_context(
        patch.object(ws_module, "SecretStore")).return_value
    mock_store.list_providers.return_value = ["google_gemini"]
    stack.enter_context(patch.object(
        ws_module, "_create_provider_instance",
        return_value=_ChunkProvider()))
    return stack


class TestVoiceWebsocketHandlers:
    def test_start_fails_closed_when_stages_unavailable(self, ws_env):
        """No voice deps in this environment: the start response is
        ok=False with the honest reason, and no session task runs."""
        with ExitStack() as stack:
            _voice_stack(stack)
            with TestClient(app) as client, client.websocket_connect(
                    f"/ws/tasks?token={engine_state.auth_token}") as ws:
                ws.receive_text()  # HELLO
                ws.send_text(Event(
                    type=EventType.VOICE_SESSION_START_REQUEST,
                    payload={"session_id": None},
                ).to_json())
                events = _receive_until(
                    ws, EventType.VOICE_SESSION_START_RESPONSE)
        payload = events[-1].payload
        assert payload["ok"] is False
        assert payload["error"].startswith("voice unavailable:")
        assert payload["webrtc_config"] is None
        assert payload["session_id"]  # a session was still created

    def test_stop_without_a_session_is_ok(self, ws_env):
        with ExitStack() as stack:
            _voice_stack(stack)
            with TestClient(app) as client, client.websocket_connect(
                    f"/ws/tasks?token={engine_state.auth_token}") as ws:
                ws.receive_text()  # HELLO
                ws.send_text(Event(
                    type=EventType.VOICE_SESSION_STOP_REQUEST,
                    payload={"session_id": "none"},
                ).to_json())
                events = _receive_until(
                    ws, EventType.VOICE_SESSION_STOP_RESPONSE)
        assert events[-1].payload == {"ok": True}

    def test_full_voice_session_over_the_socket(self, ws_env):
        """With stages injected, a start streams the whole turn over
        the socket and commits both sides to the session store."""
        transport = FakeTransport(speech_then_silence())
        real_factory = create_voice_pipeline

        def factory(**kwargs):
            return real_factory(
                vad=FakeVad({1: 0.9}),
                stt=FakeStt(["the red bicycle"] * 3),
                tts=FakeTts(),
                transport=transport,
                **kwargs,
            )

        sid = session_store.create_session("Voice chat")
        with ExitStack() as stack:
            _voice_stack(stack)
            stack.enter_context(patch(
                "aether_engine.voice.pipeline.create_voice_pipeline",
                side_effect=factory))
            # E8-5.1b wired the E1-04 intent split into the voice
            # path: this scenario is a CHAT turn, so pin the
            # classifier's verdict (the real classifier biases
            # ambiguous phrases like "the red bicycle" to work).
            from aether_engine.routing.intent import IntentDecision

            async def _chat_verdict(prompt, provider=None):
                return IntentDecision(
                    route="chat", reason="test verdict", source="model")

            stack.enter_context(patch.object(
                ws_module, "classify_intent", side_effect=_chat_verdict))
            with TestClient(app) as client, client.websocket_connect(
                    f"/ws/tasks?token={engine_state.auth_token}") as ws:
                ws.receive_text()  # HELLO
                ws.send_text(Event(
                    type=EventType.VOICE_SESSION_START_REQUEST,
                    payload={"session_id": sid},
                ).to_json())
                events = _receive_until(
                    ws, EventType.VOICE_SESSION_START_RESPONSE)
                assert events[-1].payload["ok"] is True
                assert events[-1].payload["session_id"] == sid
                assert events[-1].payload["webrtc_config"] == transport.config

                # The turn streams in: collect until spoken audio lands.
                seen = []
                for _ in range(40):
                    ev = Event.from_json(ws.receive_text())
                    seen.append(ev)
                    if (ev.type == EventType.VOICE_OUTPUT_CHUNK
                            and ev.payload.get("audio_chunk")):
                        break
                finals = [e for e in seen
                          if e.type == EventType.VOICE_PARTIAL_TRANSCRIPT
                          and e.payload["is_final"]]
                assert finals and finals[0].payload["text"] == "the red bicycle"

                ws.send_text(Event(
                    type=EventType.VOICE_SESSION_STOP_REQUEST,
                    payload={"session_id": sid},
                ).to_json())
                events = _receive_until(
                    ws, EventType.VOICE_SESSION_STOP_RESPONSE)
                assert events[-1].payload == {"ok": True}

        assert transport.closed
        sess = session_store.get_session(sid)
        assert [(m["role"], m["content"]) for m in sess["messages"]] == [
            ("user", "the red bicycle"),
            ("assistant", "Hello there"),
        ]
