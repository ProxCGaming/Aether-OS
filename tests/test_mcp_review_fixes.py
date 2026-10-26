"""Issue 009: fresh-context review fixes on issue 001 (B1, S1-S5, S2).

TDD red first: every test here fails until the fix lands.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.fake_mcp_http import start_fake_http_server
from aether_engine.mcp.client import MCPClientError
from aether_engine.mcp.registry import MCPConfigError, MCPRegistry


@pytest.fixture(scope="module")
def fake_url():
    url, stop = start_fake_http_server()
    yield url
    stop()


@pytest.fixture()
def config_path(tmp_path):
    return tmp_path / "mcp_servers.json"


def _registry_with_stub_client(config_path, stub):
    return MCPRegistry(config_path=config_path, client=stub)


class _StubClient:
    """Minimal fake MCPClientManager for registry-level tests."""

    def __init__(self, tools=None, list_tools_error=None):
        self.tools = tools or []
        self.list_tools_error = list_tools_error
        self.connected = set()
        self.disconnect_calls = []
        self._failed = {}

    def get_status(self, name):
        if name in self._failed:
            return {"status": "failed", "error": self._failed[name], "tool_count": 0}
        if name in self.connected:
            return {"status": "connected", "error": None, "tool_count": len(self.tools)}
        return {"status": "disconnected", "error": None, "tool_count": 0}

    def set_failed(self, name, error):
        self._failed[name] = error
        self.connected.discard(name)

    def is_connected(self, name):
        return name in self.connected

    async def connect(self, name, config):
        self.connected.add(name)
        return {"ok": True, "error": None}

    async def disconnect(self, name):
        self.disconnect_calls.append(name)
        self.connected.discard(name)
        return {"ok": True, "error": None}

    async def test_connection(self, name, config):
        return {"ok": True, "error": None, "tools": [t.name for t in self.tools]}

    async def list_tools(self, name):
        if self.list_tools_error is not None:
            raise self.list_tools_error
        return list(self.tools)

    async def call_tool(self, server, tool, args=None, timeout=None):
        return "ok"


# -- B1: corrupt / invalid config must never be silently destroyed --------

def test_corrupt_config_file_keeps_backup_and_refuses_load(config_path):
    config_path.write_text("{not valid json,,,", encoding="utf-8")
    original = config_path.read_bytes()

    registry = MCPRegistry(config_path=config_path)
    assert registry.list_servers() == {}
    assert getattr(registry, "config_load_error", None)

    backup = config_path.with_name(config_path.name + ".bak")
    assert backup.exists()
    assert backup.read_bytes() == original
    # The corrupt file itself is untouched.
    assert config_path.read_bytes() == original


def test_add_after_corrupt_load_does_not_destroy_corrupt_file(config_path):
    config_path.write_text("{not valid json,,,", encoding="utf-8")
    original = config_path.read_bytes()
    registry = MCPRegistry(config_path=config_path)

    with pytest.raises(MCPConfigError, match="corrupt"):
        registry.add_server("new", {"url": "http://127.0.0.1:9/mcp"})
    assert config_path.read_bytes() == original


def test_invalid_entry_kept_marked_invalid_and_preserved_on_save(config_path):
    raw = {
        "good": {"transport": "streamable_http", "url": "http://127.0.0.1:9/mcp"},
        "bad": {"transport": "streamable_http", "url": "http://a/b", "command": "npx"},
    }
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    registry = MCPRegistry(config_path=config_path)

    assert "good" in registry.list_servers()
    assert "bad" not in registry.list_servers()

    status = registry.list_servers_with_status()
    assert status["bad"]["status"] == "invalid"
    assert status["bad"]["error"]
    assert status["bad"]["tool_count"] == 0

    # Adding/removing a valid server must not write the invalid entry away.
    registry.add_server("other", {"transport": "streamable_http", "url": "http://127.0.0.1:8/mcp"})
    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["bad"] == raw["bad"]
    registry.remove_server_sync("other")
    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["bad"] == raw["bad"]


# -- S1: secrets must never be echoed to the UI ---------------------------

def test_list_servers_with_status_redacts_header_and_env_values(config_path):
    registry = MCPRegistry(config_path=config_path)
    registry.add_server(
        "fake",
        {
            "transport": "streamable_http",
            "url": "http://127.0.0.1:9/mcp",
            "headers": {"x-api-key": "secret-123", "Authorization": "Bearer hunter2"},
        },
    )
    status = registry.list_servers_with_status()["fake"]
    assert sorted(status["headers"]) == ["Authorization", "x-api-key"]
    assert "secret-123" not in json.dumps(status)
    assert "hunter2" not in json.dumps(status)


# -- S3: unvalidated inputSchema ------------------------------------------

async def test_convert_tool_falls_back_on_bad_input_schema(config_path):
    bad = SimpleNamespace(name="add", description="add", inputSchema={"type": "string"})
    missing = SimpleNamespace(name="sub", description="sub", inputSchema=None)
    good = SimpleNamespace(
        name="mul", description="mul",
        inputSchema={"type": "object", "properties": {"a": {"type": "integer"}}},
    )
    stub = _StubClient(tools=[bad, missing, good])
    registry = _registry_with_stub_client(config_path, stub)
    registry.add_server("fake", {"transport": "streamable_http", "url": "http://127.0.0.1:9/mcp"})
    result = await registry.connect_server("fake")
    assert result["ok"] is True, result

    by_name = {t.name: t for t in registry.get_mcp_tools()}
    assert by_name["fake__add"].input_schema == {"type": "object", "properties": {}}
    assert by_name["fake__sub"].input_schema == {"type": "object", "properties": {}}
    assert by_name["fake__mul"].input_schema == {
        "type": "object", "properties": {"a": {"type": "integer"}}
    }


# -- S4: _refresh_tools failure must not leave a session with no tools ----

async def test_connect_refresh_failure_disconnects_and_marks_failed(config_path):
    stub = _StubClient(tools=[], list_tools_error=MCPClientError("list_tools exploded"))
    registry = _registry_with_stub_client(config_path, stub)
    registry.add_server("fake", {"transport": "streamable_http", "url": "http://127.0.0.1:9/mcp"})

    result = await registry.connect_server("fake")
    assert result["ok"] is False
    assert "list_tools exploded" in result["error"]
    assert "fake" in stub.disconnect_calls
    assert not stub.is_connected("fake")
    assert registry.get_mcp_tools() == []
    status = registry.list_servers_with_status()["fake"]
    assert status["status"] == "failed"
    assert "list_tools exploded" in (status["error"] or "")


# -- S5: remove_server closes the live session ----------------------------

async def test_remove_server_closes_live_session(config_path, fake_url):
    registry = MCPRegistry(config_path=config_path)
    registry.add_server("fake", {"transport": "streamable_http", "url": fake_url, "timeout": 10})
    assert (await registry.connect_server("fake"))["ok"] is True
    assert registry._client.is_connected("fake")

    await registry.remove_server("fake")  # async: disconnects inside removal, same task
    assert not registry._client.is_connected("fake")
    assert "fake" not in registry.list_servers()
    assert registry.get_mcp_tools() == []


# -- S2: tool-call timeout is Tool.timeout_s (60s), not the server timeout -

async def test_tool_call_uses_tool_timeout_not_server_timeout(config_path, fake_url):
    from aether_engine.tools.registry import build_tool_registry

    registry = MCPRegistry(config_path=config_path)
    # Server connect timeout is deliberately short (5s).
    registry.add_server("fake", {"transport": "streamable_http", "url": fake_url, "timeout": 5})
    assert (await registry.connect_server("fake"))["ok"] is True

    seen = []
    client = registry._client
    original_call_tool = client.call_tool

    async def _spy(server, tool, args=None, timeout=None):
        seen.append(timeout)
        return await original_call_tool(server, tool, args, timeout=timeout)

    client.call_tool = _spy
    tools = registry.get_mcp_tools()
    assert tools[0].timeout_s == 60
    tool_registry = build_tool_registry(mcp_tools=tools)
    result = await tool_registry.execute("fake__add", {"a": 3, "b": 5}, approved=True)
    assert result.strip() == "8"
    assert seen == [60], f"call_tool got timeout={seen}, expected [60]"

    await registry.disconnect_server("fake")
