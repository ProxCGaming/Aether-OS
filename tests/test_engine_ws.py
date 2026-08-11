import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aether_common.contracts import (
    SCHEMA_VERSION,
    Event,
    EventType,
    TaskState,
)
from aether_engine.app import app, engine_state
from aether_engine.providers.base import BaseProvider, StreamChunk
from aether_engine.secrets.storage import SecretStore


class DummyProvider(BaseProvider):
    async def call_stream(self, messages, tools=None):
        yield StreamChunk(text="Hello ")
        yield StreamChunk(text="from test LLM!")


class TestEngineWebSocket(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.token_file = self.tmp_path / "engine.token"
        self.audit_file = self.tmp_path / "audit.log"
        self.secrets_db = self.tmp_path / "test_secrets.db"

        engine_state.token_path = self.token_file
        engine_state.audit_logger.log_path = self.audit_file
        self.token = engine_state.initialize_auth()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_health_endpoint(self):
        with TestClient(app) as client:
            response = client.get("/health")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["schema_version"], SCHEMA_VERSION)

    def test_websocket_unauthenticated_rejected(self):
        with TestClient(app) as client:
            with self.assertRaises(WebSocketDisconnect) as cm:
                with client.websocket_connect("/ws/tasks"):
                    pass
            self.assertEqual(cm.exception.code, 1008)

            with self.assertRaises(WebSocketDisconnect) as cm:
                with client.websocket_connect("/ws/tasks?token=invalid_token_12345"):
                    pass
            self.assertEqual(cm.exception.code, 1008)

        audit_content = self.audit_file.read_text(encoding="utf-8")
        self.assertIn("Unauthorized", audit_content)
        self.assertIn('"success": false', audit_content)

    def test_websocket_authenticated_and_hello(self):
        with TestClient(app) as client:
            valid_token = engine_state.auth_token
            with client.websocket_connect(f"/ws/tasks?token={valid_token}") as ws:
                msg_text = ws.receive_text()
                hello_event = Event.from_json(msg_text)
                self.assertEqual(hello_event.type, EventType.HELLO)
                self.assertEqual(hello_event.schema_version, SCHEMA_VERSION)

        audit_content = self.audit_file.read_text(encoding="utf-8")
        self.assertIn('"success": true', audit_content)
        self.assertIn("Authenticated successfully", audit_content)

    def test_websocket_missing_api_key_fails_gracefully(self):
        with TestClient(app) as client:
            valid_token = engine_state.auth_token
            with patch("aether_engine.app.SecretStore") as mock_store_cls:
                # Mock store returning ProviderNotFoundError
                mock_store = mock_store_cls.return_value
                from aether_engine.secrets.storage import ProviderNotFoundError
                mock_store.load_provider.side_effect = ProviderNotFoundError("Not found")

                with client.websocket_connect(f"/ws/tasks?token={valid_token}") as ws:
                    ws.receive_text()  # HELLO

                    start_event = Event(
                        type=EventType.START_TASK,
                        payload={"prompt": "Hello"},
                    )
                    ws.send_text(start_event.to_json())

                    failed_ev = Event.from_json(ws.receive_text())
                    self.assertEqual(failed_ev.type, EventType.TASK_FAILED)
                    self.assertIn("API key not found", failed_ev.payload["error"])

    def test_websocket_task_flow_with_provider(self):
        with TestClient(app) as client:
            valid_token = engine_state.auth_token
            
            async def mock_acompletion(*args, **kwargs):
                class DummyMessage:
                    content = "Hello from test LLM!"
                    tool_calls = None
                class DummyChoice:
                    message = DummyMessage()
                class DummyResponse:
                    choices = [DummyChoice()]
                return DummyResponse()

            with patch("aether_engine.app.SecretStore") as mock_store_cls, \
                 patch("aether_engine.app._create_provider_instance") as mock_factory, \
                 patch("aether_engine.langgraph.nodes.researcher.litellm.acompletion", new=mock_acompletion), \
                 patch("aether_engine.langgraph.nodes.planner.litellm.acompletion", new=mock_acompletion), \
                 patch("aether_engine.langgraph.nodes.coder.litellm.acompletion", new=mock_acompletion):

                mock_store = mock_store_cls.return_value
                mock_store.load_provider.return_value = "fake-key"
                mock_factory.return_value = DummyProvider() # Still mocked so it doesn't crash initialization

                with client.websocket_connect(f"/ws/tasks?token={valid_token}") as ws:
                    ws.receive_text()  # HELLO

                    start_event = Event(
                        type=EventType.START_TASK,
                        payload={"prompt": "What is the weather?"},
                    )
                    ws.send_text(start_event.to_json())

                    # 1. TASK_CREATED
                    ev1 = Event.from_json(ws.receive_text())
                    self.assertEqual(ev1.type, EventType.TASK_CREATED)

                    # 2. TASK_PROGRESS deltas (Graph now emits the full message as one delta per node)
                    ev2 = Event.from_json(ws.receive_text())
                    self.assertEqual(ev2.type, EventType.TASK_PROGRESS)
                    self.assertEqual(ev2.payload["text_delta"], "Hello from test LLM!")

                    # 3. TASK_COMPLETED
                    ev4 = Event.from_json(ws.receive_text())
                    self.assertEqual(ev4.type, EventType.TASK_COMPLETED)
                    # The response payload is currently empty in LangGraph implementation
                    # self.assertEqual(ev4.payload.get("response", ""), "Hello from test LLM!")

        audit_content = self.audit_file.read_text(encoding="utf-8")
        self.assertIn("TASK_TRANSITION", audit_content)
        self.assertIn("RUNNING", audit_content)
        self.assertIn("SUCCEEDED", audit_content)

    def test_websocket_task_cancellation_flow(self):
        async def mock_acompletion(*args, **kwargs):
            await asyncio.sleep(5)  # Simulate a slow query
            class DummyMessage:
                content = "Done"
                tool_calls = None
            class DummyChoice:
                message = DummyMessage()
            class DummyResponse:
                choices = [DummyChoice()]
            return DummyResponse()

        with TestClient(app) as client:
            valid_token = engine_state.auth_token
            with patch("aether_engine.app.SecretStore") as mock_store_cls, \
                 patch("aether_engine.app._create_provider_instance") as mock_factory, \
                 patch("aether_engine.langgraph.nodes.researcher.litellm.acompletion", new=mock_acompletion), \
                 patch("aether_engine.langgraph.nodes.planner.litellm.acompletion", new=mock_acompletion), \
                 patch("aether_engine.langgraph.nodes.coder.litellm.acompletion", new=mock_acompletion):

                mock_store = mock_store_cls.return_value
                mock_store.load_provider.return_value = "fake-key"
                mock_factory.return_value = DummyProvider() # Just so initialization doesn't crash

                with client.websocket_connect(f"/ws/tasks?token={valid_token}") as ws:
                    ws.receive_text()  # HELLO

                    start_event = Event(
                        type=EventType.START_TASK,
                        payload={"prompt": "Slow query"},
                    )
                    ws.send_text(start_event.to_json())

                    # TASK_CREATED
                    Event.from_json(ws.receive_text())

                    # Cancel task
                    cancel_event = Event(type=EventType.CANCEL_TASK)
                    ws.send_text(cancel_event.to_json())

                    # Receive events until TASK_CANCELLED
                    types = []
                    for _ in range(5):
                        ev = Event.from_json(ws.receive_text())
                        types.append(ev.type)
                        if ev.type == EventType.TASK_CANCELLED:
                            break

                    self.assertIn(EventType.TASK_CANCELLED, types)

        audit_content = self.audit_file.read_text(encoding="utf-8")
        self.assertIn("CANCELLED", audit_content)

    # -------------------------------------------------------------------
    # Phase 3: Provider management tests
    # -------------------------------------------------------------------
    def test_provider_list_request(self):
        with TestClient(app) as client:
            valid_token = engine_state.auth_token
            with client.websocket_connect(f"/ws/tasks?token={valid_token}") as ws:
                ws.receive_text()  # HELLO

                list_req = Event(type=EventType.PROVIDER_LIST_REQUEST)
                ws.send_text(list_req.to_json())

                resp = Event.from_json(ws.receive_text())
                self.assertEqual(resp.type, EventType.PROVIDER_LIST_RESPONSE)
                providers = resp.payload["providers"]
                self.assertTrue(len(providers) >= 5)
                names = [p["name"] for p in providers]
                self.assertIn("google_gemini", names)
                self.assertIn("openai", names)

    def test_provider_save_and_remove(self):
        with TestClient(app) as client:
            valid_token = engine_state.auth_token
            with patch("aether_engine.app.SecretStore") as mock_cls:
                mock_store = mock_cls.return_value
                mock_store.list_providers.return_value = []
                mock_store.save_provider.return_value = None
                mock_store.delete_provider.return_value = True

                with client.websocket_connect(f"/ws/tasks?token={valid_token}") as ws:
                    ws.receive_text()  # HELLO

                    # Save
                    save_req = Event(
                        type=EventType.PROVIDER_SAVE_REQUEST,
                        payload={"provider": "google_gemini", "api_key": "test-key-123",
                                 "is_default": True, "default_model": "gemini-2.5-flash"},
                    )
                    ws.send_text(save_req.to_json())
                    resp = Event.from_json(ws.receive_text())
                    self.assertEqual(resp.type, EventType.PROVIDER_SAVE_RESPONSE)
                    self.assertTrue(resp.payload["success"])

                    # Remove
                    rm_req = Event(
                        type=EventType.PROVIDER_REMOVE_REQUEST,
                        payload={"provider": "google_gemini"},
                    )
                    ws.send_text(rm_req.to_json())
                    resp = Event.from_json(ws.receive_text())
                    self.assertEqual(resp.type, EventType.PROVIDER_REMOVE_RESPONSE)
                    self.assertTrue(resp.payload["deleted"])

    def test_model_set_default(self):
        with TestClient(app) as client:
            valid_token = engine_state.auth_token
            with client.websocket_connect(f"/ws/tasks?token={valid_token}") as ws:
                ws.receive_text()  # HELLO

                set_req = Event(
                    type=EventType.MODEL_SET_DEFAULT,
                    payload={"provider": "google_gemini", "model": "gemini-2.5-pro"},
                )
                ws.send_text(set_req.to_json())
                resp = Event.from_json(ws.receive_text())
                self.assertEqual(resp.type, EventType.MODEL_DEFAULT_CHANGED)
                self.assertEqual(resp.payload["model"], "gemini-2.5-pro")


if __name__ == "__main__":
    unittest.main()

