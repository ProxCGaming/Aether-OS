"""E7-F05: Settings plugin toggle/uninstall omit the plugin identity
and cannot act on real installed entries.

The identity contract was broken end to end:

- installed records carry the plugin's name only under
  ``manifest.name`` — there is no top-level ``name``;
- the PLUGIN_LIST handler serialised ``list(registry.values())``,
  discarding the registry keys (which ARE the canonical identity:
  ``install()`` keys the registry by ``manifest.name``);
- DiagnosticsPanel displays ``plugin.manifest?.name`` but both its
  buttons sent ``name: plugin.name`` — undefined, so JSON serialisation
  omitted the field entirely;
- toggle with a missing name failed loudly enough on the engine side
  (``Plugin not found: None``) but the renderer ignored failure
  replies; uninstall with a missing/unknown name RETURNED SILENTLY in
  the installer while the WS branch still replied ``success: True`` —
  the plugin stayed installed and the UI believed it was gone.

These tests pin the fixed behaviour at the real seams:

- the LIST reply carries each plugin's canonical identity as a
  top-level ``name`` that round-trips into toggle/uninstall;
- a name-less or unknown-name uninstall replies ``success: False``
  and removes nothing — the silent success is gone, at the socket
  level and at the installer level (mirroring toggle's KeyError);
- a name-less toggle keeps replying failure (control);
- the identity taken from a real LIST reply drives a real
  disable/enable/uninstall round trip (controls).
"""

from __future__ import annotations

import contextlib
import json
import queue
import threading
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import aether_engine.skills.manager as skills_module
from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.mcp.registry import MCPRegistry
from aether_engine.plugins.installer import PluginInstaller
from aether_engine.skills.manager import SkillManager

PLUGIN = "IdentityPlugin"
SKILL = "identity-skill"


def _make_plugin_source(base: Path) -> Path:
    source = base / "pkg-src"
    source.mkdir()
    manifest = {
        "name": PLUGIN,
        "version": "1.0",
        "skills": [{"name": SKILL, "file_path": "skill.md"}],
    }
    (source / "manifest.json").write_text(json.dumps(manifest))
    (source / "skill.md").write_text(
        f"---\nname: {SKILL}\ndescription: {SKILL} description\n---\n\nbody\n"
    )
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
    # Hermetic live skill manager (the E7-F02 pattern): the install /
    # uninstall / toggle refreshes land on a tmp-rooted instance.
    live = SkillManager(
        global_dir=tmp_path / ".aether" / "skills", workspace_dirs=[]
    )
    monkeypatch.setattr(skills_module, "GLOBAL_SKILL_MANAGER", live)

    source = _make_plugin_source(tmp_path)
    yield _Env(tmp_path, installer, source)

    engine_state.plugin_installer = original_installer
    with contextlib.suppress(Exception):
        from aether_engine.routes import websocket as ws_routes

        ws_routes.pending_plugin_installs.clear()


class _Env:
    def __init__(self, tmp_path: Path, installer, source):
        self.tmp_path = tmp_path
        self.installer = installer
        self.source = source

    @property
    def plugin_dir(self) -> Path:
        return self.tmp_path / "plugins" / PLUGIN

    def persisted_names(self) -> list[str]:
        fresh = PluginInstaller(self.tmp_path / "plugins")
        return sorted(fresh.get_all())


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
    assert PLUGIN in env.installer.get_all()
    assert env.plugin_dir.exists()


def _list_over_ws(ws) -> list[dict]:
    resp = _send(ws, EventType.PLUGIN_LIST_REQUEST, request_id="list-1")
    assert resp.type == EventType.PLUGIN_LIST_RESPONSE, resp
    plugins = resp.payload["plugins"]
    assert isinstance(plugins, list), resp.payload
    return plugins


# ---------------------------------------------------------------------------
# The identity round-trip
# ---------------------------------------------------------------------------


def test_list_response_carries_the_sendable_identity(env):
    with TestClient(app) as client, _connect_ws(client) as ws:
        _recv(ws)  # HELLO
        _install_over_ws(ws, env)

        entries = _list_over_ws(ws)
        assert len(entries) == 1, entries
        entry = entries[0]
        # The record's own truth: the name lives under the manifest.
        assert entry["manifest"]["name"] == PLUGIN
        # The contract the panel round-trips: a top-level identity
        # equal to the registry key / manifest name. Pre-fix the
        # handler discarded the keys and this field did not exist —
        # which is exactly why the panel's buttons sent nothing.
        assert entry["name"] == PLUGIN


def test_identity_from_list_drives_toggle_and_uninstall(env):
    with TestClient(app) as client, _connect_ws(client) as ws:
        _recv(ws)  # HELLO
        _install_over_ws(ws, env)

        name = _list_over_ws(ws)[0]["name"]

        resp = _send(ws, EventType.PLUGIN_TOGGLE_REQUEST,
                     {"name": name, "enabled": False})
        assert resp.type == EventType.PLUGIN_TOGGLE_RESPONSE, resp
        assert resp.payload["success"] is True, resp.payload
        assert env.installer.get_all()[PLUGIN]["enabled"] is False

        resp = _send(ws, EventType.PLUGIN_TOGGLE_REQUEST,
                     {"name": name, "enabled": True})
        assert resp.payload["success"] is True, resp.payload
        assert env.installer.get_all()[PLUGIN]["enabled"] is True

        resp = _send(ws, EventType.PLUGIN_UNINSTALL_REQUEST, {"name": name})
        assert resp.type == EventType.PLUGIN_UNINSTALL_RESPONSE, resp
        assert resp.payload["success"] is True, resp.payload
        assert PLUGIN not in env.installer.get_all()
        assert env.persisted_names() == []
        assert not env.plugin_dir.exists()


# ---------------------------------------------------------------------------
# The silent-success lie: name-less / unknown uninstall
# ---------------------------------------------------------------------------


def test_nameless_uninstall_is_not_a_silent_success(env):
    """The payload the pre-fix panel actually sent: no ``name`` key at
    all (``plugin.name`` was undefined and JSON dropped it)."""
    with TestClient(app) as client, _connect_ws(client) as ws:
        _recv(ws)  # HELLO
        _install_over_ws(ws, env)

        resp = _send(ws, EventType.PLUGIN_UNINSTALL_REQUEST, {})
        assert resp.type == EventType.PLUGIN_UNINSTALL_RESPONSE, resp
        # Pre-fix: {"success": True, "name": None} while the plugin
        # stayed installed — the core lie of this finding.
        assert resp.payload["success"] is False, resp.payload
        assert resp.payload.get("error"), resp.payload
        assert PLUGIN in env.installer.get_all()
        assert env.persisted_names() == [PLUGIN]
        assert env.plugin_dir.exists()


def test_unknown_name_uninstall_fails_and_removes_nothing(env):
    with TestClient(app) as client, _connect_ws(client) as ws:
        _recv(ws)  # HELLO
        _install_over_ws(ws, env)

        resp = _send(ws, EventType.PLUGIN_UNINSTALL_REQUEST,
                     {"name": "NoSuchPlugin"})
        assert resp.type == EventType.PLUGIN_UNINSTALL_RESPONSE, resp
        assert resp.payload["success"] is False, resp.payload
        assert resp.payload.get("error"), resp.payload
        assert PLUGIN in env.installer.get_all()
        assert env.persisted_names() == [PLUGIN]
        assert env.plugin_dir.exists()


def test_installer_uninstall_unknown_name_raises(env):
    """The refusal lives in the installer, not only the WS branch —
    mirroring toggle()'s KeyError — so no caller can mistake a no-op
    uninstall for a removal."""
    env.installer.install(env.installer.prepare_install(str(env.source)))
    assert PLUGIN in env.installer.get_all()

    with pytest.raises(KeyError, match="Plugin not found"):
        env.installer.uninstall("NoSuchPlugin")
    with pytest.raises(KeyError, match="Plugin not found"):
        env.installer.uninstall(None)

    assert PLUGIN in env.installer.get_all()
    assert env.plugin_dir.exists()


# ---------------------------------------------------------------------------
# Controls: toggle's refusal was already loud — pin that it stays so
# ---------------------------------------------------------------------------


def test_nameless_and_unknown_toggle_reply_failure(env):
    with TestClient(app) as client, _connect_ws(client) as ws:
        _recv(ws)  # HELLO
        _install_over_ws(ws, env)

        resp = _send(ws, EventType.PLUGIN_TOGGLE_REQUEST, {"enabled": False})
        assert resp.type == EventType.PLUGIN_TOGGLE_RESPONSE, resp
        assert resp.payload["success"] is False, resp.payload
        assert "Plugin not found" in resp.payload["error"]

        resp = _send(ws, EventType.PLUGIN_TOGGLE_REQUEST,
                     {"name": "NoSuchPlugin", "enabled": False})
        assert resp.payload["success"] is False, resp.payload
        assert "Plugin not found" in resp.payload["error"]

        # Neither refusal disturbed the real entry.
        assert env.installer.get_all()[PLUGIN]["enabled"] is True
