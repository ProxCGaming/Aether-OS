"""Tests for request_id correlation across loopback WebSocket events."""
from pathlib import Path
import tempfile
import unittest

from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.secrets.storage import SecretStore


class TestRequestIdCorrelation(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.token_file = self.tmp_path / "engine.token"
        self.audit_file = self.tmp_path / "audit.log"
        self.secrets_db = self.tmp_path / "test_secrets.db"

        engine_state.token_path = self.token_file
        engine_state.audit_logger.log_path = self.audit_file
        engine_state.initialize()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_event_serialization_preserves_request_id(self):
        ev = Event(
            type=EventType.PING,
            request_id="req-12345",
            payload={"test": True},
        )
        json_str = ev.to_json()
        self.assertIn('"request_id": "req-12345"', json_str)

        deserialized = Event.from_json(json_str)
        self.assertEqual(deserialized.request_id, "req-12345")
        self.assertEqual(deserialized.type, EventType.PING)

    def test_websocket_ping_echoes_request_id(self):
        with TestClient(app) as client:
            token = engine_state.auth_token
            with client.websocket_connect(f"/ws/tasks?token={token}") as ws:
                hello_raw = ws.receive_text()
                self.assertEqual(Event.from_json(hello_raw).type, EventType.HELLO)

                req_id = "ping-req-999"
                ws.send_text(Event(
                    type=EventType.PING,
                    request_id=req_id,
                ).to_json())

                pong_raw = ws.receive_text()
                pong_event = Event.from_json(pong_raw)
                self.assertEqual(pong_event.type, EventType.PONG)
                self.assertEqual(pong_event.request_id, req_id)

    def test_websocket_provider_list_echoes_request_id(self):
        with TestClient(app) as client:
            token = engine_state.auth_token
            with client.websocket_connect(f"/ws/tasks?token={token}") as ws:
                ws.receive_text()  # HELLO

                req_id = "list-req-456"
                ws.send_text(Event(
                    type=EventType.PROVIDER_LIST_REQUEST,
                    request_id=req_id,
                ).to_json())

                resp_raw = ws.receive_text()
                resp_event = Event.from_json(resp_raw)
                self.assertEqual(resp_event.type, EventType.PROVIDER_LIST_RESPONSE)
                self.assertEqual(resp_event.request_id, req_id)
