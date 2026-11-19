"""Wave 5 Lane A — MCP depth: per-server tool detail (D4-2),
edit-in-place with the trust gate (D4-3 remainder), and registry
update state (FF4-04, MCP half).

Pins:

* the MCP list answer carries each server's converted tools —
  ``None`` while they are unknown (never connected), ``[]`` for a
  connected server offering none — enriched from the same policy
  store the Tools list reads, so the two views cannot disagree;
* a tool's origin is on the entry (``source == "mcp:<server>"``),
  the field the Tools panel badges from;
* editing a server's run configuration in place replaces it
  (multi-pair headers/env, secret values kept by key name, fields
  removed by explicit null, transport switchable), and changing
  what runs resets ``confirmed`` unless the edit itself carried
  the confirmation — a no-op edit never trips the gate;
* a registry-imported server can be checked for a newer published
  version; the result persists and surfaces as ``has_update`` in
  the list answer. Servers without registry provenance refuse the
  check honestly.
"""
from types import SimpleNamespace

import pytest

from aether_engine.mcp import registry as registry_module
from aether_engine.mcp.registry import (
    MCPConfigError,
    MCPRegistry,
    _latest_version_from_registry_payload,
)


class _HandoverClient:
    """Client double: connect hands over a fixed tool listing."""

    def __init__(self, tools=()):
        self._tools = list(tools)
        self.connected = set()

    async def connect(self, name, config):
        self.connected.add(name)
        return {"ok": True, "error": None, "tools": list(self._tools)}

    async def connect_owned(self, name, config):
        return await self.connect(name, config)

    async def list_tools(self, name):
        raise AssertionError("registry listed the tools a second time")

    def get_status(self, name):
        if name in self.connected:
            return {"status": "connected", "error": None,
                    "tool_count": len(self._tools)}
        return {"status": "disconnected", "error": None, "tool_count": 0}

    def is_connected(self, name):
        return name in self.connected

    async def disconnect(self, name):
        self.connected.discard(name)
        return {"ok": True, "error": None}

    def set_failed(self, name, error):
        raise AssertionError("a handed-over connect must not be marked failed")


def _registry(tmp_path, client=None):
    return MCPRegistry(config_path=tmp_path / "mcp_servers.json", client=client)


def _tool(name="add", description="Add two numbers"):
    return SimpleNamespace(
        name=name, description=description,
        inputSchema={"type": "object", "properties": {}},
    )


# ---------------------------------------------------------------------------
# D4-2: per-server tool detail on the list answer
# ---------------------------------------------------------------------------

async def test_tools_unknown_until_connected_then_listed(tmp_path):
    client = _HandoverClient(tools=[_tool()])
    registry = _registry(tmp_path, client)
    registry.add_server(
        "fake", {"transport": "streamable_http", "url": "http://127.0.0.1:9/mcp"})

    # Never connected: tools are unknown — None, not an empty list.
    entry = registry.list_servers_with_status()["fake"]
    assert entry["tools"] is None

    await registry.connect_server("fake")
    entry = registry.list_servers_with_status()["fake"]
    assert entry["tool_count"] == 1
    (tool_entry,) = entry["tools"]
    assert tool_entry["name"] == "fake__add"
    assert tool_entry["tool_name"] == "add"
    assert tool_entry["description"] == "Add two numbers"
    # The origin field the Tools panel badges from.
    assert tool_entry["source"] == "mcp:fake"
    assert tool_entry["risk"] == "execute"
    # No user override: the entry reports the default honestly.
    assert tool_entry["policy_is_default"] is True
    assert tool_entry["policy"] == "Require Approval"
    assert tool_entry["effective_policy"] == "ask"
    assert tool_entry["always_allow_permitted"] is False

    # Disconnect clears the cache: unknown again, never stale.
    await registry.disconnect_server("fake")
    assert registry.list_servers_with_status()["fake"]["tools"] is None


async def test_connected_server_with_no_tools_reports_empty_list(tmp_path):
    client = _HandoverClient(tools=[])
    registry = _registry(tmp_path, client)
    registry.add_server(
        "bare", {"transport": "streamable_http", "url": "http://127.0.0.1:9/mcp"})
    await registry.connect_server("bare")
    assert registry.list_servers_with_status()["bare"]["tools"] == []


async def test_tool_entries_reflect_the_shared_policy_store(tmp_path):
    client = _HandoverClient(tools=[_tool()])
    registry = _registry(tmp_path, client)
    registry.add_server(
        "fake", {"transport": "streamable_http", "url": "http://127.0.0.1:9/mcp"})
    await registry.connect_server("fake")

    entry = registry.list_servers_with_status(
        user_policies={"fake__add": "Deny"})["fake"]
    (tool_entry,) = entry["tools"]
    assert tool_entry["policy"] == "Deny"
    assert tool_entry["policy_is_default"] is False
    assert tool_entry["effective_policy"] == "deny"


async def test_tool_entries_honour_risk_overrides(tmp_path):
    client = _HandoverClient(tools=[_tool()])
    registry = _registry(tmp_path, client)
    registry.add_server(
        "fake", {"transport": "streamable_http", "url": "http://127.0.0.1:9/mcp"})
    await registry.connect_server("fake")

    entry = registry.list_servers_with_status(
        risk_overrides={"fake__add": "read_only"})["fake"]
    (tool_entry,) = entry["tools"]
    assert tool_entry["risk"] == "read_only"
    assert tool_entry["always_allow_permitted"] is True


# ---------------------------------------------------------------------------
# D4-3: edit-in-place + the trust gate
# ---------------------------------------------------------------------------

async def test_edit_replaces_run_config_and_resets_confirmation(tmp_path):
    registry = _registry(tmp_path)
    registry.add_server("srv", {
        "transport": "stdio", "command": "npx", "args": ["-y", "old-pkg"],
        "confirmed": True,
    })
    result = await registry.update_server(
        "srv", config={"command": "npx", "args": ["-y", "new-pkg"]})
    assert result["args"] == ["-y", "new-pkg"]
    # What runs changed and the edit was not itself confirmed:
    # the marker must not carry over.
    assert result["confirmed"] is False
    reloaded = _registry(tmp_path)
    assert reloaded.list_servers()["srv"]["confirmed"] is False
    assert reloaded.list_servers()["srv"]["args"] == ["-y", "new-pkg"]


async def test_confirmed_edit_keeps_the_marker(tmp_path):
    registry = _registry(tmp_path)
    registry.add_server("srv", {
        "transport": "stdio", "command": "npx", "args": ["old"],
        "confirmed": True,
    })
    result = await registry.update_server(
        "srv", config={"command": "uvx", "args": ["new"]}, confirmed=True)
    assert result["command"] == "uvx"
    assert result["confirmed"] is True


async def test_noop_edit_never_trips_the_gate(tmp_path):
    registry = _registry(tmp_path)
    registry.add_server("srv", {
        "transport": "stdio", "command": "npx", "args": ["pkg"],
        "confirmed": True,
    })
    # Only the timeout changes — not a run field.
    result = await registry.update_server("srv", config={"timeout": 60})
    assert result["timeout"] == 60
    assert result["confirmed"] is True
    # Re-saving the identical run config is also a no-op.
    result = await registry.update_server(
        "srv", config={"command": "npx", "args": ["pkg"]})
    assert result["confirmed"] is True


async def test_edit_keeps_secret_values_by_key_name(tmp_path):
    registry = _registry(tmp_path)
    registry.add_server("remote", {
        "transport": "streamable_http", "url": "http://127.0.0.1:9/mcp",
        "headers": {"x-api-key": "s3cret", "x-drop": "gone"},
        "confirmed": True,
    })
    result = await registry.update_server("remote", config={
        "url": "http://127.0.0.1:9/mcp",
        # x-api-key kept by name (value None), x-drop deleted by
        # absence, x-new added with a value.
        "headers": {"x-api-key": None, "x-new": "fresh"},
    }, confirmed=True)
    # The list answer still redacts: names only, never values.
    assert result["headers"] == ["x-api-key", "x-new"]
    stored = registry.list_servers()["remote"]["headers"]
    assert stored == {"x-api-key": "s3cret", "x-new": "fresh"}


async def test_edit_keep_for_unstored_key_is_an_error(tmp_path):
    registry = _registry(tmp_path)
    registry.add_server("remote", {
        "transport": "streamable_http", "url": "http://127.0.0.1:9/mcp",
    })
    with pytest.raises(MCPConfigError, match="no stored value to keep"):
        await registry.update_server(
            "remote", config={"headers": {"x-missing": None}})


async def test_edit_removes_fields_and_switches_transport(tmp_path):
    registry = _registry(tmp_path)
    registry.add_server("srv", {
        "transport": "stdio", "command": "npx", "args": ["pkg"],
        "env": {"TOKEN": "x"}, "cwd": "/tmp/work", "confirmed": True,
    })
    result = await registry.update_server("srv", config={
        "command": None, "args": None, "env": None, "cwd": None,
        "url": "http://127.0.0.1:9/mcp", "headers": {},
    }, confirmed=True)
    assert result["transport"] == "streamable_http"
    assert result["url"] == "http://127.0.0.1:9/mcp"
    stored = registry.list_servers()["srv"]
    assert "command" not in stored
    assert "cwd" not in stored


async def test_edit_disconnects_a_connected_server(tmp_path):
    client = _HandoverClient(tools=[_tool()])
    registry = _registry(tmp_path, client)
    registry.add_server("srv", {
        "transport": "stdio", "command": "npx", "args": ["old"],
        "confirmed": True,
    })
    await registry.connect_server("srv")
    assert client.is_connected("srv")
    await registry.update_server(
        "srv", config={"command": "npx", "args": ["new"]}, confirmed=True)
    # The live session ran the old config; it cannot stay up.
    assert not client.is_connected("srv")


async def test_edit_unknown_server_raises(tmp_path):
    registry = _registry(tmp_path)
    with pytest.raises(MCPConfigError, match="Unknown MCP server"):
        await registry.update_server("ghost", config={"command": "npx"})


# ---------------------------------------------------------------------------
# FF4-04 (MCP half): registry version state
# ---------------------------------------------------------------------------

def test_latest_version_payload_parsing():
    payload = {"servers": [
        {"server": {"name": "io.x/srv", "version": "1.9.0"},
         "_meta": {"io.modelcontextprotocol.registry/official":
                   {"isLatest": False}}},
        {"server": {"name": "io.x/srv", "version": "1.10.0"},
         "_meta": {"io.modelcontextprotocol.registry/official":
                   {"isLatest": True}}},
    ]}
    assert _latest_version_from_registry_payload(payload) == "1.10.0"
    # Without an isLatest flag the numeric max wins (1.10 > 1.9).
    payload["servers"][1]["_meta"][
        "io.modelcontextprotocol.registry/official"]["isLatest"] = False
    assert _latest_version_from_registry_payload(payload) == "1.10.0"
    with pytest.raises(MCPConfigError):
        _latest_version_from_registry_payload({"servers": []})
    with pytest.raises(MCPConfigError):
        _latest_version_from_registry_payload({"unexpected": True})


async def test_update_check_requires_registry_provenance(tmp_path):
    registry = _registry(tmp_path)
    registry.add_server("plain", {"transport": "stdio", "command": "npx"})
    with pytest.raises(MCPConfigError, match="not imported from a registry"):
        await registry.check_for_update("plain")


async def test_update_check_records_latest_and_surfaces_has_update(
        tmp_path, monkeypatch):
    registry = _registry(tmp_path)
    registry.add_server("imported", {
        "transport": "stdio", "command": "npx", "args": ["pkg"],
        "version": "1.0.0", "registry_name": "io.example/imported",
    })
    # No check yet: the chip honestly matches nothing.
    assert registry.list_servers_with_status()["imported"]["has_update"] is False

    async def _fake_fetch(registry_name):
        assert registry_name == "io.example/imported"
        return "1.2.0"

    monkeypatch.setattr(
        registry_module, "fetch_registry_latest_version", _fake_fetch)
    result = await registry.check_for_update("imported")
    assert result == {
        "name": "imported", "version": "1.0.0",
        "latest_version": "1.2.0", "has_update": True,
    }
    entry = registry.list_servers_with_status()["imported"]
    assert entry["has_update"] is True
    assert entry["version"] == "1.0.0"
    assert entry["registry_latest_version"] == "1.2.0"
    # The recorded check persists across a reload.
    reloaded = _registry(tmp_path)
    assert reloaded.list_servers_with_status()["imported"]["has_update"] is True


# ---------------------------------------------------------------------------
# Wire level: the same behaviours through the real /ws/tasks handlers
# ---------------------------------------------------------------------------

class TestWave5Handlers:
    """Drives the real websocket endpoint with a temp-config registry
    (the test_mcp_websocket_handlers harness pattern)."""

    @pytest.fixture(autouse=True)
    def _harness(self, tmp_path):
        from unittest.mock import patch

        from aether_engine.app import engine_state

        self._engine_state = engine_state
        engine_state.token_path = tmp_path / "engine.token"
        engine_state.audit_logger.log_path = tmp_path / "audit.log"
        engine_state.initialize()
        self._db_patcher = patch(
            "aether_engine.memory.session_store.DB_PATH",
            tmp_path / "test_memory.db",
        )
        self._db_patcher.start()
        self.registry = MCPRegistry(config_path=tmp_path / "mcp_servers.json")
        self._patcher = patch(
            "aether_engine.mcp.registry.GLOBAL_MCP_REGISTRY", self.registry
        )
        self._patcher.start()
        yield
        self._patcher.stop()
        self._db_patcher.stop()

    def _roundtrip(self, ws, type_, payload):
        from aether_common.contracts import Event

        ws.send_text(Event(type=type_, payload=payload).to_json())
        return Event.from_json(ws.receive_text())

    def test_update_with_config_and_update_check_over_the_wire(self):
        from starlette.testclient import TestClient

        from aether_common.contracts import EventType
        from aether_engine.app import app

        self.registry.add_server("srv", {
            "transport": "stdio", "command": "npx", "args": ["old"],
            "confirmed": True,
        })
        with TestClient(app) as client:
            ws = client.websocket_connect(
                "/ws/tasks",
                headers={"Authorization": f"Bearer {self._engine_state.auth_token}"},
            )
            with ws:
                ws.receive_text()  # HELLO
                resp = self._roundtrip(ws, EventType.MCP_SERVER_UPDATE_REQUEST, {
                    "name": "srv", "config": {"command": "npx", "args": ["new"]},
                })
                assert resp.type == EventType.MCP_SERVER_UPDATE_RESPONSE
                assert resp.payload["success"] is True
                # The run change was not confirmed on the card, so
                # the wire answer shows the marker reset.
                assert resp.payload["config"]["confirmed"] is False
                assert resp.payload["config"]["args"] == ["new"]

                resp = self._roundtrip(
                    ws, EventType.MCP_SERVER_UPDATE_CHECK_REQUEST,
                    {"name": "srv"})
                assert resp.type == EventType.MCP_SERVER_UPDATE_CHECK_RESPONSE
                assert resp.payload["success"] is False
                assert "not imported from a registry" in resp.payload["error"]

                resp = self._roundtrip(
                    ws, EventType.MCP_SERVER_LIST_REQUEST, {})
                entry = resp.payload["servers"]["srv"]
                assert entry["tools"] is None
                assert entry["has_update"] is False


async def test_update_check_up_to_date_and_failed_check_is_honest(
        tmp_path, monkeypatch):
    registry = _registry(tmp_path)
    registry.add_server("imported", {
        "transport": "stdio", "command": "npx",
        "version": "2.0.0", "registry_name": "io.example/imported",
    })

    async def _same(registry_name):
        return "2.0.0"

    monkeypatch.setattr(
        registry_module, "fetch_registry_latest_version", _same)
    result = await registry.check_for_update("imported")
    assert result["has_update"] is False

    async def _boom(registry_name):
        raise MCPConfigError("registry unreachable")

    monkeypatch.setattr(
        registry_module, "fetch_registry_latest_version", _boom)
    with pytest.raises(MCPConfigError, match="registry unreachable"):
        await registry.check_for_update("imported")
    # A failed check leaves the stored record untouched.
    stored = registry.list_servers()["imported"]
    assert stored["registry_latest_version"] == "2.0.0"
