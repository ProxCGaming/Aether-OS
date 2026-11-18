"""MCP add confirmation: the ``confirmed`` marker + auto-connect gate.

An MCP server is a command (or remote endpoint) the engine runs.
Adding one now requires the user to confirm a card naming what
will run; the confirmation is persisted as ``confirmed`` on the
server config, and startup auto-connect honours only confirmed
servers. Configs written before the marker existed load as
unconfirmed — they are surfaced as awaiting confirmation, never
connected silently at startup. Manual Connect is the user's
explicit act and is not gated.
"""
import asyncio
import json
import time

import pytest

from aether_engine.mcp.registry import (
    MCPConfigError,
    MCPRegistry,
    validate_mcp_server_config,
)
from tests.fake_mcp_http import start_fake_http_server


@pytest.fixture(scope="module")
def fake_url():
    url, stop = start_fake_http_server()
    yield url
    stop()


def _registry(tmp_path):
    return MCPRegistry(config_path=tmp_path / "mcp_servers.json")


def _http_config(url, **overrides):
    cfg = {"transport": "streamable_http", "url": url, "timeout": 5}
    cfg.update(overrides)
    return cfg


async def _wait_for(predicate, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return predicate()


def test_validate_confirmed_defaults_false_and_preserves_true():
    cfg = validate_mcp_server_config("x", {"transport": "stdio", "command": "npx"})
    assert cfg["confirmed"] is False
    cfg2 = validate_mcp_server_config(
        "y", {"transport": "stdio", "command": "npx", "confirmed": True})
    assert cfg2["confirmed"] is True


def test_confirmed_marker_persists_across_reload(tmp_path):
    registry = _registry(tmp_path)
    registry.add_server("ok", _http_config("http://a/b", confirmed=True))
    registry.add_server("pending", _http_config("http://a/c"))
    reloaded = _registry(tmp_path)
    assert reloaded.list_servers()["ok"]["confirmed"] is True
    assert reloaded.list_servers()["pending"]["confirmed"] is False
    # The list answer the panel renders carries the marker too.
    assert reloaded.list_servers_with_status()["pending"]["confirmed"] is False


def test_legacy_config_without_marker_loads_unconfirmed(tmp_path):
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps({
        "legacy": {"transport": "streamable_http", "url": "http://a/b"},
    }))
    registry = MCPRegistry(config_path=path)
    assert registry.list_servers()["legacy"]["confirmed"] is False


async def test_auto_connect_skips_unconfirmed_connects_confirmed(tmp_path, fake_url):
    registry = _registry(tmp_path)
    registry.add_server("confirmed-one", _http_config(fake_url, confirmed=True))
    registry.add_server("unconfirmed-one", _http_config(fake_url))

    started = registry.start_auto_connect()
    assert started == ["confirmed-one"]

    assert await _wait_for(
        lambda: registry._client.get_status("confirmed-one")["status"] == "connected"
    ), registry._client.get_status("confirmed-one")
    # The unconfirmed server was never attempted.
    assert registry._client.get_status("unconfirmed-one")["status"] == "disconnected"
    await registry.stop_auto_connect()


async def test_update_server_records_confirmation(tmp_path):
    registry = _registry(tmp_path)
    registry.add_server("srv", _http_config("http://a/b"))
    assert registry.list_servers()["srv"]["confirmed"] is False

    updated = await registry.update_server("srv", confirmed=True)
    assert updated["confirmed"] is True
    reloaded = _registry(tmp_path)
    assert reloaded.list_servers()["srv"]["confirmed"] is True

    with pytest.raises(MCPConfigError):
        await registry.update_server("srv", confirmed="yes")
    with pytest.raises(MCPConfigError):
        await registry.update_server("ghost", confirmed=True)


async def test_update_result_forwards_confirmed_to_the_registry(
    tmp_path, monkeypatch
):
    """The websocket update path carries the panel's confirmation
    to update_server — without this hop, confirming an existing
    server in place would silently do nothing."""
    import contextlib

    import aether_engine.mcp.registry as registry_module
    from aether_common.contracts import EventType
    from aether_engine.routes.ws_mcp_plugins import (
        send_mcp_server_update_result,
    )

    registry = _registry(tmp_path)
    registry.add_server("srv", _http_config("http://a/b"))
    monkeypatch.setattr(registry_module, "GLOBAL_MCP_REGISTRY", registry)

    class FakeConn:
        def __init__(self):
            self.sent = []

        @contextlib.asynccontextmanager
        async def mcp_server_lock(self, name):
            yield

        async def send_event(self, req_id, type_, payload, failure_message):
            self.sent.append((type_, payload))

    conn = FakeConn()
    await send_mcp_server_update_result(
        conn, "req-1", "srv", None, None, confirmed_=True)
    assert registry.list_servers()["srv"]["confirmed"] is True
    type_, payload = conn.sent[-1]
    assert type_ == EventType.MCP_SERVER_UPDATE_RESPONSE
    assert payload["success"] is True
    assert payload["config"]["confirmed"] is True
