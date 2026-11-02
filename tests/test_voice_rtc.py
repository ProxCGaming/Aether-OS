"""F-12a (Astra M18): the production SmallWebRTC voice transport.

Before this slice ``WebRtcVoiceTransport`` was a permanent stub:
``unavailable_reason()`` always answered and ``open()``/``frames()``
always raised, so a streaming voice session could never start.
These tests pin the real thing:

  * availability is the aiortc probe only — present means the
    transport can run, missing means the honest reason;
  * negotiation is a real offer/answer exchange: a second aiortc
    peer (the "renderer") offers, the transport answers, ICE
    trickles both ways over the wire-shaped candidate dicts, and
    both peers reach connectionState "connected" over UDP loopback;
  * audio actually flows: the renderer peer sends 48 kHz tone
    frames from a generated track; ``frames()`` yields them
    resampled to PCM16 mono at the session sample rate, with the
    tone's energy intact (not silence, not garbage);
  * a full VoicePipeline runs on the real transport — fake
    VAD/STT/TTS/talker stages, real peer connection — and a spoken
    tone burst becomes a final transcript and a spoken reply.

Environment note: this VM's sandbox refuses every UDP send
(EPERM — aioice logs "Operation not permitted" for its own
checks), so no ICE connection can complete here no matter how
correct the code is. The media tests therefore split honestly:
negotiation and the whole track -> resample -> frames -> pipeline
path run everywhere (the track is delivered through the same
"track" event aiortc fires on setRemoteDescription), while the
end-to-end *connected* test is gated on a UDP-loopback probe and
runs for real on any UDP-capable host — it skips, loudly, here.
  * the websocket signalling handlers (VOICE_RTC_OFFER_* /
    VOICE_RTC_ICE_*) route to the live session's pipeline, push the
    engine's own candidates out as VOICE_RTC_ICE, and fail closed
    with no session;
  * misuse fails closed with VoiceUnavailableError, never a crash:
    negotiation before open, malformed offers/candidates, and a
    pipeline whose transport does not negotiate.

No microphone, no models, no external network: media is generated
in-process and the peer connection is loopback.
"""
import asyncio
import fractions
import time
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.memory import session_store
from aether_engine.routes import websocket as ws_module
from aether_engine.voice import VoiceUnavailableError
from aether_engine.voice.pipeline import VoicePipeline, create_voice_pipeline
from aether_engine.voice.rtc import WebRtcVoiceTransport

RENDERER_RATE = 48000
SESSION_RATE = 16000
FRAME_SAMPLES = 960  # 20 ms at 48 kHz


# ---------------------------------------------------------------------------
# Renderer-side helpers: a generated audio track + wire conversion
# ---------------------------------------------------------------------------

class _ToneScript:
    """The segment script behind a generated aiortc audio track.

    Segments are (seconds, frequency | None): a tone at that
    frequency, or silence. After the script, silence forever — the
    peer stays connected like an idle microphone.
    """

    def __init__(self, segments):
        import numpy as np

        self._np = np
        self.segments = segments

    def frame_pcm(self, frame_index: int):
        """int16 samples for one 20 ms frame by absolute index."""
        np = self._np
        start_sample = frame_index * FRAME_SAMPLES
        elapsed = start_sample / RENDERER_RATE
        freq = None
        boundary = 0.0
        for seconds, segment_freq in self.segments:
            boundary += seconds
            if elapsed < boundary:
                freq = segment_freq
                break
        if freq is None:
            return np.zeros(FRAME_SAMPLES, dtype=np.int16)
        t = (start_sample + np.arange(FRAME_SAMPLES)) / RENDERER_RATE
        return (0.5 * 32767 * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def _make_tone_track(segments):
    """Build the aiortc track (subclass created lazily so importing
    this module never requires aiortc at collection time)."""
    import av
    from aiortc.mediastreams import AudioStreamTrack

    script = _ToneScript(segments)

    class Track(AudioStreamTrack):
        def __init__(self):
            super().__init__()
            self._frame_index = 0
            self._started_at = None
            self._pts = 0

        async def recv(self):
            if self._started_at is None:
                self._started_at = time.time()
            samples = script.frame_pcm(self._frame_index)
            self._frame_index += 1
            frame = av.AudioFrame(
                format="s16", layout="mono", samples=FRAME_SAMPLES)
            frame.planes[0].update(samples.tobytes())
            frame.sample_rate = RENDERER_RATE
            frame.pts = self._pts
            frame.time_base = fractions.Fraction(1, RENDERER_RATE)
            self._pts += FRAME_SAMPLES
            wait = self._started_at + self._pts / RENDERER_RATE - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            return frame

    return Track()


def _serialize_candidate(candidate):
    """aiortc candidate -> the wire dict the desktop sends."""
    if candidate is None:
        return None
    from aiortc.sdp import candidate_to_sdp

    return {
        "candidate": candidate_to_sdp(candidate),
        "sdpMid": candidate.sdpMid,
        "sdpMLineIndex": candidate.sdpMLineIndex,
    }


def _parse_candidate(wire):
    """Wire dict -> aiortc candidate, as the receiving side builds."""
    if wire is None:
        return None
    from aiortc.sdp import candidate_from_sdp

    parsed = candidate_from_sdp(wire["candidate"])
    parsed.sdpMid = wire.get("sdpMid")
    parsed.sdpMLineIndex = wire.get("sdpMLineIndex")
    return parsed


def _sdp_wire_candidates(sdp):
    """The a=candidate lines of an SDP as wire dicts — how either
    side's gathered candidates are read off a description (aiortc
    gathers inside setLocalDescription; there is no event)."""
    from aiortc.sdp import candidate_from_sdp

    wires = []
    m_index = -1
    mid = None
    for line in sdp.splitlines():
        if line.startswith("m="):
            m_index += 1
            mid = None
        elif line.startswith("a=mid:"):
            mid = line[len("a=mid:"):]
        elif line.startswith("a=candidate:"):
            candidate = candidate_from_sdp(line[len("a="):])
            candidate.sdpMid = mid
            candidate.sdpMLineIndex = m_index
            wires.append(_serialize_candidate(candidate))
    return wires


async def _negotiate(transport, renderer_pc):
    """Drive a full offer/answer + ICE exchange between a renderer
    peer and the engine transport, both directions over wire
    dicts. Engine candidates (dispatched by the transport during
    handle_offer) are applied only after the renderer has its
    remote description — the order real trickle consumers use."""
    engine_candidates = []

    async def _engine_ice(wire):
        engine_candidates.append(wire)

    transport.set_local_candidate_listener(_engine_ice)

    offer = await renderer_pc.createOffer()
    await renderer_pc.setLocalDescription(offer)
    local = renderer_pc.localDescription
    renderer_candidates = _sdp_wire_candidates(local.sdp)
    assert renderer_candidates, "renderer gathered no ICE candidates"

    answer = await transport.handle_offer(
        {"type": local.type, "sdp": local.sdp})
    assert answer["type"] == "answer"
    assert "m=audio" in answer["sdp"]
    from aiortc import RTCSessionDescription

    await renderer_pc.setRemoteDescription(
        RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
    for wire in engine_candidates:
        if wire is not None:
            await renderer_pc.addIceCandidate(_parse_candidate(wire))
    for wire in renderer_candidates:
        await transport.add_remote_candidate(wire)
    await transport.add_remote_candidate(None)  # end-of-candidates
    return engine_candidates, renderer_candidates


async def _wait_connected(transport, renderer_pc, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (renderer_pc.connectionState == "connected"
                and transport.connection_state == "connected"):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"peers never connected: renderer={renderer_pc.connectionState} "
        f"engine={transport.connection_state}")


async def _udp_loopback_works() -> bool:
    """Probe the one thing the connected-media test needs: a UDP
    datagram sent to 127.0.0.1 arriving. This VM's sandbox refuses
    UDP sends outright (EPERM), which no WebRTC implementation can
    work around; on a normal host the probe passes and the
    connected test runs for real."""
    received = asyncio.Event()

    class _Probe(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            received.set()

    loop = asyncio.get_running_loop()
    try:
        endpoint, _ = await loop.create_datagram_endpoint(
            _Probe, local_addr=("127.0.0.1", 0))
    except Exception:  # noqa: BLE001 — any failure means UDP is not proven usable
        return False
    try:
        port = endpoint.get_extra_info("sockname")[1]
        endpoint.sendto(b"probe", ("127.0.0.1", port))
        await asyncio.wait_for(received.wait(), timeout=2.0)
        return True
    except Exception:  # noqa: BLE001 — as above: unproven is treated as blocked
        return False
    finally:
        endpoint.close()


def _deliver_track(transport, track) -> None:
    """Fire the transport peer connection's "track" event with a
    locally generated track — the exact event aiortc emits from
    setRemoteDescription when a remote track arrives, and the
    exact entry point (the transport's pump) a connected peer's
    media takes. Only the network leg is substituted; everything
    from the track onward is production code."""
    transport._pc.emit("track", track)


# ---------------------------------------------------------------------------
# Scripted pipeline stages (energy VAD reads the real tone frames)
# ---------------------------------------------------------------------------

class EnergyVad:
    """Speech probability from the frame's peak amplitude."""

    def __init__(self):
        self.opened = False
        self.closed = False

    def unavailable_reason(self):
        return None

    async def open(self):
        self.opened = True

    async def close(self):
        self.closed = True

    async def probability(self, data, sample_rate):
        import numpy as np

        if len(data) < 2:
            return 0.0
        samples = np.frombuffer(data[: len(data) // 2 * 2], dtype=np.int16)
        if samples.size == 0:
            return 0.0
        return 0.9 if int(np.abs(samples).max()) > 4000 else 0.0


class LengthStt:
    """Partial while the utterance is short, final text when long."""

    def __init__(self):
        self.opened = False
        self.closed = False
        self.calls = []

    def unavailable_reason(self):
        return None

    async def open(self):
        self.opened = True

    async def close(self):
        self.closed = True

    async def transcribe(self, audio, *, sample_rate):
        self.calls.append(len(audio))
        seconds = len(audio) / 2 / sample_rate
        return "hello aether" if seconds >= 0.45 else "hello"


class FakeTts:
    def __init__(self):
        self.opened = False
        self.closed = False
        self.requests = []

    def unavailable_reason(self):
        return None

    async def open(self):
        self.opened = True

    async def close(self):
        self.closed = True

    async def synthesize(self, text, *, voice):
        self.requests.append((text, voice))
        yield b"AUD1:" + text.encode()
        yield b"AUD2:" + text.encode()


class FakeTalker:
    def __init__(self):
        self.prompts = []

    async def __call__(self, text):
        self.prompts.append(text)
        yield "Hi there."


class Emitter:
    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)

    def of_type(self, event_type):
        return [e for e in self.events if e.type == event_type]


# ---------------------------------------------------------------------------
# Availability + misuse
# ---------------------------------------------------------------------------

class TestAvailability:
    def test_transport_available_with_aiortc_present(self):
        assert WebRtcVoiceTransport().unavailable_reason() is None

    def test_transport_unavailable_without_aiortc(self, monkeypatch):
        import importlib.util

        real_find_spec = importlib.util.find_spec

        def hidden(name, package=None):
            if name == "aiortc":
                return None
            return real_find_spec(name, package)

        monkeypatch.setattr(importlib.util, "find_spec", hidden)
        transport = WebRtcVoiceTransport()
        reason = transport.unavailable_reason()
        assert reason is not None and "aiortc is not installed" in reason

    async def test_negotiation_before_open_fails_closed(self):
        transport = WebRtcVoiceTransport()
        with pytest.raises(VoiceUnavailableError):
            await transport.handle_offer({"type": "offer", "sdp": "v=0\r\n"})
        with pytest.raises(VoiceUnavailableError):
            await transport.add_remote_candidate(
                {"candidate": "candidate:1 1 udp 1 127.0.0.1 9 typ host"})
        with pytest.raises(VoiceUnavailableError):
            async for _ in transport.frames():
                pass

    async def test_malformed_offer_and_candidate_fail_closed(self):
        from aiortc import RTCPeerConnection

        transport = WebRtcVoiceTransport()
        await transport.open()
        renderer = RTCPeerConnection()
        try:
            with pytest.raises(VoiceUnavailableError):
                await transport.handle_offer({"type": "offer", "sdp": ""})
            with pytest.raises(VoiceUnavailableError):
                await transport.handle_offer({"sdp": "v=0\r\n"})
            with pytest.raises(VoiceUnavailableError):
                await transport.add_remote_candidate(
                    {"candidate": "not-a-candidate"})
        finally:
            await transport.close()
            await renderer.close()

    async def test_pipeline_delegation_requires_a_negotiating_transport(self):
        class BareTransport:
            def unavailable_reason(self):
                return None

            async def open(self):
                return None

            async def frames(self):
                yield b""
                await asyncio.sleep(3600)

            async def close(self):
                return None

        pipeline = VoicePipeline(
            session_id="s",
            vad=EnergyVad(), stt=LengthStt(), tts=FakeTts(),
            transport=BareTransport(), talker=FakeTalker(), emit=Emitter())
        with pytest.raises(VoiceUnavailableError):
            await pipeline.handle_rtc_offer({"type": "offer", "sdp": "v=0"})
        with pytest.raises(VoiceUnavailableError):
            await pipeline.add_rtc_ice_candidate(None)
        # The ICE listener seam is best-effort: a transport without
        # one simply never pushes candidates.
        pipeline.set_rtc_ice_listener(lambda candidate: asyncio.sleep(0))


# ---------------------------------------------------------------------------
# Real negotiation + media flow (loopback peer connection)
# ---------------------------------------------------------------------------

class TestLoopbackNegotiation:
    async def test_offer_answer_and_candidate_wire(self):
        """A real aiortc offer is answered by the transport, and
        candidates cross both ways in wire shape — the whole
        signalling contract, no network leg needed."""
        from aiortc import RTCPeerConnection

        transport = WebRtcVoiceTransport()
        config = await transport.open()
        assert config == {"iceServers": []}

        renderer = RTCPeerConnection()
        renderer.addTrack(_make_tone_track([(2.0, 440), (60.0, None)]))
        try:
            engine_candidates, renderer_candidates = await _negotiate(
                transport, renderer)
            assert any(c is not None for c in renderer_candidates)
            assert any(c is not None for c in engine_candidates)
            wire = next(c for c in engine_candidates if c is not None)
            assert set(wire) == {"candidate", "sdpMid", "sdpMLineIndex"}
            assert wire["candidate"].startswith("candidate:")
            # Both peers accepted the exchange and are connecting
            # (completion is the UDP-gated test below).
            assert transport.connection_state in ("connecting", "connected")
        finally:
            await transport.close()
            await renderer.close()

    async def test_frames_resample_a_delivered_track(self):
        """The media path: a track delivered to the peer connection
        is decoded and resampled into frames() — PCM16 mono at the
        session rate, tone energy intact — and closing the
        transport ends the stream."""
        transport = WebRtcVoiceTransport()
        await transport.open()
        _deliver_track(
            transport, _make_tone_track([(2.0, 440), (60.0, None)]))
        frames_iter = transport.frames()
        try:
            collected = bytearray()
            deadline = time.time() + 10.0
            while len(collected) < SESSION_RATE * 2 * 0.6:
                if time.time() > deadline:
                    raise AssertionError("frames stalled before 0.6 s")
                chunk = await asyncio.wait_for(
                    frames_iter.__anext__(), timeout=5.0)
                assert len(chunk) % 2 == 0
                collected += chunk
            import numpy as np

            samples = np.frombuffer(bytes(collected), dtype=np.int16)
            assert int(np.abs(samples).max()) > 8000
        finally:
            await transport.close()

        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(frames_iter.__anext__(), timeout=5.0)

    async def test_connected_peers_stream_audio(self):
        """The full loopback connection: two real peers connect
        over UDP and the tone streams through. Gated on the UDP
        probe — this VM's sandbox refuses UDP sends (EPERM), so
        here it skips; on a UDP-capable host it runs for real."""
        if not await _udp_loopback_works():
            pytest.skip(
                "UDP loopback is blocked in this environment "
                "(sendto EPERM); the connected-media proof needs a "
                "UDP-capable host")
        from aiortc import RTCPeerConnection

        transport = WebRtcVoiceTransport()
        await transport.open()
        renderer = RTCPeerConnection()
        renderer.addTrack(_make_tone_track([(2.0, 440), (60.0, None)]))
        frames_iter = transport.frames()
        try:
            await _negotiate(transport, renderer)
            await _wait_connected(transport, renderer)
            collected = bytearray()
            deadline = time.time() + 10.0
            while len(collected) < SESSION_RATE * 2 * 0.6:
                if time.time() > deadline:
                    raise AssertionError("frames stalled before 0.6 s")
                chunk = await asyncio.wait_for(
                    frames_iter.__anext__(), timeout=5.0)
                collected += chunk
            import numpy as np

            samples = np.frombuffer(bytes(collected), dtype=np.int16)
            assert int(np.abs(samples).max()) > 8000
        finally:
            await transport.close()
            await renderer.close()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(frames_iter.__anext__(), timeout=5.0)

    async def test_pipeline_runs_a_full_turn_on_the_real_transport(self):
        """Negotiated session, real transport, fake stages: a tone
        burst delivered as the peer's track becomes a final
        transcript and a spoken reply through the whole cascade."""
        from aiortc import RTCPeerConnection

        transport = WebRtcVoiceTransport()
        stt = LengthStt()
        tts = FakeTts()
        talker = FakeTalker()
        emitter = Emitter()
        committed = []

        async def on_user_final(text):
            committed.append(("user", text))

        async def on_assistant_final(text):
            committed.append(("assistant", text))

        pipeline = VoicePipeline(
            session_id="sess-rtc",
            vad=EnergyVad(), stt=stt, tts=tts, transport=transport,
            talker=talker, emit=emitter,
            on_user_final=on_user_final,
            on_assistant_final=on_assistant_final)
        config = await pipeline.start()
        assert config == {"iceServers": []}

        renderer = RTCPeerConnection()
        renderer.addTrack(_make_tone_track([(30.0, None)]))
        run_task = asyncio.ensure_future(pipeline.run())
        try:
            await _negotiate(transport, renderer)
            # 0.3 s silence, 0.6 s tone (the "utterance"), then
            # silence: past the 700 ms endpointing window the turn
            # finalises.
            _deliver_track(transport, _make_tone_track(
                [(0.3, None), (0.6, 440), (30.0, None)]))
            deadline = time.time() + 20.0
            while time.time() < deadline:
                chunks = emitter.of_type(EventType.VOICE_OUTPUT_CHUNK)
                if any(c.payload["audio_chunk"] for c in chunks):
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("no spoken reply over the real transport")
        finally:
            await pipeline.stop()
            await asyncio.wait_for(run_task, timeout=5.0)
            await renderer.close()

        finals = [e for e in emitter.of_type(EventType.VOICE_PARTIAL_TRANSCRIPT)
                  if e.payload["is_final"]]
        assert finals and finals[0].payload["text"] == "hello aether"
        assert talker.prompts == ["hello aether"]
        assert tts.requests == [("Hi there.", "af_heart")]
        assert committed == [
            ("user", "hello aether"), ("assistant", "Hi there.")]
        # The STT stage really received the resampled utterance.
        assert stt.calls and max(stt.calls) > SESSION_RATE  # > 0.5 s


# ---------------------------------------------------------------------------
# Websocket signalling (real socket; the transport is the real one,
# the model stages are fakes — media proof lives in the loopback
# tests above, this layer proves the signalling wiring)
# ---------------------------------------------------------------------------

@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    saved_token_path = engine_state.token_path
    saved_log_path = engine_state.audit_logger.log_path
    saved_user_config = engine_state.user_config
    saved_auth_token = engine_state.auth_token
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    engine_state.initialize_auth()
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))
    try:
        yield {"tmp_path": tmp_path}
    finally:
        engine_state.token_path = saved_token_path
        engine_state.audit_logger.log_path = saved_log_path
        engine_state.user_config = saved_user_config
        engine_state.auth_token = saved_auth_token


def _receive_until(ws, wanted, max_events=60):
    events = []
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        events.append(ev)
        if ev.type == wanted:
            return events
    raise AssertionError(
        f"{wanted} not received; got {[ev.type for ev in events]}")


def _voice_stack(stack: ExitStack):
    mock_store = stack.enter_context(
        patch.object(ws_module, "SecretStore")).return_value
    mock_store.list_providers.return_value = ["google_gemini"]
    stack.enter_context(patch.object(
        ws_module, "_create_provider_instance", return_value=object()))
    return stack


def _harvest_offer_and_candidate():
    """Create a real aiortc offer + one gathered ICE candidate in a
    throwaway loop (the signalling test needs wire material, not a
    live peer)."""
    async def _make():
        from aiortc import RTCPeerConnection

        pc = RTCPeerConnection()
        pc.addTrack(_make_tone_track([(30.0, None)]))
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        local = pc.localDescription
        gathered = _sdp_wire_candidates(local.sdp)
        result = (
            {"type": local.type, "sdp": local.sdp},
            gathered[0] if gathered else None,
        )
        await pc.close()
        return result

    return asyncio.run(_make())


class TestRtcSignallingWebsocket:
    def test_offer_and_ice_without_a_session_fail_closed(self, ws_env):
        with ExitStack() as stack:
            _voice_stack(stack)
            with TestClient(app) as client, client.websocket_connect(
                    "/ws/tasks", headers={"Authorization": f"Bearer {engine_state.auth_token}"}) as ws:
                ws.receive_text()  # HELLO
                ws.send_text(Event(
                    type=EventType.VOICE_RTC_OFFER_REQUEST,
                    payload={"session_id": "none",
                             "sdp": {"type": "offer", "sdp": "v=0\r\n"}},
                ).to_json())
                events = _receive_until(ws, EventType.VOICE_RTC_OFFER_RESPONSE)
                payload = events[-1].payload
                assert payload["ok"] is False
                assert payload["answer"] is None
                assert "no active voice session" in payload["error"]

                ws.send_text(Event(
                    type=EventType.VOICE_RTC_ICE_REQUEST,
                    payload={"session_id": "none", "candidate": None},
                ).to_json())
                events = _receive_until(ws, EventType.VOICE_RTC_ICE_RESPONSE)
                assert events[-1].payload["ok"] is False

    def test_signalling_against_a_live_session(self, ws_env):
        offer, candidate = _harvest_offer_and_candidate()
        assert candidate is not None, "no ICE candidate gathered on loopback"

        transport = WebRtcVoiceTransport()
        real_factory = create_voice_pipeline

        def factory(**kwargs):
            return real_factory(
                vad=EnergyVad(), stt=LengthStt(), tts=FakeTts(),
                transport=transport, **kwargs)

        sid = session_store.create_session("Voice RTC")
        with ExitStack() as stack:
            _voice_stack(stack)
            stack.enter_context(patch(
                "aether_engine.voice.pipeline.create_voice_pipeline",
                side_effect=factory))
            from aether_engine.routing.intent import IntentDecision

            async def _chat_verdict(prompt, provider=None):
                return IntentDecision(
                    route="chat", reason="test verdict", source="model")

            stack.enter_context(patch.object(
                ws_module, "classify_intent", side_effect=_chat_verdict))
            with TestClient(app) as client, client.websocket_connect(
                    "/ws/tasks", headers={"Authorization": f"Bearer {engine_state.auth_token}"}) as ws:
                ws.receive_text()  # HELLO
                ws.send_text(Event(
                    type=EventType.VOICE_SESSION_START_REQUEST,
                    payload={"session_id": sid},
                ).to_json())
                events = _receive_until(
                    ws, EventType.VOICE_SESSION_START_RESPONSE)
                assert events[-1].payload["ok"] is True
                assert events[-1].payload["webrtc_config"] == {
                    "iceServers": []}

                ws.send_text(Event(
                    type=EventType.VOICE_RTC_OFFER_REQUEST,
                    payload={"session_id": sid, "sdp": offer},
                ).to_json())
                # The answer and the engine's own candidate pushes
                # race on the socket (candidates dispatch during
                # negotiation): collect until both have been seen.
                seen = []
                wanted = {
                    EventType.VOICE_RTC_OFFER_RESPONSE,
                    EventType.VOICE_RTC_ICE,
                }
                for _ in range(60):
                    ev = Event.from_json(ws.receive_text())
                    seen.append(ev)
                    if wanted <= {e.type for e in seen}:
                        break
                else:
                    raise AssertionError(
                        f"signalling incomplete; got "
                        f"{[e.type for e in seen]}")
                payload = next(
                    e.payload for e in seen
                    if e.type == EventType.VOICE_RTC_OFFER_RESPONSE)
                assert payload["ok"] is True
                assert payload["error"] is None
                assert payload["answer"]["type"] == "answer"
                assert "m=audio" in payload["answer"]["sdp"]

                # The engine's candidates arrive as VOICE_RTC_ICE
                # pushes in wire shape (None = end-of-candidates).
                wire = next(
                    e.payload["candidate"] for e in seen
                    if e.type == EventType.VOICE_RTC_ICE)
                assert wire is None or (
                    wire["candidate"].startswith("candidate:")
                    and set(wire) == {"candidate", "sdpMid", "sdpMLineIndex"})

                ws.send_text(Event(
                    type=EventType.VOICE_RTC_ICE_REQUEST,
                    payload={"session_id": sid, "candidate": candidate},
                ).to_json())
                events = _receive_until(ws, EventType.VOICE_RTC_ICE_RESPONSE)
                assert events[-1].payload["ok"] is True

                ws.send_text(Event(
                    type=EventType.VOICE_SESSION_STOP_REQUEST,
                    payload={"session_id": sid},
                ).to_json())
                events = _receive_until(
                    ws, EventType.VOICE_SESSION_STOP_RESPONSE)
                assert events[-1].payload == {"ok": True}
