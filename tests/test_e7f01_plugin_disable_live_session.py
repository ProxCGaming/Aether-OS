"""E7-F01: plugin disable reports success while a correctly owned MCP
session remains live and callable.

ADR 0035 + E7-03: disabling a plugin must disconnect/remove its live
MCP servers, not merely save a flag. Pre-fix the seam was:

- the WS PLUGIN_TOGGLE_REQUEST branch called the synchronous
  ``PluginInstaller.toggle`` directly — unlike PLUGIN_UNINSTALL_REQUEST
  it never awaited a disconnect first;
- ``MCPRegistry.remove_server_sync`` safely REFUSES a connected server
  inside a running event loop (the session can only be closed by its
  owner task — E3-F03), but ``toggle`` caught that refusal, logged a
  warning ("teardown is best-effort") and proceeded to move the skills,
  refresh, persist ``enabled=False`` and report success.

Result (audit repro, checks/epic7-boundary-observations/
connected-disable.json): disable "succeeded" while the plugin's owned
stdio session stayed connected, its tool stayed in the inventory, and
the retained Tool object still executed ``add(3, 5) -> 8``.

These tests pin the fixed behaviour at the real seams:

- WS toggle-disable awaits disconnection (mirroring uninstall) BEFORE
  the flag is persisted; afterwards the server is gone from the
  registry, out of the tool inventory, and a retained Tool no longer
  executes.
- If disconnection cannot complete, the disable FAILS honestly:
  failure reply, ``enabled`` stays True (memory and disk), the session
  stays connected and callable, and the skills are NOT moved — the
  toggle's MCP teardown runs before any skill move, and a failed
  disable must leave a still-enabled plugin whole.
- The installer's synchronous path no longer swallows the registry
  refusal: called with a live session inside a running loop it raises
  before mutating anything.
- Controls: uninstall with a live session still disconnects cleanly;
  disabling a plugin with no live session works exactly as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import queue
import sys
import threading
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.mcp.registry import MCPRegistry
from aether_engine.plugins.installer import PluginInstaller

FAKE_STDIO = Path(__file__).parent / "fake_stdio_server.py"

PLUGIN = "E7F01Plugin"
SKILL = "e7f01-skill"
SERVER = "e7f01-server"
ADD_TOOL = f"{SERVER}__add"


def _make_plugin_source(base: Path) -> Path:
    source = base / "pkg-src"
    source.mkdir()
    manifest = {
        "name": PLUGIN,
        "version": "1.0",
        "skills": [{"name": SKILL, "file_path": "skill.md"}],
        "mcp_servers": [{"name": SERVER, "file_path": "server.json"}],
    }
    (source / "manifest.json").write_text(json.dumps(manifest))
    (source / "skill.md").write_text("e7f01 skill body")
    (source / "server.json").write_text(json.dumps({
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(FAKE_STDIO)],
        "timeout": 10,
    }))
    return source


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # Fixture law: the audit logger's path must be repointed into tmp
    # BEFORE initialize_auth() — the process-global logger otherwise
    # keeps a dangling path from an earlier suite.
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    engine_state.initialize_auth()
    monkeypatch.setattr(
        "aether_engine.memory.session_store.DB_PATH", tmp_path / "test_memory.db"
    )
    # Isolated data home: install()/toggle() resolve the skills root
    # from Path.home() at call time.
    monkeypatch.setenv("HOME", str(tmp_path))

    installer = PluginInstaller(tmp_path / "plugins")
    original_installer = engine_state.plugin_installer
    engine_state.plugin_installer = installer

    registry = MCPRegistry(config_path=tmp_path / "mcp_servers.json")
    monkeypatch.setattr(
        "aether_engine.mcp.registry.GLOBAL_MCP_REGISTRY", registry
    )

    source = _make_plugin_source(tmp_path)
    yield _Env(tmp_path, installer, registry, source)

    engine_state.plugin_installer = original_installer
    with contextlib.suppress(Exception):
        from aether_engine.routes import websocket as ws_routes

        ws_routes.pending_plugin_installs.clear()


class _Env:
    def __init__(self, tmp_path: Path, installer, registry, source):
        self.tmp_path = tmp_path
        self.installer = installer
        self.registry = registry
        self.source = source

    @property
    def skills_root(self) -> Path:
        return self.tmp_path / ".aether" / "skills"

    def persisted_enabled(self) -> bool:
        fresh = PluginInstaller(self.tmp_path / "plugins")
        return fresh.get_all()[PLUGIN]["enabled"]


def _recv(ws, timeout=30.0) -> Event:
    """Receive one Event, failing (never hanging) if the server is quiet."""
    q: queue.Queue = queue.Queue()

    def _do():
        try:
            q.put(("ok", ws.receive_text()))
        except Exception as e:  # noqa: BLE001 — relayed to the test thread
            q.put(("err", e))

    threading.Thread(target=_do, daemon=True).start()
    try:
        kind, value = q.get(timeout=timeout)
    except queue.Empty:
        raise AssertionError(
            f"No WebSocket message within {timeout}s — the session was "
            "probably dropped by an unhandled handler error."
        ) from None
    if kind == "err":
        raise value
    return Event.from_json(value)


def _send(ws, type_, payload=None, request_id="r1") -> Event:
    ws.send_text(Event(
        type=type_, request_id=request_id, payload=payload or {}
    ).to_json())
    return _recv(ws)


def _connect_ws(client):
    # The app lifespan regenerates engine_state.auth_token — read fresh.
    return client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"},
    )


def _install_over_ws(ws, env: _Env):
    resp = _send(ws, EventType.PLUGIN_INSTALL_REQUEST,
                 {"source": str(env.source)}, request_id="install-1")
    assert resp.type == EventType.PLUGIN_APPROVAL_REQUEST, resp
    resp = _send(ws, EventType.PLUGIN_APPROVAL_GRANTED, request_id="install-1")
    assert resp.type == EventType.PLUGIN_INSTALL_RESPONSE, resp
    assert resp.payload["success"] is True, resp.payload
    assert SERVER in env.registry.list_servers()
    assert (env.skills_root / SKILL / "SKILL.md").exists()


def _connect_server_over_ws(ws, env: _Env):
    resp = _send(ws, EventType.MCP_SERVER_CONNECT_REQUEST, {"name": SERVER})
    assert resp.type == EventType.MCP_SERVER_CONNECT_RESPONSE, resp
    assert resp.payload["success"] is True, resp.payload
    assert resp.payload["status"] == "connected", resp.payload
    assert env.registry.get_server_status(SERVER)["status"] == "connected"
    tools = env.registry.get_mcp_tools()
    assert ADD_TOOL in [t.name for t in tools]
    return next(t for t in tools if t.name == ADD_TOOL)


def _cleanup(env: _Env, client=None):
    with contextlib.suppress(Exception):
        if client is not None:
            client.portal.call(env.registry.disconnect_server, SERVER)
    with contextlib.suppress(Exception):
        env.registry.remove_server_sync(SERVER)


# ---------------------------------------------------------------------------
# The audit repro: disable over the real WS toggle path
# ---------------------------------------------------------------------------


def test_disable_disconnects_live_session_before_persisting(env):
    with TestClient(app) as client, _connect_ws(client) as ws:
        hello = _recv(ws)
        assert hello.type == EventType.HELLO
        _install_over_ws(ws, env)
        add_tool = _connect_server_over_ws(ws, env)

        # Sanity: the tool genuinely executes before disable.
        out = client.portal.call(lambda: add_tool.execute_fn(a=3, b=5))
        assert out.strip() == "8"

        resp = _send(ws, EventType.PLUGIN_TOGGLE_REQUEST,
                     {"name": PLUGIN, "enabled": False})
        try:
            assert resp.type == EventType.PLUGIN_TOGGLE_RESPONSE, resp
            assert resp.payload["success"] is True, resp.payload

            # The flag persisted — and this time it is true.
            assert env.installer.get_all()[PLUGIN]["enabled"] is False
            assert env.persisted_enabled() is False

            # The session is actually gone: config removed, not
            # connected, tool out of a fresh inventory.
            assert SERVER not in env.registry.list_servers()
            assert env.registry.get_server_status(SERVER)["status"] != "connected"
            assert ADD_TOOL not in [
                t.name for t in env.registry.get_mcp_tools()
            ]

            # The retained Tool object no longer executes: converted
            # MCP tools fail gracefully (an error string, never a
            # raise), so post-disable the call must come back as the
            # not-connected error — pre-fix it returned "8".
            out = client.portal.call(lambda: add_tool.execute_fn(a=3, b=5))
            assert out.strip() != "8", out
            assert "not connected" in out, out

            # Skills behaviour preserved: moved into the holding area.
            assert not (env.skills_root / SKILL).exists()
            assert (
                env.skills_root / ".disabled-by-plugin" / PLUGIN / SKILL
                / "SKILL.md"
            ).exists()
        finally:
            _cleanup(env, client)


def test_disable_fails_honestly_when_disconnect_cannot_complete(env, monkeypatch):
    with TestClient(app) as client, _connect_ws(client) as ws:
        _recv(ws)  # HELLO
        _install_over_ws(ws, env)
        add_tool = _connect_server_over_ws(ws, env)

        real_disconnect = env.registry.disconnect_server

        async def _boom(name):
            raise RuntimeError("scripted disconnect failure")

        monkeypatch.setattr(env.registry, "disconnect_server", _boom)
        try:
            resp = _send(ws, EventType.PLUGIN_TOGGLE_REQUEST,
                         {"name": PLUGIN, "enabled": False})
            assert resp.type == EventType.PLUGIN_TOGGLE_RESPONSE, resp
            assert resp.payload["success"] is False, resp.payload
            assert "scripted disconnect failure" in resp.payload["error"]

            # No half-state: still enabled in memory AND on disk,
            # session untouched and still callable, skills NOT moved
            # for a plugin that stays enabled.
            assert env.installer.get_all()[PLUGIN]["enabled"] is True
            assert env.persisted_enabled() is True
            assert env.registry.get_server_status(SERVER)["status"] == "connected"
            assert (env.skills_root / SKILL / "SKILL.md").exists()
            assert not (env.skills_root / ".disabled-by-plugin").exists()
            out = client.portal.call(lambda: add_tool.execute_fn(a=3, b=5))
            assert out.strip() == "8"
        finally:
            monkeypatch.setattr(env.registry, "disconnect_server", real_disconnect)
            _cleanup(env, client)


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------


def test_uninstall_with_live_session_still_disconnects(env):
    with TestClient(app) as client, _connect_ws(client) as ws:
        _recv(ws)  # HELLO
        _install_over_ws(ws, env)
        _connect_server_over_ws(ws, env)

        resp = _send(ws, EventType.PLUGIN_UNINSTALL_REQUEST, {"name": PLUGIN})
        try:
            assert resp.type == EventType.PLUGIN_UNINSTALL_RESPONSE, resp
            assert resp.payload["success"] is True, resp.payload
            assert PLUGIN not in env.installer.get_all()
            assert SERVER not in env.registry.list_servers()
            assert env.registry.get_server_status(SERVER)["status"] != "connected"
        finally:
            _cleanup(env, client)


def test_disable_without_live_session_still_works(env):
    with TestClient(app) as client, _connect_ws(client) as ws:
        _recv(ws)  # HELLO
        _install_over_ws(ws, env)

        resp = _send(ws, EventType.PLUGIN_TOGGLE_REQUEST,
                     {"name": PLUGIN, "enabled": False})
        assert resp.payload["success"] is True, resp.payload
        assert env.persisted_enabled() is False
        assert SERVER not in env.registry.list_servers()
        assert not (env.skills_root / SKILL).exists()

        resp = _send(ws, EventType.PLUGIN_TOGGLE_REQUEST,
                     {"name": PLUGIN, "enabled": True})
        try:
            assert resp.payload["success"] is True, resp.payload
            assert env.persisted_enabled() is True
            assert SERVER in env.registry.list_servers()
            assert (env.skills_root / SKILL / "SKILL.md").exists()
        finally:
            _cleanup(env, client)


# ---------------------------------------------------------------------------
# Installer seam: the registry refusal must surface, not be swallowed
# ---------------------------------------------------------------------------


async def test_sync_toggle_refusal_is_not_swallowed(env):
    env.installer.install(env.installer.prepare_install(str(env.source)))
    assert (await env.registry.connect_server(SERVER))["ok"] is True
    try:
        # Inside a running loop with a live session, remove_server_sync
        # refuses — toggle must raise BEFORE mutating anything rather
        # than warn-and-continue into a persisted "disabled" lie.
        from aether_engine.mcp.registry import MCPConfigError

        with pytest.raises(MCPConfigError):
            env.installer.toggle(PLUGIN, False)

        assert env.installer.get_all()[PLUGIN]["enabled"] is True
        assert env.persisted_enabled() is True
        assert (env.skills_root / SKILL / "SKILL.md").exists()
        assert not (env.skills_root / ".disabled-by-plugin").exists()
        assert env.registry.get_server_status(SERVER)["status"] == "connected"

        # After an awaited disconnect (the WS handler's job), the same
        # toggle completes for real.
        await env.registry.disconnect_server(SERVER)
        env.installer.toggle(PLUGIN, False)
        assert env.persisted_enabled() is False
        assert SERVER not in env.registry.list_servers()
    finally:
        with contextlib.suppress(Exception):
            await env.registry.disconnect_server(SERVER)
        _cleanup(env)
        # Owner tasks park inside this test's loop; stop them so the
        # session/subprocess do not outlive it.
        with contextlib.suppress(Exception):
            await env.registry.stop_auto_connect()
        await asyncio.sleep(0)
