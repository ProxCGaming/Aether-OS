"""Wave 5 (Lane B) — plugins + skills surfaces over the engine.

Pins, at the engine seams the Settings panels consume:

- the plugin prepare payload carries the approval card's
  per-server summaries (what a bundled server runs / connects to /
  is given — env and header NAMES only, never values);
- install records the source it came from, so the version state
  can be re-checked later;
- a registry record whose stored manifest no longer validates is
  a per-plugin load error (contract C7 data), while a healthy
  record has none;
- the update check re-prepares the recorded source and reports
  version/component drift truthfully: added components and changed
  MCP server configs are permission growth (PC-13); a changed
  skill body is drift, not growth; a plugin with no recorded
  source reports exactly that instead of a fabricated answer;
- the skills UI workflow is the CLI engine: search reads a named
  source (a github source answers with its honest unavailability),
  prepare returns the pin hash without writing anything, install
  refuses when the source changed after review, and the list
  annotation reports a locally edited pinned skill as drifted.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import aether_engine.mcp.registry as mcp_module
import aether_engine.routes.ws_skills_registry as ws_skills
import aether_engine.skills.manager as skills_module
from aether_common.contracts import Event, EventType
from aether_engine.mcp.registry import MCPRegistry
from aether_engine.plugins.installer import PluginInstaller
from aether_engine.routes import websocket as ws_routes
from aether_engine.routes.ws_mcp_plugins import (
    send_plugin_update_check_result,
)
from aether_engine.skills import skill_registry
from aether_engine.skills.lockfile import LOCKFILE_NAME, load_lockfile
from aether_engine.skills.manager import SkillManager
from aether_engine.state import engine_state

SECRET_VALUE = "sk-live-secret-value"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Isolated data home + registries (the E7-F02/F05 pattern)."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        mcp_module, "GLOBAL_MCP_REGISTRY",
        MCPRegistry(config_path=tmp_path / "mcp_servers.json"))
    live = SkillManager(
        global_dir=tmp_path / ".aether" / "skills", workspace_dirs=[])
    monkeypatch.setattr(skills_module, "GLOBAL_SKILL_MANAGER", live)
    return tmp_path


def _skill_md(name: str, body: str = "Body.") -> str:
    return f"---\nname: {name}\ndescription: {name} description\n---\n\n{body}\n"


def _plugin_source(
    base: Path,
    *,
    version: str = "1.0",
    skills: tuple[str, ...] = ("plug-skill",),
    mcp_command: str = "npx",
    skill_body: str = "Body.",
) -> Path:
    source = base / "pkg"
    source.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"name": "DemoPlugin", "version": version, "skills": []}
    for skill in skills:
        manifest["skills"].append(
            {"name": skill, "file_path": f"{skill}.md"})
        (source / f"{skill}.md").write_text(_skill_md(skill, skill_body))
    manifest["mcp_servers"] = [
        {"name": "demo-server", "file_path": "server.json"}]
    (source / "server.json").write_text(json.dumps({
        "command": mcp_command,
        "args": ["-y", "demo-mcp"],
        "env": {"DEMO_API_KEY": SECRET_VALUE},
    }))
    (source / "manifest.json").write_text(json.dumps(manifest))
    return source


def _install(installer: PluginInstaller, source: Path) -> None:
    installer.install(installer.prepare_install(str(source)))


# -- Prepare payload: the trust card's server summaries --------------------

def test_prepare_payload_summarises_servers_without_secret_values(home):
    installer = PluginInstaller(home / "plugins")
    prepared = installer.prepare_install(str(_plugin_source(home)))
    (summary,) = prepared["mcp_server_summaries"]
    assert summary["name"] == "demo-server"
    assert summary["runs"] == "npx -y demo-mcp"
    assert summary["env_keys"] == ["DEMO_API_KEY"]
    # The value is a secret: it must not ride the payload the UI
    # renders back (PC-12).
    assert SECRET_VALUE not in json.dumps(prepared)


def test_install_records_its_source(home):
    installer = PluginInstaller(home / "plugins")
    source = _plugin_source(home)
    _install(installer, source)
    entry = installer.get_all()["DemoPlugin"]
    assert entry["source"] == str(source)


# -- Per-plugin load errors -------------------------------------------------

def test_load_errors_flag_only_broken_records(home):
    installer = PluginInstaller(home / "plugins")
    installer._registry = {
        "Good": {"manifest": {"name": "Good", "version": "1.0"},
                 "enabled": True},
        "BadName": {"manifest": {"name": "../escape", "version": "1.0"},
                    "enabled": True},
        "NoManifest": {"enabled": True},
        "NotADict": "corrupt",
    }
    errors = installer.load_errors()
    assert "Good" not in errors
    assert set(errors) == {"BadName", "NoManifest", "NotADict"}
    assert all(isinstance(e, str) and e for e in errors.values())


# -- Update check (FF4-04 plugins half, PC-13) ------------------------------

def test_update_check_without_recorded_source_is_honest(home):
    installer = PluginInstaller(home / "plugins")
    installer._registry = {
        "Legacy": {"manifest": {"name": "Legacy", "version": "1.0"},
                   "enabled": True},
    }
    result = installer.check_for_update("Legacy")
    assert result["checked"] is False
    assert result["has_update"] is False
    assert "No install source was recorded" in result["error"]


def test_update_check_unknown_plugin(home):
    installer = PluginInstaller(home / "plugins")
    result = installer.check_for_update("Ghost")
    assert result["checked"] is False
    assert "not found" in result["error"]


def test_update_check_clean_when_source_unchanged(home):
    installer = PluginInstaller(home / "plugins")
    source = _plugin_source(home)
    _install(installer, source)
    result = installer.check_for_update("DemoPlugin")
    assert result["checked"] is True
    assert result["error"] is None
    assert result["installed_version"] == "1.0"
    assert result["latest_version"] == "1.0"
    assert result["has_update"] is False
    assert result["permissions_grew"] is False


def test_update_check_reports_growth(home):
    installer = PluginInstaller(home / "plugins")
    source = _plugin_source(home)
    _install(installer, source)
    # The publisher ships 1.1: a new skill and a different server
    # command — both grow what the bundle can do (PC-13).
    _plugin_source(
        home, version="1.1", skills=("plug-skill", "new-skill"),
        mcp_command="uvx")
    result = installer.check_for_update("DemoPlugin")
    assert result["checked"] is True
    assert result["latest_version"] == "1.1"
    assert result["has_update"] is True
    assert result["added_skills"] == ["new-skill"]
    assert result["changed_mcp_servers"] == ["demo-server"]
    assert result["permissions_grew"] is True


def test_update_check_skill_body_drift_is_not_growth(home):
    installer = PluginInstaller(home / "plugins")
    source = _plugin_source(home)
    _install(installer, source)
    _plugin_source(home, skill_body="Edited body.")
    result = installer.check_for_update("DemoPlugin")
    assert result["changed_skills"] == ["plug-skill"]
    assert result["has_update"] is True
    assert result["permissions_grew"] is False


def test_update_check_vanished_source_reports_error(home):
    installer = PluginInstaller(home / "plugins")
    source = _plugin_source(home)
    _install(installer, source)
    (source / "manifest.json").unlink()
    result = installer.check_for_update("DemoPlugin")
    assert result["checked"] is False
    assert result["has_update"] is False
    assert result["error"]


# -- Skills: the UI workflow over the CLI engine (D4-6) ---------------------

def _skill_source(base: Path) -> Path:
    root = base / "catalogue"
    skill_dir = root / "hello-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_skill_md("hello-skill", "Says hello."))
    return root


def test_search_catalogue_reads_a_named_source(home):
    root = _skill_source(home)
    entries = skill_registry.search_catalogue("local", str(root), "hello")
    assert [e["name"] for e in entries] == ["hello-skill"]
    assert entries[0]["description"] == "hello-skill description"


def test_search_catalogue_github_source_is_honestly_unavailable(home):
    with pytest.raises(Exception) as excinfo:
        skill_registry.search_catalogue("github", "owner/repo", "x")
    assert "no remote registry access" in str(excinfo.value)


def test_prepare_named_returns_pin_without_writing(home):
    root = _skill_source(home)
    pin = skill_registry.prepare_named("hello-skill", "local", str(root))
    assert pin["name"] == "hello-skill"
    assert pin["source"] == str(root)
    assert pin["hash"] == skill_registry.hash_skill_dir(root / "hello-skill")
    # Nothing was installed or pinned by a prepare.
    assert not (home / ".aether" / "skills" / "hello-skill").exists()
    assert load_lockfile(home / ".aether" / LOCKFILE_NAME) == {}


def test_install_refuses_when_source_changed_after_review(home):
    root = _skill_source(home)
    pin = skill_registry.prepare_named("hello-skill", "local", str(root))
    (root / "hello-skill" / "SKILL.md").write_text(
        _skill_md("hello-skill", "Changed after review."))
    from aether_engine.skills.sources import open_source
    outcome = skill_registry.install_named(
        "hello-skill", open_source("local", str(root)),
        source_type="local", source_locator=str(root),
        lockfile_path=home / ".aether" / LOCKFILE_NAME,
        skills_root=home / ".aether" / "skills",
        expected_hash=pin["hash"])
    assert outcome.ok is False
    assert outcome.action == "refused"
    assert "changed since" in outcome.detail
    assert not (home / ".aether" / "skills" / "hello-skill").exists()


def test_pin_states_report_drift_after_local_edit(home):
    root = _skill_source(home)
    manager = skills_module.GLOBAL_SKILL_MANAGER
    from aether_engine.skills.sources import open_source
    outcome = skill_registry.install_named(
        "hello-skill", open_source("local", str(root)),
        source_type="local", source_locator=str(root),
        lockfile_path=home / ".aether" / LOCKFILE_NAME,
        skills_root=home / ".aether" / "skills")
    assert outcome.ok
    manager.refresh()
    states = skill_registry.pin_states(manager)
    assert states["hello-skill"]["status"] == "installed"
    assert states["hello-skill"]["source"] == str(root)
    # A local edit is drift: the annotation must say so.
    installed = home / ".aether" / "skills" / "hello-skill" / "SKILL.md"
    installed.write_text(_skill_md("hello-skill", "Locally edited."))
    states = skill_registry.pin_states(manager)
    assert states["hello-skill"]["status"] == "drifted"
    rows = skill_registry.annotated_skill_dicts(manager)
    row = next(r for r in rows if r["name"] == "hello-skill")
    assert row["pin"]["status"] == "drifted"


# -- Socket seams: the replies the panels actually consume ------------------

class _FakeWS:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, text):
        self.sent.append(text)


class _FakeConn:
    """The WsConnection surface the senders use: ws + reply."""

    def __init__(self):
        self.ws = _FakeWS()
        self.replies: list[tuple] = []

    async def reply(self, req_id, event_type, build_payload, *,
                    error_payload=None, send_failure_message=None,
                    warn=None):
        try:
            payload = await build_payload()
        except Exception as e:  # noqa: BLE001 — the fake mirrors WsConnection.reply's contract
            payload = error_payload(e)
        self.replies.append((event_type, payload))


def test_plugin_list_reply_marks_broken_record_load_error(home, monkeypatch):
    installer = PluginInstaller(home / "plugins")
    installer._registry = {
        "Good": {"manifest": {"name": "Good", "version": "1.0"},
                 "enabled": True},
        "Broken": {"manifest": {"name": "../escape", "version": "1.0"},
                   "enabled": True},
    }
    monkeypatch.setattr(engine_state, "plugin_installer", installer)
    conn = _FakeConn()
    msg = Event(type=EventType.PLUGIN_LIST_REQUEST, request_id="list-1")
    asyncio.run(ws_routes._handle_plugin_list_request(conn, msg))
    (text,) = conn.ws.sent
    payload = json.loads(text)["payload"]
    rows = {row["name"]: row for row in payload["plugins"]}
    assert "state" not in rows["Good"]
    assert rows["Broken"]["state"] == "load_error"
    assert rows["Broken"]["error"]
    assert payload["registry_load_error"] is None


def test_plugin_update_check_reply_carries_the_comparison(home, monkeypatch):
    installer = PluginInstaller(home / "plugins")
    source = _plugin_source(home)
    _install(installer, source)
    monkeypatch.setattr(engine_state, "plugin_installer", installer)
    conn = _FakeConn()
    asyncio.run(send_plugin_update_check_result(conn, "chk-1", "DemoPlugin"))
    (event_type, payload), = conn.replies
    assert event_type == EventType.PLUGIN_UPDATE_CHECK_RESPONSE
    assert payload["success"] is True
    assert payload["checked"] is True
    assert payload["has_update"] is False


def test_skill_search_and_install_replies_land_disabled(home, monkeypatch):
    root = _skill_source(home)
    manager = skills_module.GLOBAL_SKILL_MANAGER
    monkeypatch.setattr(ws_skills, "GLOBAL_SKILL_MANAGER", manager)
    conn = _FakeConn()
    asyncio.run(ws_skills.send_skill_search_result(
        conn, "s-1", "local", str(root), "hello"))
    (event_type, payload), = conn.replies
    assert event_type == EventType.SKILL_SEARCH_RESPONSE
    assert payload["success"] is True
    assert [e["name"] for e in payload["entries"]] == ["hello-skill"]

    conn = _FakeConn()
    asyncio.run(ws_skills.send_skill_install_prepare_result(
        conn, "p-1", "local", str(root), "hello-skill"))
    (event_type, payload), = conn.replies
    assert event_type == EventType.SKILL_INSTALL_PREPARE_RESPONSE
    pin = payload["pin"]
    assert pin["hash"]

    conn = _FakeConn()
    asyncio.run(ws_skills.send_skill_install_result(
        conn, "i-1", "local", str(root), "hello-skill", pin["hash"]))
    (event_type, payload), = conn.replies
    assert event_type == EventType.SKILL_INSTALL_RESPONSE
    assert payload["success"] is True
    assert payload["action"] == "installed"
    # D4-6: a fresh install lands disabled — the refreshed list in
    # the reply already shows it that way.
    row = next(r for r in payload["skills"] if r["name"] == "hello-skill")
    assert row["enabled"] is False
    assert row["pin"]["status"] == "installed"


def test_skill_search_reply_surfaces_source_errors(home, monkeypatch):
    manager = skills_module.GLOBAL_SKILL_MANAGER
    monkeypatch.setattr(ws_skills, "GLOBAL_SKILL_MANAGER", manager)
    conn = _FakeConn()
    asyncio.run(ws_skills.send_skill_search_result(
        conn, "s-2", "github", "owner/repo", "x"))
    (event_type, payload), = conn.replies
    assert payload["success"] is False
    assert "no remote registry access" in payload["error"]
    assert payload["entries"] == []
