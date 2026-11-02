"""E7-F02: successful plugin install/uninstall leaves the live skill
index and cached instructions stale.

Install copies skill files and uninstall deletes them, but neither
updated ``GLOBAL_SKILL_MANAGER`` — and the WS branches that call them
did not refresh either. Only ``toggle()`` refreshed. Compounding it,
``SkillManager.load_skill_content`` serves the content cache before
consulting disk, so a once-cached body outlived its skill directory.

Audit repro (checks/epic7-boundary-observations/
install-uninstall-index.json):

- a real prepare -> approval -> successful fresh install left an
  already-live manager with NO catalogue entry / slash / body, while
  a fresh manager loaded the new skill fine;
- after an explicit refresh + cache load, a successful uninstall
  deleted the file but the live manager still resolved the deleted
  skill's slash, advertised it, and returned its cached body, while
  a fresh post-uninstall manager returned empty.

These tests pin the fixed behaviour at the real seams: the live
manager IS the module-global ``GLOBAL_SKILL_MANAGER`` (a tmp-rooted
instance patched in, the E6-F04 pattern), driven through the real
installer and the real WS approval flow. The fix places the
invalidation in the installer — exactly where toggle's refresh
already lives — after install's commit point and after uninstall's
registry save, so a failed operation leaves the live index exactly
as it was.
"""

from __future__ import annotations

import contextlib
import json
import queue
import threading
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import aether_engine.mcp.registry as reg_module
import aether_engine.skills.manager as skills_module
from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.mcp.registry import MCPRegistry
from aether_engine.plugins.installer import PluginInstaller
from aether_engine.skills.manager import SkillManager

PLUGIN = "IndexPlugin"
SKILL = "idx-skill"
SLASH = f"/{SKILL}"
BODY = "SYNTHETIC_INDEX_BODY"
KEEPER = "keeper-skill"
KEEPER_BODY = "KEEPER_BODY"


def _skill_md(name: str, body: str) -> str:
    return (
        f"---\nname: {name}\ndescription: {name} description\n---\n\n{body}\n"
    )


def _make_plugin_source(base: Path) -> Path:
    source = base / "pkg-src"
    source.mkdir()
    manifest = {
        "name": PLUGIN,
        "version": "1.0",
        "skills": [{"name": SKILL, "file_path": "skill.md"}],
    }
    (source / "manifest.json").write_text(json.dumps(manifest))
    (source / "skill.md").write_text(_skill_md(SKILL, BODY))
    return source


def _fresh_manager(skills_root: Path) -> SkillManager:
    return SkillManager(global_dir=skills_root, workspace_dirs=[])


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Installer seam: tmp home, tmp MCP registry, tmp-rooted live
    skill manager patched in as the module global BEFORE any install
    (the audit's "already-live manager")."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        reg_module, "GLOBAL_MCP_REGISTRY",
        MCPRegistry(config_path=tmp_path / "mcp_servers.json"),
    )
    skills_root = tmp_path / ".aether" / "skills"
    # An unrelated, hand-placed skill the plugin lifecycle never owns.
    keeper_dir = skills_root / KEEPER
    keeper_dir.mkdir(parents=True)
    (keeper_dir / "SKILL.md").write_text(_skill_md(KEEPER, KEEPER_BODY))

    live = SkillManager(global_dir=skills_root, workspace_dirs=[])
    monkeypatch.setattr(skills_module, "GLOBAL_SKILL_MANAGER", live)

    installer = PluginInstaller(tmp_path / "plugins")
    source = _make_plugin_source(tmp_path)
    return _Env(tmp_path, installer, live, skills_root, source)


class _Env:
    def __init__(self, tmp_path, installer, live, skills_root, source):
        self.tmp_path = tmp_path
        self.installer = installer
        self.live = live
        self.skills_root = skills_root
        self.source = source

    def install(self):
        # prepare_install IS the approval payload; install() is what
        # the PLUGIN_APPROVAL_GRANTED branch runs.
        self.installer.install(self.installer.prepare_install(str(self.source)))


# ---------------------------------------------------------------------------
# Install direction: the live manager must see the new skill
# ---------------------------------------------------------------------------


def test_install_makes_skill_visible_to_live_manager(env):
    live = env.live
    # Pre-conditions: the already-live manager predates the install
    # and knows nothing of the plugin's skill; the keeper is served.
    assert live.resolve_slash_command(SLASH) is None
    assert SKILL not in live.catalogue()
    assert live.load_skill_content(SKILL) == ""
    assert KEEPER in live.catalogue()

    env.install()

    # Known-good acceptance is unchanged: the file landed on disk.
    assert (env.skills_root / SKILL / "SKILL.md").exists()

    # The live manager now advertises, resolves and serves the skill
    # — no restart, no manual refresh.
    assert SKILL in live.catalogue()
    assert live.resolve_slash_command(SLASH) is not None
    assert BODY in live.load_skill_content(SKILL)
    assert SKILL in {s.name for s in live.list_all()}

    # A fresh manager agrees (pre-fix it was the ONLY one that did).
    fresh = _fresh_manager(env.skills_root)
    assert SKILL in fresh.catalogue()
    assert BODY in fresh.load_skill_content(SKILL)

    # The unrelated skill is undisturbed.
    assert live.resolve_slash_command(f"/{KEEPER}") is not None
    assert KEEPER_BODY in live.load_skill_content(KEEPER)


# ---------------------------------------------------------------------------
# Uninstall direction: the live manager must forget the skill,
# including its cached body
# ---------------------------------------------------------------------------


def test_uninstall_purges_live_index_and_cached_body(env):
    live = env.live
    env.install()

    # The audit's in-between step: an explicit refresh + a content
    # load, so the body sits in the live manager's cache.
    live.refresh()
    assert BODY in live.load_skill_content(SKILL)
    assert SKILL in live._content_cache
    assert KEEPER_BODY in live.load_skill_content(KEEPER)

    env.installer.uninstall(PLUGIN)

    # Known-good acceptance is unchanged: the file is gone from disk.
    assert not (env.skills_root / SKILL).exists()

    # The live manager no longer advertises, resolves or serves the
    # removed skill — above all, NOT its cached body.
    assert live.resolve_slash_command(SLASH) is None
    assert SKILL not in live.catalogue()
    assert SKILL not in {s.name for s in live.list_all()}
    assert live.load_skill_content(SKILL) == ""

    # A fresh manager agrees it is empty.
    fresh = _fresh_manager(env.skills_root)
    assert fresh.resolve_slash_command(SLASH) is None
    assert fresh.load_skill_content(SKILL) == ""

    # The unrelated skill's body keeps serving.
    assert live.resolve_slash_command(f"/{KEEPER}") is not None
    assert KEEPER_BODY in live.load_skill_content(KEEPER)


# ---------------------------------------------------------------------------
# Controls: failure leaves the index as it was; toggle still works
# ---------------------------------------------------------------------------


def test_failed_install_leaves_live_index_exactly_as_before(env, monkeypatch):
    live = env.live
    env.install()
    live.refresh()
    assert BODY in live.load_skill_content(SKILL)
    catalogue_before = live.catalogue()

    # A second plugin whose install fails at the registry-save commit
    # point: mutations roll back, and no refresh may leak the aborted
    # skill into (or the existing skill out of) the live index.
    bad_source = env.tmp_path / "bad-src"
    bad_source.mkdir()
    (bad_source / "manifest.json").write_text(json.dumps({
        "name": "BadPlugin",
        "version": "1.0",
        "skills": [{"name": "bad-skill", "file_path": "skill.md"}],
    }))
    (bad_source / "skill.md").write_text(_skill_md("bad-skill", "BAD_BODY"))

    real_save = env.installer._save_registry

    def boom():
        raise OSError("scripted registry save failure")

    monkeypatch.setattr(env.installer, "_save_registry", boom)
    try:
        with pytest.raises(RuntimeError):
            env.installer.install(env.installer.prepare_install(str(bad_source)))
    finally:
        monkeypatch.setattr(env.installer, "_save_registry", real_save)

    assert "BadPlugin" not in env.installer.get_all()
    assert not (env.skills_root / "bad-skill").exists()
    # The live index is byte-for-byte the pre-attempt one, cached
    # body included.
    assert live.catalogue() == catalogue_before
    assert live.resolve_slash_command(SLASH) is not None
    assert BODY in live.load_skill_content(SKILL)
    assert live.resolve_slash_command("/bad-skill") is None
    assert live.load_skill_content("bad-skill") == ""


def test_toggle_lifecycle_still_drives_live_manager(env):
    live = env.live
    env.install()
    assert live.resolve_slash_command(SLASH) is not None

    env.installer.toggle(PLUGIN, False)
    assert live.resolve_slash_command(SLASH) is None
    assert live.load_skill_content(SKILL) == ""

    env.installer.toggle(PLUGIN, True)
    assert live.resolve_slash_command(SLASH) is not None
    assert BODY in live.load_skill_content(SKILL)


# ---------------------------------------------------------------------------
# WS end-to-end: approval-granted install / uninstall over the socket
# ---------------------------------------------------------------------------


@pytest.fixture()
def ws_env(tmp_path, monkeypatch):
    # Fixture law: audit log path into tmp BEFORE initialize_auth().
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    engine_state.initialize_auth()
    monkeypatch.setattr(
        "aether_engine.memory.session_store.DB_PATH", tmp_path / "test_memory.db"
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    installer = PluginInstaller(tmp_path / "plugins")
    original_installer = engine_state.plugin_installer
    engine_state.plugin_installer = installer

    registry = MCPRegistry(config_path=tmp_path / "mcp_servers.json")
    monkeypatch.setattr(reg_module, "GLOBAL_MCP_REGISTRY", registry)

    skills_root = tmp_path / ".aether" / "skills"
    live = SkillManager(global_dir=skills_root, workspace_dirs=[])
    monkeypatch.setattr(skills_module, "GLOBAL_SKILL_MANAGER", live)

    source = _make_plugin_source(tmp_path)
    yield _Env(tmp_path, installer, live, skills_root, source)

    engine_state.plugin_installer = original_installer
    with contextlib.suppress(Exception):
        from aether_engine.routes import websocket as ws_routes

        ws_routes.pending_plugin_installs.clear()


def _recv(ws, timeout=30.0) -> Event:
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


def test_ws_install_then_uninstall_updates_live_manager(ws_env):
    live = ws_env.live
    with TestClient(app) as client, client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"},
    ) as ws:
        hello = _recv(ws)
        assert hello.type == EventType.HELLO
        assert live.resolve_slash_command(SLASH) is None

        resp = _send(ws, EventType.PLUGIN_INSTALL_REQUEST,
                     {"source": str(ws_env.source)}, request_id="install-1")
        assert resp.type == EventType.PLUGIN_APPROVAL_REQUEST, resp
        resp = _send(ws, EventType.PLUGIN_APPROVAL_GRANTED,
                     request_id="install-1")
        assert resp.type == EventType.PLUGIN_INSTALL_RESPONSE, resp
        assert resp.payload["success"] is True, resp.payload

        # The approval-granted install is visible to the live manager.
        assert SKILL in live.catalogue()
        assert live.resolve_slash_command(SLASH) is not None
        assert BODY in live.load_skill_content(SKILL)

        # Cache the body, then uninstall over the socket: the live
        # manager must forget the skill AND its cached body.
        resp = _send(ws, EventType.PLUGIN_UNINSTALL_REQUEST, {"name": PLUGIN})
        assert resp.type == EventType.PLUGIN_UNINSTALL_RESPONSE, resp
        assert resp.payload["success"] is True, resp.payload

        assert live.resolve_slash_command(SLASH) is None
        assert SKILL not in live.catalogue()
        assert live.load_skill_content(SKILL) == ""
