"""Wave 1 hardening: per-message isolation in the websocket receive loop.

Covers the blowup-ledger cards E1-01 (one malformed message must not
kill the session or its running task), E8-04 (Event.from_json must
reject non-object frames with a typed error the loop already handles),
E1-03 (the six RUX-B-08 background helpers must ANSWER on backend
failure instead of swallowing it), E1-13 (an empty STEER_TASK must be
rejected, never degraded into a spurious "Say hello" task that cancels
the live one), and the launch-cancel audit gap (a CANCEL_TASK that
kills a launch still in its pre-work must leave the same TASK_CANCELLED
audit record the pump writes for a running-task cancel).
"""

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state


def _read_until(ws, wanted, bound=25, timeout=20):
    """Read events until one of ``wanted`` arrives; return (event, seen_types).

    Runs in a daemon thread with a join timeout: a response that never
    arrives (the E1-03 failure shape) fails the test instead of
    hanging the suite on a blocking receive.
    """
    box = {}

    def _run():
        seen = []
        try:
            for _ in range(bound):
                ev = Event.from_json(ws.receive_text())
                seen.append(ev.type)
                if ev.type in wanted:
                    box["event"] = ev
                    break
            box["seen"] = seen
        except Exception as e:  # noqa: BLE001 - re-raised in the test thread
            box["error"] = e
            box["seen"] = seen

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise AssertionError(
            f"Timed out waiting for {wanted}; saw {box.get('seen')}")
    if "error" in box:
        raise box["error"]
    if "event" not in box:
        raise AssertionError(
            f"Never received any of {wanted}; saw {box.get('seen')}")
    return box["event"], box.get("seen", [])


class _WsHarness(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.token_file = self.tmp_path / "engine.token"
        self.audit_file = self.tmp_path / "audit.log"
        self.secrets_db = self.tmp_path / "test_secrets.db"

        engine_state.token_path = self.token_file
        engine_state.audit_logger.log_path = self.audit_file
        self.token = engine_state.initialize()

        self.patcher = patch(
            "aether_engine.memory.session_store.DB_PATH",
            self.tmp_path / "test_memory.db")
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp_dir.cleanup()

    def _connect(self, client):
        ws = client.websocket_connect(
            "/ws/tasks",
            headers={"Authorization": f"Bearer {engine_state.auth_token}"})
        return ws

    def _assert_socket_alive(self, ws):
        ws.send_text(Event(type=EventType.PING).to_json())
        ev, _ = _read_until(ws, {EventType.PONG})
        self.assertEqual(ev.type, EventType.PONG)


class TestMalformedMessagesDoNotKillSession(_WsHarness):
    """E1-01 / E8-04: a coercible-bad message draws an error reply for
    THAT message; the socket (and any task on it) survives."""

    def test_bad_budget_value_rejected_and_socket_survives(self):
        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            before = engine_state.user_config.daily_budget_usd
            ws.send_text(Event(
                type=EventType.DAILY_BUDGET_SET_REQUEST,
                payload={"daily_budget_usd": "not-a-number"},
            ).to_json())
            ev, _ = _read_until(ws, {EventType.DAILY_BUDGET_SET_RESPONSE})
            self.assertEqual(ev.type, EventType.DAILY_BUDGET_SET_RESPONSE)
            self.assertFalse(ev.payload["ok"])
            self.assertIn("error", ev.payload)
            # The bad value was rejected, not applied.
            self.assertEqual(engine_state.user_config.daily_budget_usd, before)
            self._assert_socket_alive(ws)

    def test_bad_grant_id_rejected_and_socket_survives(self):
        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(Event(
                type=EventType.APPROVAL_GRANT_REVOKE_REQUEST,
                payload={"id": "not-a-number"},
            ).to_json())
            ev, _ = _read_until(ws, {EventType.APPROVAL_GRANT_REVOKE_RESPONSE})
            self.assertEqual(ev.type, EventType.APPROVAL_GRANT_REVOKE_RESPONSE)
            self.assertFalse(ev.payload["revoked"])
            self.assertIn("grants", ev.payload)
            self._assert_socket_alive(ws)

    def test_bad_sample_rate_does_not_kill_socket(self):
        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(Event(
                type=EventType.DICTATION_STOP_REQUEST,
                payload={"audio": None, "sample_rate": "not-a-number"},
            ).to_json())
            ev, _ = _read_until(ws, {EventType.DICTATION_STOP_RESPONSE})
            self.assertEqual(ev.type, EventType.DICTATION_STOP_RESPONSE)
            self._assert_socket_alive(ws)

    def test_non_object_config_update_rejected_and_socket_survives(self):
        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(Event(
                type=EventType.CONFIG_UPDATE_REQUEST,
                payload={"config": "disable_fallbacks"},
            ).to_json())
            ev, _ = _read_until(ws, {EventType.CONFIG_UPDATE_RESPONSE})
            self.assertEqual(ev.type, EventType.CONFIG_UPDATE_RESPONSE)
            self.assertFalse(ev.payload["success"])
            self.assertIn("error", ev.payload)
            self._assert_socket_alive(ws)

    def test_non_string_workspace_path_rejected_and_socket_survives(self):
        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(Event(
                type=EventType.WORKSPACE_ADD_REQUEST,
                payload={"path": 12345},
            ).to_json())
            ev, _ = _read_until(ws, {EventType.WORKSPACE_ADD_RESPONSE})
            self.assertEqual(ev.type, EventType.WORKSPACE_ADD_RESPONSE)
            self.assertFalse(ev.payload["success"])
            self._assert_socket_alive(ws)

    def test_scalar_frame_gets_error_reply_and_socket_survives(self):
        # E8-04: a non-object JSON frame must draw the loop's ERROR
        # reply (the parse guard), not tear the connection down.
        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            ws.send_text("123")
            ev, _ = _read_until(ws, {EventType.ERROR})
            self.assertEqual(ev.type, EventType.ERROR)
            self.assertIn("error", ev.payload)
            ws.send_text("[1, 2]")
            ev, _ = _read_until(ws, {EventType.ERROR})
            self.assertEqual(ev.type, EventType.ERROR)
            self._assert_socket_alive(ws)


class TestHelperFailuresAreAnswered(_WsHarness):
    """E1-03: each RUX-B-08 helper must send its *_RESPONSE carrying an
    error when the backend raises — the UI must never wait forever."""

    def test_local_model_list_failure_is_answered(self):
        with TestClient(app) as client, \
                patch.object(engine_state.ollama_manager, "list_models",
                             new=AsyncMock(side_effect=RuntimeError("daemon down"))), \
                self._connect(client) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(Event(type=EventType.LOCAL_MODEL_LIST_REQUEST).to_json())
            ev, _ = _read_until(ws, {EventType.LOCAL_MODEL_LIST_RESPONSE})
            self.assertEqual(ev.type, EventType.LOCAL_MODEL_LIST_RESPONSE)
            self.assertEqual(ev.payload["models"], [])
            self.assertIn("daemon down", ev.payload["error"])

    def test_local_model_delete_failure_is_answered(self):
        with TestClient(app) as client, \
                patch.object(engine_state.ollama_manager, "delete_model",
                             new=AsyncMock(side_effect=RuntimeError("daemon down"))), \
                self._connect(client) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(Event(
                type=EventType.LOCAL_MODEL_DELETE_REQUEST,
                payload={"model": "some-model"},
            ).to_json())
            ev, _ = _read_until(ws, {EventType.LOCAL_MODEL_DELETE_RESPONSE})
            self.assertEqual(ev.type, EventType.LOCAL_MODEL_DELETE_RESPONSE)
            self.assertEqual(ev.payload["model"], "some-model")
            self.assertFalse(ev.payload["deleted"])
            self.assertIn("daemon down", ev.payload["error"])

    def test_model_hub_search_failure_is_answered(self):
        with TestClient(app) as client, \
                patch.object(engine_state.model_hub, "search",
                             new=AsyncMock(side_effect=RuntimeError("catalogue down"))), \
                self._connect(client) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(Event(
                type=EventType.MODEL_HUB_SEARCH_REQUEST,
                payload={"source": "hf", "query": "qwen"},
            ).to_json())
            ev, _ = _read_until(ws, {EventType.MODEL_HUB_SEARCH_RESPONSE})
            self.assertEqual(ev.type, EventType.MODEL_HUB_SEARCH_RESPONSE)
            self.assertEqual(ev.payload["models"], [])
            self.assertIn("catalogue down", ev.payload["error"])

    def test_model_hub_detail_failure_is_answered(self):
        with TestClient(app) as client, \
                patch.object(engine_state.model_hub, "detail",
                             new=AsyncMock(side_effect=RuntimeError("catalogue down"))), \
                self._connect(client) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(Event(
                type=EventType.MODEL_HUB_DETAIL_REQUEST,
                payload={"source": "hf", "repo_id": "some/repo"},
            ).to_json())
            ev, _ = _read_until(ws, {EventType.MODEL_HUB_DETAIL_RESPONSE})
            self.assertEqual(ev.type, EventType.MODEL_HUB_DETAIL_RESPONSE)
            self.assertIsNone(ev.payload["detail"])
            self.assertIn("catalogue down", ev.payload["error"])

    def test_model_hub_delete_failure_is_answered(self):
        with TestClient(app) as client, \
                patch.object(engine_state.hub_downloads, "delete",
                             new=AsyncMock(side_effect=RuntimeError("store down"))), \
                self._connect(client) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(Event(
                type=EventType.MODEL_HUB_DELETE_REQUEST,
                payload={"source": "ollama", "id": "some-model"},
            ).to_json())
            ev, _ = _read_until(ws, {EventType.MODEL_HUB_DELETE_RESPONSE})
            self.assertEqual(ev.type, EventType.MODEL_HUB_DELETE_RESPONSE)
            self.assertFalse(ev.payload["success"])
            self.assertFalse(ev.payload["deleted"])
            self.assertIn("store down", ev.payload["error"])

    def test_media_search_failure_is_answered(self):
        with TestClient(app) as client, \
                patch("aether_engine.tools.media_recall.search_media_entries",
                      new=AsyncMock(side_effect=RuntimeError("index down"))), \
                self._connect(client) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(Event(
                type=EventType.MEDIA_SEARCH_REQUEST,
                payload={"query": "cats"},
            ).to_json())
            ev, _ = _read_until(ws, {EventType.MEDIA_SEARCH_RESPONSE})
            self.assertEqual(ev.type, EventType.MEDIA_SEARCH_RESPONSE)
            self.assertEqual(ev.payload["query"], "cats")
            self.assertEqual(ev.payload["media"], [])
            self.assertTrue(ev.payload["degraded"])
            self.assertIn("index down", ev.payload["error"])


class TestEmptySteerRejected(_WsHarness):
    """E1-13: an empty STEER_TASK is rejected with an ERROR reply; it
    must never become a "Say hello" START_TASK that cancels the live
    task."""

    def _slow_provider(self):
        from aether_engine.providers.base import StreamChunk
        from aether_engine.providers.litellm_provider import LiteLLMProvider

        async def slow_call_stream(messages, tools=None, node_name=None):
            await asyncio.sleep(10)
            yield StreamChunk(text="Done")  # pragma: no cover - cancelled first

        provider = LiteLLMProvider(api_key="fake-key", model="test-model",
                                   provider_name="test")
        provider.call_stream = slow_call_stream
        return provider

    def test_empty_steer_without_task_starts_nothing(self):
        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(Event(
                type=EventType.STEER_TASK,
                payload={"prompt": "   ", "session_id": "steer-empty-1"},
            ).to_json())
            ev, seen = _read_until(ws, {
                EventType.ERROR, EventType.TASK_CREATED, EventType.TASK_FAILED})
            self.assertEqual(ev.type, EventType.ERROR)
            self.assertNotIn(EventType.TASK_CREATED, seen)
            self._assert_socket_alive(ws)

    def test_empty_steer_leaves_live_task_untouched(self):
        with TestClient(app) as client:
            valid_token = engine_state.auth_token
            with patch("aether_engine.routes.websocket.SecretStore") as mock_store_cls, \
                    patch("aether_engine.routes.websocket._create_provider_instance") as mock_factory:
                mock_store = mock_store_cls.return_value
                mock_store.load_provider.return_value = "fake-key"
                mock_factory.return_value = self._slow_provider()

                with client.websocket_connect(
                        "/ws/tasks",
                        headers={"Authorization": f"Bearer {valid_token}"}) as ws:
                    ws.receive_text()  # HELLO
                    ws.send_text(Event(
                        type=EventType.START_TASK,
                        payload={"prompt": "Slow query",
                                 "session_id": "steer-live-1"},
                    ).to_json())
                    ev, _ = _read_until(ws, {EventType.TASK_CREATED})
                    self.assertEqual(ev.type, EventType.TASK_CREATED)

                    # The empty steer must be answered with ERROR and
                    # must NOT spawn a replacement task.
                    ws.send_text(Event(
                        type=EventType.STEER_TASK,
                        payload={"prompt": "",
                                 "session_id": "steer-live-1"},
                    ).to_json())
                    ev, seen = _read_until(
                        ws, {EventType.ERROR, EventType.TASK_CREATED})
                    self.assertEqual(ev.type, EventType.ERROR)
                    self.assertNotIn(EventType.TASK_CREATED, seen)

                    # The live task survived: it is still there to be
                    # cancelled, and the cancel is honoured.
                    ws.send_text(Event(type=EventType.CANCEL_TASK).to_json())
                    ev, _ = _read_until(ws, {EventType.TASK_CANCELLED})
                    self.assertEqual(ev.type, EventType.TASK_CANCELLED)


class TestLaunchCancelAudit(_WsHarness):
    """A CANCEL_TASK that lands while the launch is still in its
    pre-work (no pump exists yet) must write the same TASK_CANCELLED
    audit record the pump writes for a running-task cancel."""

    def test_launch_cancel_writes_task_cancelled_audit_record(self):
        async def slow_classify(*args, **kwargs):
            await asyncio.sleep(30)

        with TestClient(app) as client:
            valid_token = engine_state.auth_token
            with patch("aether_engine.routes.websocket.SecretStore") as mock_store_cls, \
                    patch("aether_engine.routes.websocket._create_provider_instance") as mock_factory, \
                    patch("aether_engine.routes.websocket.classify_intent",
                          new=slow_classify):
                mock_store = mock_store_cls.return_value
                mock_store.load_provider.return_value = "fake-key"
                from aether_engine.providers.litellm_provider import LiteLLMProvider
                mock_factory.return_value = LiteLLMProvider(
                    api_key="fake-key", model="test-model", provider_name="test")

                with client.websocket_connect(
                        "/ws/tasks",
                        headers={"Authorization": f"Bearer {valid_token}"}) as ws:
                    ws.receive_text()  # HELLO
                    ws.send_text(Event(
                        type=EventType.START_TASK,
                        payload={"prompt": "Classify me slowly",
                                 "session_id": "launch-cancel-1"},
                    ).to_json())
                    # The launch is parked in classification pre-work;
                    # no task exists yet. Cancel it now.
                    ws.send_text(Event(type=EventType.CANCEL_TASK).to_json())
                    ev, _ = _read_until(ws, {EventType.TASK_CANCELLED})
                    self.assertEqual(ev.type, EventType.TASK_CANCELLED)

                    audit_content = self.audit_file.read_text(encoding="utf-8")
                    self.assertEqual(
                        audit_content.count('"action": "TASK_CANCELLED"'), 1)
                    self.assertIn('"task_id": "unknown"', audit_content)


if __name__ == "__main__":
    unittest.main()
