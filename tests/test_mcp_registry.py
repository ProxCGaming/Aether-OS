"""Issue 001: validated MCP server config + real get_mcp_tools().

TDD red first: fails until aether_engine/mcp/registry.py is extended.
"""
import pytest

from aether_engine.mcp.registry import (
    MCP_TOOL_BACKSTOP_TIMEOUT_S,
    MCPConfigError,
    MCPRegistry,
    validate_mcp_server_config,
)
from aether_engine.tools.registry import ToolRisk
from tests.fake_mcp_http import start_fake_http_server


@pytest.fixture(scope="module")
def fake_url():
    url, stop = start_fake_http_server()
    yield url
    stop()


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    # Never touch the real ~/.aether/mcp_servers.json.
    import aether_engine.mcp.registry as reg_module

    monkeypatch.setattr(reg_module, "_MCP_CONFIG_PATH", tmp_path / "mcp_servers.json")
    return MCPRegistry()


def test_validate_rejects_both_transports():
    with pytest.raises(MCPConfigError, match="exactly one"):
        validate_mcp_server_config(
            "x", {"transport": "streamable_http", "url": "http://a/b", "command": "npx"}
        )


def test_validate_rejects_neither_transport():
    with pytest.raises(MCPConfigError):
        validate_mcp_server_config("x", {"transport": "streamable_http"})


def test_validate_rejects_bad_name():
    with pytest.raises(MCPConfigError):
        validate_mcp_server_config("bad name!", {"transport": "stdio", "command": "npx"})


def test_validate_rejects_bad_timeout_and_headers():
    with pytest.raises(MCPConfigError):
        validate_mcp_server_config(
            "x", {"transport": "stdio", "command": "npx", "timeout": 9999}
        )
    with pytest.raises(MCPConfigError):
        validate_mcp_server_config(
            "x",
            {"transport": "streamable_http", "url": "http://a/b", "headers": {"k": 1}},
        )


def test_validate_normalises_defaults():
    cfg = validate_mcp_server_config("x", {"transport": "stdio", "command": "npx"})
    assert cfg["timeout"] == 30
    assert cfg["enabled"] is True
    assert cfg["auto_connect"] is True
    assert cfg["args"] == []
    # Transport inferred from the shape when not given (standard mcpServers).
    cfg2 = validate_mcp_server_config("y", {"url": "http://a/b"})
    assert cfg2["transport"] == "streamable_http"


def test_add_invalid_server_raises_and_does_not_save(registry):
    with pytest.raises(MCPConfigError):
        registry.add_server("bad", {"transport": "streamable_http", "command": "npx", "url": "http://a/b"})
    assert "bad" not in registry.list_servers()


def test_get_mcp_tools_empty_before_connect(registry):
    assert registry.get_mcp_tools() == []


async def test_connect_fake_http_server_exposes_prefixed_tool(registry, fake_url):
    registry.add_server(
        "fake", {"transport": "streamable_http", "url": fake_url, "timeout": 10}
    )
    result = await registry.connect_server("fake")
    assert result["ok"] is True, result

    tools = registry.get_mcp_tools()
    assert [t.name for t in tools] == ["fake__add"]
    tool = tools[0]
    assert tool.risk == ToolRisk.EXECUTE
    assert tool.source == "mcp:fake"
    # The Tool's timeout_s is the registry backstop: the 60s call budget
    # plus the client's post-cancel grace + slack, so the backstop can
    # never pre-empt call_tool's own graceful failure path (Wave 2).
    assert tool.timeout_s == MCP_TOOL_BACKSTOP_TIMEOUT_S

    status = registry.list_servers_with_status()["fake"]
    assert status["status"] == "connected"
    assert status["tool_count"] == 1

    await registry.disconnect_server("fake")
    assert registry.list_servers_with_status()["fake"]["status"] == "disconnected"


async def test_test_connection_lists_tools_without_connecting(registry, fake_url):
    registry.add_server(
        "fake", {"transport": "streamable_http", "url": fake_url, "timeout": 10}
    )
    result = await registry.test_connection("fake")
    assert result == {"ok": True, "error": None, "tools": ["add"]}
    assert registry.get_mcp_tools() == []


async def test_connect_fills_tool_cache_from_connect_listing(registry):
    """The client hands the tools it listed while connecting to the
    registry (the "tools" key), and the registry fills its cache from
    them — no second list_tools round-trip, and no window where the
    server already reads connected while get_mcp_tools() still offers
    nothing. The hand-over is consumed: callers keep receiving exactly
    {"ok", "error"}."""
    from types import SimpleNamespace

    class _HandoverClient:
        def __init__(self):
            self.list_tools_calls = 0
            self.connected = set()

        async def connect(self, name, config):
            self.connected.add(name)
            return {"ok": True, "error": None, "tools": [
                SimpleNamespace(
                    name="add", description="Add two numbers",
                    inputSchema={"type": "object", "properties": {}},
                ),
            ]}

        async def connect_owned(self, name, config):
            return await self.connect(name, config)

        async def list_tools(self, name):
            self.list_tools_calls += 1
            raise AssertionError("registry listed the tools a second time")

        def get_status(self, name):
            if name in self.connected:
                return {"status": "connected", "error": None, "tool_count": 1}
            return {"status": "disconnected", "error": None, "tool_count": 0}

        def is_connected(self, name):
            return name in self.connected

        async def disconnect(self, name):
            self.connected.discard(name)
            return {"ok": True, "error": None}

        def set_failed(self, name, error):
            raise AssertionError("a handed-over connect must not be marked failed")

    registry.add_server(
        "fake", {"transport": "streamable_http", "url": "http://127.0.0.1:9/mcp"}
    )
    client = _HandoverClient()
    registry._client = client
    result = await registry.connect_server("fake")
    assert result == {"ok": True, "error": None}
    assert client.list_tools_calls == 0
    assert [t.name for t in registry.get_mcp_tools()] == ["fake__add"]
