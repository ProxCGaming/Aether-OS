"""Issue 009 (S7): MCP websocket handlers, tested at handler level.

Drives the real /ws/tasks endpoint (Starlette TestClient, as in
test_engine_ws.py) with the real MCPRegistry pointed at a temp config
file, so ADD/CONNECT/TEST go through the real handler code — not a mock
of it. TDD red first: fails until the 009 fixes land.
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.mcp.registry import MCPRegistry
from tests.fake_mcp_http import start_fake_http_server


class TestMCPWebSocketHandlers:
    def setup_method(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        engine_state.token_path = self.tmp_path / "engine.token"
        engine_state.audit_logger.log_path = self.tmp_path / "audit.log"
        self.token = engine_state.initialize_auth()
        self._db_patcher = patch(
            "aether_engine.memory.session_store.DB_PATH",
            self.tmp_path / "test_memory.db",
        )
        self._db_patcher.start()
        self.config_path = self.tmp_path / "mcp_servers.json"
        self.registry = MCPRegistry(config_path=self.config_path)
        self._patcher = patch(
            "aether_engine.mcp.registry.GLOBAL_MCP_REGISTRY", self.registry
        )
        self._patcher.start()

    def teardown_method(self):
        self._patcher.stop()
        self._db_patcher.stop()
        self.tmp_dir.cleanup()

    def _connect(self, client):
        # The app lifespan re-runs engine_state.initialize() when the
        # TestClient starts, regenerating the token - read the current one.
        return client.websocket_connect(f"/ws/tasks?token={engine_state.auth_token}")

    def _send_mcp(self, ws, type_, name=None, config=None):
        payload = {}
        if name is not None:
            payload["name"] = name
        if config is not None:
            payload["config"] = config
        ws.send_text(
            Event(type=type_, payload=payload).to_json()
        )
        return Event.from_json(ws.receive_text())

    def test_invalid_add_returns_failure_with_error(self):
        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            resp = self._send_mcp(
                ws, EventType.MCP_SERVER_ADD_REQUEST, "bad",
                {"transport": "streamable_http", "url": "http://a/b", "command": "npx"},
            )
            assert resp.type == EventType.MCP_SERVER_ADD_RESPONSE
            assert resp.payload["success"] is False
            assert "exactly one" in resp.payload["error"]

    def test_add_response_redacts_secrets(self):
        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            resp = self._send_mcp(
                ws, EventType.MCP_SERVER_ADD_REQUEST, "remote",
                {
                    "transport": "streamable_http",
                    "url": "http://127.0.0.1:9/mcp",
                    "headers": {"x-api-key": "secret-123"},
                },
            )
            assert resp.payload["success"] is True
            assert "secret-123" not in json.dumps(resp.payload)
            assert resp.payload["config"]["headers"] == ["x-api-key"]

    def test_list_response_redacts_secrets(self):
        self.registry.add_server(
            "remote",
            {
                "transport": "streamable_http",
                "url": "http://127.0.0.1:9/mcp",
                "headers": {"x-api-key": "secret-123"},
            },
        )
        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            resp = self._send_mcp(ws, EventType.MCP_SERVER_LIST_REQUEST)
            assert resp.type == EventType.MCP_SERVER_LIST_RESPONSE
            assert "secret-123" not in json.dumps(resp.payload)
            assert resp.payload["servers"]["remote"]["headers"] == ["x-api-key"]

    def test_connect_on_dead_server_returns_failure(self):
        self.registry.add_server(
            "dead",
            {"transport": "streamable_http", "url": "http://127.0.0.1:9/mcp", "timeout": 2},
        )
        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            resp = self._send_mcp(ws, EventType.MCP_SERVER_CONNECT_REQUEST, "dead")
            assert resp.type == EventType.MCP_SERVER_CONNECT_RESPONSE
            assert resp.payload["success"] is False
            assert resp.payload["error"]

    def test_test_lists_tools_without_leaving_a_session(self):
        url, stop = start_fake_http_server()
        try:
            # Issue 004: auto_connect must be off here, or the lifespan
            # auto-connect (engine startup) would connect this server
            # before TEST runs, and the "no session left" assertion below
            # would be testing startup, not TEST.
            self.registry.add_server(
                "fake", {"transport": "streamable_http", "url": url, "timeout": 10, "auto_connect": False}
            )
            with TestClient(app) as client, self._connect(client) as ws:
                ws.receive_text()  # HELLO
                resp = self._send_mcp(ws, EventType.MCP_SERVER_TEST_REQUEST, "fake")
                assert resp.type == EventType.MCP_SERVER_TEST_RESPONSE
                assert resp.payload["success"] is True
                assert resp.payload["tools"] == ["add"]
                # No session left behind: LIST still shows disconnected.
                listed = self._send_mcp(ws, EventType.MCP_SERVER_LIST_REQUEST)
                assert listed.payload["servers"]["fake"]["status"] == "disconnected"
                assert listed.payload["servers"]["fake"]["tool_count"] == 0
        finally:
            stop()
