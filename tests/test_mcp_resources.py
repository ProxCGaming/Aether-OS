"""Issue 006: load an MCP resource into chat.

TDD red first: these tests fail until list_resources / read_resource
exist in the client, registry, and websocket handlers.

The fake HTTP server (tests/fake_mcp_http.py) exposes two resources:
``fake://notes`` (short text) and ``fake://big`` (25,000 chars, for the
truncation rule). Resources are read-only data — loading one must never
execute anything, and a resource is never auto-loaded.
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.mcp.registry import MCPRegistry
from tests.fake_mcp_http import start_fake_http_server


@pytest.fixture(scope="module")
def fake_url():
    url, stop = start_fake_http_server()
    yield url
    stop()


@pytest.fixture()
def manager():
    from aether_engine.mcp.client import MCPClientManager

    return MCPClientManager()


async def test_list_and_read_resource(fake_url, manager):
    config = {"transport": "streamable_http", "url": fake_url, "timeout": 10}
    result = await manager.connect("fake", config)
    assert result["ok"] is True, result

    resources = await manager.list_resources("fake")
    uris = [r["uri"] for r in resources]
    assert "fake://notes" in uris
    assert "fake://big" in uris

    text = await manager.read_resource("fake", "fake://notes")
    assert "The launch code is 42." in text

    await manager.disconnect("fake")


async def test_read_resource_truncates_with_note(fake_url, manager):
    config = {"transport": "streamable_http", "url": fake_url, "timeout": 10}
    await manager.connect("fake", config)
    text = await manager.read_resource("fake", "fake://big")
    assert len(text) <= 20000 + len("\n...[resource truncated to 20000 chars]")
    assert "truncated" in text
    await manager.disconnect("fake")


async def test_resources_on_disconnected_server_fail_gracefully(manager):
    from aether_engine.mcp.client import MCPClientError

    with pytest.raises(MCPClientError):
        await manager.list_resources("ghost")
    with pytest.raises(MCPClientError):
        await manager.read_resource("ghost", "fake://notes")


async def test_registry_lists_and_reads_resources(fake_url):
    with tempfile.TemporaryDirectory() as tmp:
        registry = MCPRegistry(config_path=Path(tmp) / "mcp_servers.json")
        registry.add_server(
            "fake",
            {"transport": "streamable_http", "url": fake_url, "timeout": 10},
        )
        result = await registry.connect_server("fake")
        assert result["ok"] is True, result
        resources = await registry.list_resources("fake")
        assert "fake://notes" in [r["uri"] for r in resources]
        text = await registry.read_resource("fake", "fake://notes")
        assert "The launch code is 42." in text
        await registry.disconnect_server("fake")


class TestMCPResourceWebSocketHandlers:
    """Handler level: real /ws/tasks endpoint, real registry on a temp
    config file, session_store pointed at a temp DB (same pattern as
    test_mcp_websocket_handlers.py)."""

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
        # session_store caches "initialised" in a module global: a fresh
        # tmp DB per test needs its tables created, so reset it here.
        from aether_engine.memory import session_store

        session_store._initialized = False
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
        return client.websocket_connect(f"/ws/tasks?token={engine_state.auth_token}")

    def _send(self, ws, type_, payload):
        ws.send_text(Event(type=type_, payload=payload).to_json())
        return Event.from_json(ws.receive_text())

    def _add_and_connect_fake(self, ws, fake_url):
        self.registry.add_server(
            "fake",
            {"transport": "streamable_http", "url": fake_url, "timeout": 10, "auto_connect": False},
        )
        resp = self._send(ws, EventType.MCP_SERVER_CONNECT_REQUEST, {"name": "fake"})
        assert resp.payload["success"] is True, resp.payload

    def _disconnect_fake(self, ws):
        # A manually-connected session must be closed in the task that
        # opened it: leaving it open makes TestClient teardown trip the
        # SDK's anyio cancel-scope rule ("attempted to exit a cancel
        # scope in a different task").
        resp = self._send(ws, EventType.MCP_SERVER_DISCONNECT_REQUEST, {"name": "fake"})
        assert resp.payload["success"] is True, resp.payload

    def test_list_resources_over_websocket(self, fake_url):
        with TestClient(app) as client:
            with self._connect(client) as ws:
                ws.receive_text()  # HELLO
                self._add_and_connect_fake(ws, fake_url)
                resp = self._send(
                    ws, EventType.MCP_RESOURCE_LIST_REQUEST, {"name": "fake"}
                )
                assert resp.type == EventType.MCP_RESOURCE_LIST_RESPONSE
                assert resp.payload["success"] is True
                uris = [r["uri"] for r in resp.payload["resources"]]
                assert "fake://notes" in uris
                self._disconnect_fake(ws)

    def test_read_resource_appends_to_current_chat(self, fake_url):
        from aether_engine.memory import session_store

        with TestClient(app) as client:
            with self._connect(client) as ws:
                ws.receive_text()  # HELLO
                self._add_and_connect_fake(ws, fake_url)
                session_id = session_store.create_session("chat")
                resp = self._send(
                    ws,
                    EventType.MCP_RESOURCE_READ_REQUEST,
                    {"name": "fake", "uri": "fake://notes", "session_id": session_id},
                )
                assert resp.type == EventType.MCP_RESOURCE_READ_RESPONSE
                assert resp.payload["success"] is True, resp.payload
                assert "The launch code is 42." in resp.payload["content"]
                assert resp.payload["session_id"] == session_id
                # The content is in the chat history the model reads via
                # get_current_chat_history (supervisor memory briefing).
                sess = session_store.get_session(session_id)
                contents = [m["content"] for m in sess["messages"]]
                assert any("The launch code is 42." in c for c in contents)
                self._disconnect_fake(ws)

    def test_read_resource_without_session_creates_one(self, fake_url):
        from aether_engine.memory import session_store

        with TestClient(app) as client:
            with self._connect(client) as ws:
                ws.receive_text()  # HELLO
                self._add_and_connect_fake(ws, fake_url)
                resp = self._send(
                    ws,
                    EventType.MCP_RESOURCE_READ_REQUEST,
                    {"name": "fake", "uri": "fake://notes"},
                )
                assert resp.payload["success"] is True, resp.payload
                assert resp.payload["session_id"]
                sess = session_store.get_session(resp.payload["session_id"])
                assert sess is not None
                assert any("The launch code is 42." in m["content"] for m in sess["messages"])
                self._disconnect_fake(ws)

    def test_read_resource_on_disconnected_server_fails(self, fake_url):
        self.registry.add_server(
            "fake",
            {"transport": "streamable_http", "url": fake_url, "timeout": 10, "auto_connect": False},
        )
        with TestClient(app) as client:
            with self._connect(client) as ws:
                ws.receive_text()  # HELLO
                resp = self._send(
                    ws,
                    EventType.MCP_RESOURCE_READ_REQUEST,
                    {"name": "fake", "uri": "fake://notes"},
                )
                assert resp.type == EventType.MCP_RESOURCE_READ_RESPONSE
                assert resp.payload["success"] is False
                assert resp.payload["error"]
                assert "not connected" in resp.payload["error"]
