"""Issue 004: auto-connect on startup, per-server toggle, fail fast.

TDD red first: these tests fail until the implementation lands.
- start_auto_connect() runs in the background: it returns immediately,
  one dead server cannot block startup or the others, opted-out /
  disabled servers stay disconnected, no retry ever.
- Sessions opened by a background task must be closed by their owner
  task: a stdio session disconnected from another task used to leak
  (anyio cancel-scope rule, "Attempted to exit cancel scope in a
  different task than it was entered in").
"""
import asyncio
import logging
import sys
import time

import pytest

from tests.fake_mcp_http import start_fake_http_server


@pytest.fixture(scope="module")
def fake_url():
    url, stop = start_fake_http_server()
    yield url
    stop()


def _registry(tmp_path):
    from aether_engine.mcp.registry import MCPRegistry

    return MCPRegistry(config_path=tmp_path / "mcp_servers.json")


def _http_config(url, **overrides):
    cfg = {"transport": "streamable_http", "url": url, "timeout": 5}
    cfg.update(overrides)
    return cfg


def _stdio_config(**overrides):
    cfg = {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["tests/fake_stdio_server.py"],
        "timeout": 10,
    }
    cfg.update(overrides)
    return cfg


async def _wait_for(predicate, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return predicate()


async def test_start_auto_connect_connects_eligible_skips_others_dead_does_not_block(
    tmp_path, fake_url
):
    registry = _registry(tmp_path)
    registry.add_server("live", _http_config(fake_url))
    registry.add_server("dead", _http_config("http://127.0.0.1:1/mcp"))
    registry.add_server("optout", _http_config(fake_url, auto_connect=False))
    registry.add_server("off", _http_config(fake_url, enabled=False))

    started = registry.start_auto_connect()
    assert "live" in started and "dead" in started
    assert "optout" not in started and "off" not in started

    assert await _wait_for(
        lambda: registry._client.get_status("live")["status"] == "connected"
    ), registry._client.get_status("live")
    assert await _wait_for(
        lambda: registry._client.get_status("dead")["status"] == "failed"
    ), registry._client.get_status("dead")
    assert registry._client.get_status("optout")["status"] == "disconnected"
    assert registry.list_servers_with_status()["off"]["status"] == "disabled"
    assert registry.get_mcp_tools()
    assert [t.name for t in registry.get_mcp_tools()] == ["live__add"]
    await registry.stop_auto_connect()


async def test_start_auto_connect_returns_immediately_with_dead_server(tmp_path):
    registry = _registry(tmp_path)
    registry.add_server("dead", _http_config("http://127.0.0.1:1/mcp", timeout=30))
    start = time.monotonic()
    registry.start_auto_connect()
    assert time.monotonic() - start < 2, "start_auto_connect blocked startup"
    assert await _wait_for(
        lambda: registry._client.get_status("dead")["status"] == "failed"
    )
    await registry.stop_auto_connect()


async def test_update_server_toggles_auto_connect_and_persists(tmp_path, fake_url):
    registry = _registry(tmp_path)
    registry.add_server("live", _http_config(fake_url))
    assert registry.list_servers_with_status()["live"]["auto_connect"] is True

    updated = await registry.update_server("live", auto_connect=False)
    assert updated["auto_connect"] is False
    assert registry.list_servers_with_status()["live"]["auto_connect"] is False

    # Persists across a reload from the same file.
    from aether_engine.mcp.registry import MCPRegistry

    reloaded = MCPRegistry(config_path=tmp_path / "mcp_servers.json")
    assert reloaded.list_servers_with_status()["live"]["auto_connect"] is False

    with pytest.raises(Exception):
        await registry.update_server("ghost", auto_connect=False)


async def test_update_server_disabling_enabled_disconnects(tmp_path, fake_url):
    registry = _registry(tmp_path)
    registry.add_server("live", _http_config(fake_url))
    result = await registry.connect_server("live")
    assert result["ok"] is True
    assert registry._client.is_connected("live")

    await registry.update_server("live", enabled=False)
    assert not registry._client.is_connected("live")
    assert registry.list_servers_with_status()["live"]["status"] == "disabled"

    # Re-enabling does not auto-reconnect — the user clicks Connect.
    await registry.update_server("live", enabled=True)
    assert not registry._client.is_connected("live")


async def test_owned_stdio_disconnect_from_another_task_closes_stack(tmp_path, caplog):
    from aether_engine.mcp.client import MCPClientManager

    manager = MCPClientManager()
    config = _stdio_config()
    with caplog.at_level(logging.WARNING, logger="aether_engine.mcp.client"):
        result = await manager.connect_owned("local", config)
        assert result["ok"] is True, result
        assert manager.is_connected("local")
        out = await manager.call_tool("local", "add", {"a": 3, "b": 5})
        assert out.strip() == "8"
        # Disconnect from a *different* task than the owner task.
        await asyncio.create_task(manager.disconnect("local"))
    assert not manager.is_connected("local")
    assert manager.get_status("local")["status"] == "disconnected"
    assert "different task" not in caplog.text, caplog.text
    # And a fresh manual reconnect still works (no leaked subprocess).
    result = await manager.connect("local", config)
    assert result["ok"] is True, result
    await manager.disconnect("local")


async def test_mid_task_kill_marks_failed_no_retry_manual_reconnect(tmp_path):
    registry = _registry(tmp_path)
    registry.add_server("local", _stdio_config())
    result = await registry.connect_server_owned("local")
    assert result["ok"] is True, result
    tools = {t.name: t for t in registry.get_mcp_tools()}
    assert "local__add" in tools

    # Kill the server mid-session via its own crash tool.
    crash = await tools["local__crash"].execute_fn()
    assert isinstance(crash, str)  # graceful error string, never a raise

    # The next call fails gracefully, the server is marked failed, and a
    # manual reconnect recovers — no background retry in between.
    out = await tools["local__add"].execute_fn(a=1, b=2)
    assert isinstance(out, str)
    assert "failed" in out.lower() or "not connected" in out.lower(), out
    assert registry._client.get_status("local")["status"] == "failed"
    assert registry.list_servers_with_status()["local"]["status"] == "failed"

    result = await registry.connect_server_owned("local")
    assert result["ok"] is True, result
    tools = {t.name: t for t in registry.get_mcp_tools()}
    assert await tools["local__add"].execute_fn(a=3, b=5) == "8"
    await registry.disconnect_server("local")


class TestMCPUpdateWebSocketHandlers:
    """UPDATE handler + CONNECT/DISCONNECT response shape, at handler level."""

    def setup_method(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from aether_engine.app import engine_state

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
        from aether_engine.mcp.registry import MCPRegistry

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
        from aether_engine.app import engine_state

        # The app lifespan re-runs engine_state.initialize() when the
        # TestClient starts, regenerating the token - read the current one.
        return client.websocket_connect(f"/ws/tasks?token={engine_state.auth_token}")

    def _send(self, ws, type_, payload):
        from aether_common.contracts import Event

        ws.send_text(Event(type=type_, payload=payload).to_json())
        return Event.from_json(ws.receive_text())

    def test_lifespan_auto_connects_configured_server(self, fake_url):
        # The server is configured *before* the engine starts: the
        # lifespan auto-connect must have connected it by the time the
        # UI asks for the list — no manual Connect click.
        self.registry.add_server(
            "fake", {"transport": "streamable_http", "url": fake_url, "timeout": 10}
        )
        from starlette.testclient import TestClient

        from aether_common.contracts import EventType
        from aether_engine.app import app

        with TestClient(app) as client:
            with self._connect(client) as ws:
                ws.receive_text()  # HELLO
                # Auto-connect runs in the background: the first LIST
                # may still show "disconnected" while the connect is in
                # flight. Poll until it lands (the UI refreshes the list
                # the same way after engine start).
                deadline = time.time() + 15
                servers = {}
                while time.time() < deadline:
                    resp = self._send(ws, EventType.MCP_SERVER_LIST_REQUEST, {})
                    servers = resp.payload["servers"]
                    if servers.get("fake", {}).get("status") == "connected":
                        break
                    time.sleep(0.2)
                assert servers["fake"]["status"] == "connected"
                assert servers["fake"]["tool_count"] == 1

    def test_update_auto_connect_toggle_persists_and_lists(self, fake_url):
        from starlette.testclient import TestClient

        from aether_common.contracts import EventType
        from aether_engine.app import app

        with TestClient(app) as client:
            with self._connect(client) as ws:
                ws.receive_text()  # HELLO
                resp = self._send(
                    ws, EventType.MCP_SERVER_ADD_REQUEST,
                    {"name": "fake", "config": {"transport": "streamable_http", "url": fake_url}},
                )
                assert resp.payload["success"] is True, resp.payload
                resp = self._send(
                    ws, EventType.MCP_SERVER_UPDATE_REQUEST,
                    {"name": "fake", "auto_connect": False},
                )
                assert resp.type == EventType.MCP_SERVER_UPDATE_RESPONSE
                assert resp.payload["success"] is True, resp.payload
                assert resp.payload["config"]["auto_connect"] is False
                resp = self._send(ws, EventType.MCP_SERVER_LIST_REQUEST, {})
                assert resp.payload["servers"]["fake"]["auto_connect"] is False

    def test_update_unknown_server_fails(self):
        from starlette.testclient import TestClient

        from aether_common.contracts import EventType
        from aether_engine.app import app

        with TestClient(app) as client:
            with self._connect(client) as ws:
                ws.receive_text()  # HELLO
                resp = self._send(
                    ws, EventType.MCP_SERVER_UPDATE_REQUEST,
                    {"name": "ghost", "auto_connect": False},
                )
                assert resp.payload["success"] is False
                assert "Unknown" in resp.payload["error"]

    def test_connect_response_carries_status_and_tool_count(self, fake_url):
        from starlette.testclient import TestClient

        from aether_common.contracts import EventType
        from aether_engine.app import app

        with TestClient(app) as client:
            with self._connect(client) as ws:
                ws.receive_text()  # HELLO
                self._send(
                    ws, EventType.MCP_SERVER_ADD_REQUEST,
                    {"name": "fake", "config": {"transport": "streamable_http", "url": fake_url}},
                )
                resp = self._send(
                    ws, EventType.MCP_SERVER_CONNECT_REQUEST, {"name": "fake"}
                )
                assert resp.type == EventType.MCP_SERVER_CONNECT_RESPONSE
                assert resp.payload["success"] is True, resp.payload
                assert resp.payload["status"] == "connected"
                assert resp.payload["tool_count"] == 1
                resp = self._send(
                    ws, EventType.MCP_SERVER_DISCONNECT_REQUEST, {"name": "fake"}
                )
                assert resp.payload["success"] is True, resp.payload
                assert resp.payload["status"] == "disconnected"
                assert resp.payload["tool_count"] == 0
