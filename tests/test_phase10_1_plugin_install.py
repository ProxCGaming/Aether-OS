"""Phase 10.1 A1: plugin install approval flow over /ws/tasks.

Regression tests for the undefined ``pending_plugin_installs`` store
(``aether_engine/routes/websocket.py``): the name was used at three sites
(PLUGIN_INSTALL_REQUEST assignment, PLUGIN_APPROVAL_GRANTED pop,
PLUGIN_APPROVAL_REJECTED pop) with zero definitions repo-wide, so every
plugin install failed with NameError and an approval reply dropped the
WebSocket session. These tests drive the real endpoint end to end and
would have failed before the fix.
"""
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.plugins.installer import PluginInstaller


class TestPhase101PluginInstall(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.token_file = self.tmp_path / "engine.token"
        self.audit_file = self.tmp_path / "audit.log"

        engine_state.token_path = self.token_file
        engine_state.audit_logger.log_path = self.audit_file
        self.token = engine_state.initialize_auth()

        self.patcher = patch("aether_engine.memory.session_store.DB_PATH", self.tmp_path / "test_memory.db")
        self.patcher.start()
        # install() writes skills under Path.home() / ".aether" — keep that in tmp
        self.home_patcher = patch.dict("os.environ", {"HOME": str(self.tmp_path)})
        self.home_patcher.start()

        self._orig_installer = engine_state.plugin_installer
        engine_state.plugin_installer = PluginInstaller(self.tmp_path / "plugins")

        # Minimal valid plugin source: manifest only, no skills/tools/mcp.
        self.source_dir = self.tmp_path / "source_plugin"
        self.source_dir.mkdir()
        (self.source_dir / "manifest.json").write_text(json.dumps({
            "name": "Phase101TestPlugin", "version": "1.0",
        }))
        (self.source_dir / "README.md").write_text("test plugin")

    def tearDown(self):
        engine_state.plugin_installer = self._orig_installer
        try:
            from aether_engine.routes import websocket as ws_routes

            ws_routes.pending_plugin_installs.clear()
        except (ImportError, AttributeError):
            pass  # pre-fix: the store does not exist yet
        self.home_patcher.stop()
        self.patcher.stop()
        self.tmp_dir.cleanup()

    def _connect(self, client):
        # NB: TestClient startup runs the app lifespan, which regenerates
        # engine_state.auth_token — read it fresh here, not the setUp token.
        # Return the session un-entered: the caller's `with` enters it once.
        return client.websocket_connect(f"/ws/tasks?token={engine_state.auth_token}")

    def _hello(self, ws):
        hello = self._recv_event(ws)
        self.assertEqual(hello.type, EventType.HELLO)

    @staticmethod
    def _recv_event(ws, timeout=10.0):
        """Receive one Event, failing (never hanging) if the server goes quiet.

        When the WS handler dies mid-session (e.g. the pre-fix NameError on
        the approval path), Starlette's TestClient never delivers a close
        message, so a bare receive_text() would block forever. A timeout
        turns that into a clean test failure instead.
        """
        q: queue.Queue = queue.Queue()

        def _do():
            try:
                q.put(("ok", ws.receive_text()))
            except Exception as e:  # noqa: BLE001 — relayed to the test thread for assertion
                q.put(("err", e))

        threading.Thread(target=_do, daemon=True).start()
        try:
            kind, value = q.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(
                f"No WebSocket message within {timeout}s — the session was "
                "probably dropped (e.g. unhandled NameError in the handler)."
            )
        if kind == "err":
            raise value
        return Event.from_json(value)

    def test_pending_plugin_installs_defined_at_module_scope(self):
        # Fails pre-fix with ImportError: the name did not exist at all.
        from aether_engine.routes.websocket import pending_plugin_installs

        self.assertIsInstance(pending_plugin_installs, dict)

    def test_install_request_emits_approval_request(self):
        # Pre-fix: NameError inside prepare/assign was caught by the
        # surrounding except and returned PLUGIN_INSTALL_RESPONSE(success=False),
        # so the approval card was never sent.
        with TestClient(app) as client, self._connect(client) as ws:
            self._hello(ws)
            ws.send_text(Event(
                type=EventType.PLUGIN_INSTALL_REQUEST,
                request_id="req-approval-1",
                payload={"source": str(self.source_dir)},
            ).to_json())
            resp = self._recv_event(ws)
            self.assertEqual(resp.type, EventType.PLUGIN_APPROVAL_REQUEST)
            self.assertEqual(resp.request_id, "req-approval-1")
            self.assertEqual(resp.payload["plugin_name"], "Phase101TestPlugin")

    def test_approval_grant_installs_plugin(self):
        with TestClient(app) as client, self._connect(client) as ws:
            self._hello(ws)
            ws.send_text(Event(
                type=EventType.PLUGIN_INSTALL_REQUEST,
                request_id="req-grant-1",
                payload={"source": str(self.source_dir)},
            ).to_json())
            approval = self._recv_event(ws)
            self.assertEqual(approval.type, EventType.PLUGIN_APPROVAL_REQUEST)

            ws.send_text(Event(
                type=EventType.PLUGIN_APPROVAL_GRANTED,
                request_id="req-grant-1",
            ).to_json())
            resp = self._recv_event(ws)
            self.assertEqual(resp.type, EventType.PLUGIN_INSTALL_RESPONSE)
            self.assertTrue(resp.payload["success"], resp.payload)

            self.assertIn("Phase101TestPlugin", engine_state.plugin_installer.get_all())
            self.assertTrue((self.tmp_path / "plugins" / "Phase101TestPlugin").exists())
            from aether_engine.routes import websocket as ws_routes

            self.assertNotIn("req-grant-1", ws_routes.pending_plugin_installs)

    def test_approval_reject_cancels_cleanly_and_session_survives(self):
        # Pre-fix: the reject branch raised NameError outside any try, hit
        # the outer WS except, and dropped the session.
        with TestClient(app) as client:  # noqa: SIM117 — nested for readability; combined form lands with the tip cleanup
            with self._connect(client) as ws:
                self._hello(ws)
                ws.send_text(Event(
                    type=EventType.PLUGIN_INSTALL_REQUEST,
                    request_id="req-reject-1",
                    payload={"source": str(self.source_dir)},
                ).to_json())
                approval = self._recv_event(ws)
                self.assertEqual(approval.type, EventType.PLUGIN_APPROVAL_REQUEST)

                ws.send_text(Event(
                    type=EventType.PLUGIN_APPROVAL_REJECTED,
                    request_id="req-reject-1",
                ).to_json())
                resp = self._recv_event(ws)
                self.assertEqual(resp.type, EventType.PLUGIN_INSTALL_RESPONSE)
                self.assertFalse(resp.payload["success"])
                self.assertIn("rejected", resp.payload["error"])

                self.assertNotIn("Phase101TestPlugin", engine_state.plugin_installer.get_all())
                from aether_engine.routes import websocket as ws_routes

                self.assertNotIn("req-reject-1", ws_routes.pending_plugin_installs)

                # Session must still be alive after a clean rejection.
                ws.send_text(Event(type=EventType.PLUGIN_LIST_REQUEST).to_json())
                listed = self._recv_event(ws)
                self.assertEqual(listed.type, EventType.PLUGIN_LIST_RESPONSE)

    def test_grant_with_unknown_request_id_does_not_drop_session(self):
        # Pre-fix: even a grant for an unknown request id raised NameError
        # and killed the session.
        with TestClient(app) as client, self._connect(client) as ws:
            self._hello(ws)
            ws.send_text(Event(
                type=EventType.PLUGIN_APPROVAL_GRANTED,
                request_id="req-never-pending",
            ).to_json())
            # No response is expected for an unknown grant (data is None),
            # but the session must stay alive for the next request.
            ws.send_text(Event(type=EventType.PLUGIN_LIST_REQUEST).to_json())
            listed = self._recv_event(ws)
            self.assertEqual(listed.type, EventType.PLUGIN_LIST_RESPONSE)


if __name__ == "__main__":
    unittest.main()
