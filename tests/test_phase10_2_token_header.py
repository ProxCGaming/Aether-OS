"""Phase 10.2 B1 — engine token: header-only WebSocket auth, 0600 token file.

Before this fix:
- ``desktop_app/main.cjs`` handed the full engine token to the renderer via
  the ``get-auth-token`` IPC channel (exposed as ``getAuthToken`` in
  ``preload.cjs``) and sent the token in the WebSocket URL query string
  (``?token=...``), which lands in logs/history/error reports.
- ``aether_common.auth.write_token`` wrote the token plaintext with default
  umask permissions and a non-atomic write.

After this fix:
- The renderer has no token accessor; the Electron main process owns the
  WebSocket and authenticates with an ``Authorization: Bearer`` header only.
- The token file is written atomically (temp file + rename) with 0600
  owner-only permissions.

S1-M3 follow-up: the engine side originally accepted the query token
FIRST (``_extract_token`` returned ``?token=...`` before ever looking at
the header), so a valid token in the URL still authenticated — and URLs
land in uvicorn access logs and proxy/browser histories. The engine now
accepts the ``Authorization: Bearer`` header ONLY: a token presented as
a URL query parameter is refused (1008), with no fallback to query auth.
Route choice: the real client connection is owned by the Electron MAIN
process (``desktop_app/main.cjs`` ``connectEngine``, Node ``ws``), which
can set arbitrary headers — the renderer never opens this socket and
never holds the token — and the legacy Python client
(``aether_ui/ws_client.py``, ``websockets`` library) can set headers
too. Browser ``WebSocket`` cannot set headers, but no browser/renderer
code connects to this endpoint directly, so neither the subprotocol
route nor a first-message auth frame is needed.
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aether_common.auth import generate_token, read_token, write_token
from aether_common.contracts import SCHEMA_VERSION, Event, EventType
from aether_engine.app import app, engine_state

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestHeaderTokenAuth(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.token_file = self.tmp_path / "engine.token"
        self.audit_file = self.tmp_path / "audit.log"

        engine_state.token_path = self.token_file
        engine_state.audit_logger.log_path = self.audit_file
        self.token = engine_state.initialize_auth()

        self.patcher = patch(
            "aether_engine.memory.session_store.DB_PATH",
            self.tmp_path / "test_memory.db",
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp_dir.cleanup()

    def test_websocket_header_token_accepted_and_hello(self):
        """Header-only auth works: no query string, HELLO is received."""
        with TestClient(app) as client:
            # NB: entering the TestClient runs the app lifespan, which
            # re-initializes auth (new token). Read the live token here,
            # as tests/test_engine_ws.py does — the setUp token is stale.
            live_token = engine_state.auth_token
            with client.websocket_connect(
                "/ws/tasks",
                headers={"Authorization": f"Bearer {live_token}"},
            ) as ws:
                hello_event = Event.from_json(ws.receive_text())
                self.assertEqual(hello_event.type, EventType.HELLO)
                self.assertEqual(hello_event.schema_version, SCHEMA_VERSION)

        audit_content = self.audit_file.read_text(encoding="utf-8")
        self.assertIn('"success": true', audit_content)

    def test_websocket_no_header_token_rejected_1008(self):
        """No header token (and no query token) -> close 1008."""
        with TestClient(app) as client:
            with self.assertRaises(WebSocketDisconnect) as cm:  # noqa: SIM117 — nested for readability; combined form lands with the tip cleanup
                with client.websocket_connect("/ws/tasks"):
                    pass
            self.assertEqual(cm.exception.code, 1008)

        audit_content = self.audit_file.read_text(encoding="utf-8")
        self.assertIn("Unauthorized", audit_content)
        self.assertIn('"success": false', audit_content)

    def test_websocket_wrong_header_token_rejected_1008(self):
        """A wrong Bearer token -> close 1008."""
        with TestClient(app) as client:
            with self.assertRaises(WebSocketDisconnect) as cm:  # noqa: SIM117 — nested for readability; combined form lands with the tip cleanup
                with client.websocket_connect(
                    "/ws/tasks",
                    headers={"Authorization": "Bearer wrong-token-12345"},
                ):
                    pass
            self.assertEqual(cm.exception.code, 1008)

    def test_websocket_query_token_valid_rejected_1008(self):
        """S1-M3: a VALID token in the URL query string must NOT
        authenticate — query tokens land in access logs and histories,
        so the query route is refused outright (close 1008, pre-accept)."""
        with TestClient(app) as client:
            live_token = engine_state.auth_token
            with self.assertRaises(WebSocketDisconnect) as cm, client.websocket_connect(f"/ws/tasks?token={live_token}"):
                pass
            self.assertEqual(cm.exception.code, 1008)

        audit_content = self.audit_file.read_text(encoding="utf-8")
        self.assertIn("Unauthorized", audit_content)
        self.assertIn('"success": false', audit_content)

    def test_websocket_query_token_does_not_rescue_wrong_header(self):
        """S1-M3: no fallback — a valid query token must not rescue a
        connection whose header token is wrong."""
        with TestClient(app) as client:
            live_token = engine_state.auth_token
            with self.assertRaises(WebSocketDisconnect) as cm, client.websocket_connect(
                f"/ws/tasks?token={live_token}",
                headers={"Authorization": "Bearer wrong-token-12345"},
            ):
                pass
            self.assertEqual(cm.exception.code, 1008)

    def test_websocket_query_token_ignored_when_header_valid(self):
        """S1-M3: the query string is not consulted at all — a bogus
        query token alongside a valid header still connects (the
        header alone governs)."""
        with TestClient(app) as client:
            live_token = engine_state.auth_token
            with client.websocket_connect(
                "/ws/tasks?token=bogus-query-token",
                headers={"Authorization": f"Bearer {live_token}"},
            ) as ws:
                hello_event = Event.from_json(ws.receive_text())
                self.assertEqual(hello_event.type, EventType.HELLO)


class TestEngineHasNoQueryTokenAuthPath(unittest.TestCase):
    """S1-M3 static guard: no engine code path may read a token from
    the WebSocket URL query string ever again."""

    def test_websocket_route_has_no_query_token_parameter(self):
        src = (REPO_ROOT / "aether_engine" / "routes" / "websocket.py").read_text(
            encoding="utf-8")
        self.assertNotIn("query_token", src)
        self.assertNotIn("Query(default=None)", src)


@unittest.skipIf(os.name == "nt", "POSIX mode bits do not apply on Windows")
class TestTokenFilePermissions(unittest.TestCase):
    def test_write_token_file_is_owner_only_0600(self):
        token = generate_token()
        token_file = Path(tempfile.mkdtemp()) / "sub" / "engine.token"
        written = write_token(token, token_file)
        mode = stat.S_IMODE(written.stat().st_mode)
        self.assertEqual(mode, 0o600, f"token file mode is {oct(mode)}, expected 0o600")
        self.assertEqual(read_token(written), token)

    def test_write_token_replaces_world_readable_file_with_0600(self):
        """Re-writing over a pre-existing 0644 token file leaves 0600 behind."""
        token_dir = Path(tempfile.mkdtemp())
        token_file = token_dir / "engine.token"
        token_file.write_text("old-token", encoding="utf-8")
        os.chmod(token_file, 0o644)
        self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o644)

        write_token(generate_token(), token_file)
        self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)

    def test_write_token_leaves_no_temp_files_behind(self):
        token_dir = Path(tempfile.mkdtemp())
        write_token(generate_token(), token_dir / "engine.token")
        leftovers = [p for p in token_dir.iterdir() if p.suffix == ".tmp"]
        self.assertEqual(leftovers, [])

    def test_engine_token_file_written_0600_on_initialize(self):
        """engine_state.initialize_auth() (via write_token) creates a 0600 file."""
        tmp_dir = tempfile.TemporaryDirectory()
        try:
            token_file = Path(tmp_dir.name) / "engine.token"
            audit_file = Path(tmp_dir.name) / "audit.log"
            engine_state.token_path = token_file
            engine_state.audit_logger.log_path = audit_file
            engine_state.initialize_auth()
            mode = stat.S_IMODE(token_file.stat().st_mode)
            self.assertEqual(mode, 0o600, f"token file mode is {oct(mode)}, expected 0o600")
        finally:
            tmp_dir.cleanup()


class TestRendererHasNoTokenAccess(unittest.TestCase):
    """Static guard: the Electron renderer surface must not expose the token.

    Mirrors the Jest test in ``desktop_app/src/__tests__/preload-no-token.test.js``
    so the same invariant is also checked by the Python suite.
    """

    def _read(self, rel: str) -> str:
        return (REPO_ROOT / rel).read_text(encoding="utf-8")

    def test_preload_exposes_no_token_accessor(self):
        preload = self._read("desktop_app/preload.cjs")
        self.assertNotIn("getAuthToken", preload)
        self.assertNotIn("get-auth-token", preload)

    def test_main_has_no_get_auth_token_ipc_handler(self):
        main = self._read("desktop_app/main.cjs")
        self.assertNotIn("get-auth-token", main)
        self.assertNotIn("getAuthToken", main)

    def test_main_never_sends_token_in_query_string(self):
        main = self._read("desktop_app/main.cjs")
        self.assertNotIn("?token=", main)
        self.assertNotIn("token=${", main)

    def test_main_sends_token_in_authorization_header(self):
        main = self._read("desktop_app/main.cjs")
        self.assertIn("Authorization", main)
        self.assertIn("Bearer ", main)


if __name__ == "__main__":
    unittest.main()
