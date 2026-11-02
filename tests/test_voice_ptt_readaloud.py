"""Epic 8, E8-5.3: Push-to-talk dictation + TTS read-aloud tests.

Tests the WebSocket event contracts, the fail-closed honesty when STT
(faster-whisper) or TTS (kokoro) dependencies are missing, and the
happy path with injected fakes.
"""
import base64
from contextlib import ExitStack
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.memory import session_store
from aether_engine.routes import websocket as ws_module


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    engine_state.initialize_auth()
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[])
    )
    return {"tmp_path": tmp_path}


def _receive_until(ws, wanted, max_events=40):
    events = []
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        events.append(ev)
        if ev.type == wanted:
            return events
    raise AssertionError(
        f"{wanted} not received; got {[ev.type for ev in events]}"
    )


class FakeStt:
    def __init__(self, script=(), reason=None, transcribe_error=None):
        self.script = list(script)
        self.reason = reason
        self.transcribe_error = transcribe_error
        self.calls = []
        self.open_calls = 0
        self.close_calls = 0

    def unavailable_reason(self):
        return self.reason

    async def open(self):
        self.open_calls += 1

    async def close(self):
        self.close_calls += 1

    async def transcribe(self, audio: bytes, *, sample_rate: int) -> str:
        self.calls.append((len(audio), sample_rate))
        if self.transcribe_error is not None:
            raise self.transcribe_error
        if self.script:
            return self.script.pop(0)
        return ""


class FakeTts:
    def __init__(self, reason=None):
        self.reason = reason
        self.requests = []
        self.open_calls = 0
        self.close_calls = 0

    def unavailable_reason(self):
        return self.reason

    async def open(self):
        self.open_calls += 1

    async def close(self):
        self.close_calls += 1

    async def synthesize(self, text: str, *, voice: str):
        self.requests.append((text, voice))
        yield b"CHUNK1:" + text.encode()
        yield b"CHUNK2:" + text.encode()


class TestContractsE853:
    def test_contract_event_types_exist(self):
        """Assert all E8-5.3 event types exist on EventType."""
        assert EventType.DICTATION_START_REQUEST == "DICTATION_START_REQUEST"
        assert EventType.DICTATION_START_RESPONSE == "DICTATION_START_RESPONSE"
        assert EventType.DICTATION_CHUNK_REQUEST == "DICTATION_CHUNK_REQUEST"
        assert EventType.DICTATION_CHUNK_RESPONSE == "DICTATION_CHUNK_RESPONSE"
        assert EventType.DICTATION_STOP_REQUEST == "DICTATION_STOP_REQUEST"
        assert EventType.DICTATION_STOP_RESPONSE == "DICTATION_STOP_RESPONSE"

        assert EventType.READ_ALOUD_START_REQUEST == "READ_ALOUD_START_REQUEST"
        assert EventType.READ_ALOUD_START_RESPONSE == "READ_ALOUD_START_RESPONSE"
        assert EventType.READ_ALOUD_CHUNK == "READ_ALOUD_CHUNK"
        assert EventType.READ_ALOUD_STOP_REQUEST == "READ_ALOUD_STOP_REQUEST"
        assert EventType.READ_ALOUD_STOP_RESPONSE == "READ_ALOUD_STOP_RESPONSE"


class TestDictationWebsocket:
    def test_dictation_start_fails_closed_when_stt_unavailable(self, ws_env, monkeypatch):
        """When faster-whisper is not installed, dictation start returns ok=False
        with honest reason. Absence is simulated with an unavailable
        engine: the real package IS installed since the voice
        packaging slice, so the environment no longer provides the
        absence for free."""
        monkeypatch.setattr(
            ws_module, "create_stt_engine",
            lambda: FakeStt(reason="faster-whisper is not installed (local STT)"))
        with ExitStack(), TestClient(app) as client, client.websocket_connect(
            "/ws/tasks", headers={"Authorization": f"Bearer {engine_state.auth_token}"}
        ) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(
                Event(
                    type=EventType.DICTATION_START_REQUEST,
                    payload={"session_id": "s1"},
                ).to_json()
            )
            events = _receive_until(ws, EventType.DICTATION_START_RESPONSE)
        payload = events[-1].payload
        assert payload["ok"] is False
        assert "faster-whisper is not installed" in payload["error"]

    def test_dictation_flow_with_fake_stt(self, ws_env, monkeypatch):
        """When STT is available, start succeeds, chunks transcribe, and stop finalizes."""
        fake_stt = FakeStt(["hello", "hello world", "final transcript"])
        monkeypatch.setattr(ws_module, "create_stt_engine", lambda: fake_stt)

        with ExitStack(), TestClient(app) as client, client.websocket_connect(
            "/ws/tasks", headers={"Authorization": f"Bearer {engine_state.auth_token}"}
        ) as ws:
            ws.receive_text()  # HELLO

            # 1. Start dictation
            ws.send_text(
                Event(
                    type=EventType.DICTATION_START_REQUEST,
                    payload={"session_id": "s1"},
                ).to_json()
            )
            events = _receive_until(ws, EventType.DICTATION_START_RESPONSE)
            assert events[-1].payload["ok"] is True

            # 2. Send chunk 1
            b64_audio_1 = base64.b64encode(b"audio_bytes_1").decode("ascii")
            ws.send_text(
                Event(
                    type=EventType.DICTATION_CHUNK_REQUEST,
                    payload={
                        "session_id": "s1",
                        "audio": b64_audio_1,
                        "sample_rate": 16000,
                        "is_final": False,
                    },
                ).to_json()
            )
            events = _receive_until(ws, EventType.DICTATION_CHUNK_RESPONSE)
            assert events[-1].payload["ok"] is True
            assert events[-1].payload["text"] == "hello"

            # 3. Stop dictation
            b64_audio_final = base64.b64encode(b"audio_bytes_final").decode("ascii")
            ws.send_text(
                Event(
                    type=EventType.DICTATION_STOP_REQUEST,
                    payload={
                        "session_id": "s1",
                        "audio": b64_audio_final,
                        "sample_rate": 16000,
                    },
                ).to_json()
            )
            events = _receive_until(ws, EventType.DICTATION_STOP_RESPONSE)
            assert events[-1].payload["ok"] is True
            assert events[-1].payload["text"] == "hello world"


class TestReadAloudWebsocket:
    def test_read_aloud_fails_closed_when_tts_unavailable(self, ws_env, monkeypatch):
        """When kokoro is not installed, read-aloud start returns ok=False
        with honest reason. Absence is simulated with an unavailable
        engine: the real package IS installed since the voice
        packaging slice, so the environment no longer provides the
        absence for free."""
        monkeypatch.setattr(
            ws_module, "create_tts_engine",
            lambda: FakeTts(reason="kokoro is not installed (local TTS)"))
        with ExitStack(), TestClient(app) as client, client.websocket_connect(
            "/ws/tasks", headers={"Authorization": f"Bearer {engine_state.auth_token}"}
        ) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(
                Event(
                    type=EventType.READ_ALOUD_START_REQUEST,
                    payload={"message_id": "m1", "text": "Hello world."},
                ).to_json()
            )
            events = _receive_until(ws, EventType.READ_ALOUD_START_RESPONSE)
        payload = events[-1].payload
        assert payload["ok"] is False
        assert "kokoro is not installed" in payload["error"]

    def test_read_aloud_streams_chunks_with_fake_tts(self, ws_env, monkeypatch):
        """When TTS is available, start responds ok=True, streams READ_ALOUD_CHUNKs,
        and finishes with is_final=True."""
        fake_tts = FakeTts()
        monkeypatch.setattr(ws_module, "create_tts_engine", lambda: fake_tts)

        with ExitStack(), TestClient(app) as client, client.websocket_connect(
            "/ws/tasks", headers={"Authorization": f"Bearer {engine_state.auth_token}"}
        ) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(
                Event(
                    type=EventType.READ_ALOUD_START_REQUEST,
                    payload={"message_id": "m1", "text": "Hello assistant."},
                ).to_json()
            )
            events = _receive_until(ws, EventType.READ_ALOUD_START_RESPONSE)
            assert events[-1].payload["ok"] is True
            assert events[-1].payload["message_id"] == "m1"

            # Wait for final chunk
            ev = None
            for _ in range(20):
                ev = Event.from_json(ws.receive_text())
                if ev.type == EventType.READ_ALOUD_CHUNK and ev.payload.get("is_final"):
                    break
            assert ev is not None
            assert ev.type == EventType.READ_ALOUD_CHUNK
            assert ev.payload["is_final"] is True
            assert ev.payload["message_id"] == "m1"

    def test_read_aloud_stop(self, ws_env, monkeypatch):
        """Stop request cancels playback and responds ok=True."""
        fake_tts = FakeTts()
        monkeypatch.setattr(ws_module, "create_tts_engine", lambda: fake_tts)

        with ExitStack(), TestClient(app) as client, client.websocket_connect(
            "/ws/tasks", headers={"Authorization": f"Bearer {engine_state.auth_token}"}
        ) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(
                Event(
                    type=EventType.READ_ALOUD_STOP_REQUEST,
                    payload={"message_id": "m1"},
                ).to_json()
            )
            events = _receive_until(ws, EventType.READ_ALOUD_STOP_RESPONSE)
            assert events[-1].payload["ok"] is True


def _wait_for_final_read_aloud_chunk(ws, max_events=20):
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        if ev.type == EventType.READ_ALOUD_CHUNK and ev.payload.get("is_final"):
            return ev
    raise AssertionError("final READ_ALOUD_CHUNK not received")


class TestSpeechEngineCachingS4M7:
    """S4-M7: speech engines are per-connection, not per chunk/request,
    and a dictation-stop transcription failure is reported, not
    swallowed as empty success."""

    def test_dictation_reuses_one_stt_engine_and_opens_once(self, ws_env, monkeypatch):
        fake_stt = FakeStt(["hello", "hello world", "final transcript"])
        factory_calls = {"count": 0}

        def counting_factory():
            factory_calls["count"] += 1
            return fake_stt

        monkeypatch.setattr(ws_module, "create_stt_engine", counting_factory)

        with ExitStack(), TestClient(app) as client, client.websocket_connect(
            "/ws/tasks", headers={"Authorization": f"Bearer {engine_state.auth_token}"}
        ) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(
                Event(
                    type=EventType.DICTATION_START_REQUEST,
                    payload={"session_id": "s1"},
                ).to_json()
            )
            events = _receive_until(ws, EventType.DICTATION_START_RESPONSE)
            assert events[-1].payload["ok"] is True

            for audio, expected in (
                (b"audio_bytes_1", "hello"),
                (b"audio_bytes_2", "hello world"),
            ):
                ws.send_text(
                    Event(
                        type=EventType.DICTATION_CHUNK_REQUEST,
                        payload={
                            "session_id": "s1",
                            "audio": base64.b64encode(audio).decode("ascii"),
                            "sample_rate": 16000,
                            "is_final": False,
                        },
                    ).to_json()
                )
                events = _receive_until(ws, EventType.DICTATION_CHUNK_RESPONSE)
                assert events[-1].payload["ok"] is True
                assert events[-1].payload["text"] == expected

            ws.send_text(
                Event(
                    type=EventType.DICTATION_STOP_REQUEST,
                    payload={
                        "session_id": "s1",
                        "audio": base64.b64encode(b"audio_bytes_final").decode("ascii"),
                        "sample_rate": 16000,
                    },
                ).to_json()
            )
            events = _receive_until(ws, EventType.DICTATION_STOP_RESPONSE)
            assert events[-1].payload["ok"] is True
            assert events[-1].payload["text"] == "final transcript"

            assert factory_calls["count"] == 1
            assert fake_stt.open_calls == 1
        # Engines live for the connection and close at teardown.
        assert fake_stt.close_calls == 1

    def test_dictation_stop_reports_transcribe_error(self, ws_env, monkeypatch):
        fake_stt = FakeStt(transcribe_error=RuntimeError("whisper exploded"))
        monkeypatch.setattr(ws_module, "create_stt_engine", lambda: fake_stt)

        with ExitStack(), TestClient(app) as client, client.websocket_connect(
            "/ws/tasks", headers={"Authorization": f"Bearer {engine_state.auth_token}"}
        ) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(
                Event(
                    type=EventType.DICTATION_START_REQUEST,
                    payload={"session_id": "s1"},
                ).to_json()
            )
            events = _receive_until(ws, EventType.DICTATION_START_RESPONSE)
            assert events[-1].payload["ok"] is True

            ws.send_text(
                Event(
                    type=EventType.DICTATION_STOP_REQUEST,
                    payload={
                        "session_id": "s1",
                        "audio": base64.b64encode(b"audio_bytes_final").decode("ascii"),
                        "sample_rate": 16000,
                    },
                ).to_json()
            )
            events = _receive_until(ws, EventType.DICTATION_STOP_RESPONSE)
        payload = events[-1].payload
        assert payload["ok"] is False
        assert payload["text"] == ""
        assert payload["error"]
        assert "whisper exploded" in payload["error"]

    def test_read_aloud_reuses_one_tts_engine_across_starts(self, ws_env, monkeypatch):
        fake_tts = FakeTts()
        factory_calls = {"count": 0}

        def counting_factory():
            factory_calls["count"] += 1
            return fake_tts

        monkeypatch.setattr(ws_module, "create_tts_engine", counting_factory)

        with ExitStack(), TestClient(app) as client, client.websocket_connect(
            "/ws/tasks", headers={"Authorization": f"Bearer {engine_state.auth_token}"}
        ) as ws:
            ws.receive_text()  # HELLO
            for message_id, text in (
                ("m1", "Hello assistant."),
                ("m2", "Second message."),
            ):
                ws.send_text(
                    Event(
                        type=EventType.READ_ALOUD_START_REQUEST,
                        payload={"message_id": message_id, "text": text},
                    ).to_json()
                )
                events = _receive_until(ws, EventType.READ_ALOUD_START_RESPONSE)
                assert events[-1].payload["ok"] is True
                assert events[-1].payload["message_id"] == message_id
                final = _wait_for_final_read_aloud_chunk(ws)
                assert final.payload["message_id"] == message_id

            assert factory_calls["count"] == 1
            assert fake_tts.open_calls == 1
        assert fake_tts.close_calls == 1
