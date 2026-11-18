"""S22 contract — capabilities (SPEC-6B C7), FF4-07 portion.

Capability load errors are first-class reply data on every
list/read: rows are returned, never filtered out, so the panels
render the truth instead of absence (D4-5 health honesty):

- a corrupt plugin registry file surfaces in the PLUGIN_LIST
  reply as ``registry_load_error`` — the bare plugin list cannot
  distinguish "nothing installed" from "the registry could not
  be read", and the panel must not claim the former;
- a skill directory whose manifest fails to parse appears in
  ``SkillManager.list_all()`` as a ``state="load_error"`` row
  carrying the parse ``error`` (the MCP ``_invalid_servers``
  pattern), named by its directory;
- controls: a healthy registry reports ``registry_load_error``
  as None; a skill that parses lists as ``state="ok"``; a broken
  sibling manifest.json next to a SKILL.md that parses does NOT
  mark the skill invalid (that skill loaded — only its keyword
  inheritance failed).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from aether_common.contracts import Event, EventType
from aether_engine.plugins.installer import PluginInstaller
from aether_engine.skills.manager import SkillManager

VALID_SKILL_MD = """\
---
name: good-skill
description: A skill that parses.
---

Body.
"""


def _skill_manager(tmp_path: Path) -> SkillManager:
    return SkillManager(global_dir=tmp_path / "skills", workspace_dirs=[])


def _make_skill(global_dir: Path, dir_name: str, filename: str, content: str) -> Path:
    skill_dir = global_dir / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / filename).write_text(content, encoding="utf-8")
    return skill_dir


# ---------------------------------------------------------------------------
# Skills: broken manifests are listed, never dropped
# ---------------------------------------------------------------------------


def test_broken_skill_md_is_a_load_error_row(tmp_path):
    manager = _skill_manager(tmp_path)
    _make_skill(tmp_path / "skills", "good-skill", "SKILL.md", VALID_SKILL_MD)
    _make_skill(tmp_path / "skills", "broken-skill", "SKILL.md",
                "No frontmatter here at all.\n")
    manager._rebuild_index()

    rows = {s.name: s for s in manager.list_all()}
    assert rows["good-skill"].state == "ok"
    assert rows["good-skill"].error is None

    broken = rows["broken-skill"]
    assert broken.state == "load_error"
    assert broken.error and "frontmatter" in broken.error
    assert broken.enabled is False

    invalid = manager.invalid_skills()
    assert set(invalid) == {"broken-skill"}
    assert invalid["broken-skill"]["error"] == broken.error
    assert invalid["broken-skill"]["source"] == "global"


def test_broken_manifest_json_is_a_load_error_row(tmp_path):
    manager = _skill_manager(tmp_path)
    _make_skill(tmp_path / "skills", "json-skill", "manifest.json",
                "{ this is not json")
    manager._rebuild_index()

    rows = {s.name: s for s in manager.list_all()}
    assert rows["json-skill"].state == "load_error"
    assert rows["json-skill"].error


def test_broken_sibling_manifest_does_not_invalidate_a_parsed_skill(tmp_path):
    manager = _skill_manager(tmp_path)
    skill_dir = _make_skill(
        tmp_path / "skills", "good-skill", "SKILL.md", VALID_SKILL_MD)
    (skill_dir / "manifest.json").write_text("{ broken", encoding="utf-8")
    manager._rebuild_index()

    rows = {s.name: s for s in manager.list_all()}
    assert rows["good-skill"].state == "ok"
    assert manager.invalid_skills() == {}


def test_directory_without_any_manifest_is_not_a_broken_skill(tmp_path):
    manager = _skill_manager(tmp_path)
    (tmp_path / "skills" / "random-dir").mkdir(parents=True)
    manager._rebuild_index()

    assert manager.invalid_skills() == {}
    assert manager.list_all() == []


# ---------------------------------------------------------------------------
# Plugins: the registry load error is reply data
# ---------------------------------------------------------------------------


def _corrupt_installer(tmp_path: Path) -> PluginInstaller:
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "registry.json").write_text(
        "{ this registry is corrupt", encoding="utf-8")
    return PluginInstaller(plugins_dir)


def test_installer_records_the_registry_load_error(tmp_path):
    installer = _corrupt_installer(tmp_path)
    assert installer.get_all() == {}
    assert installer.registry_load_error is not None
    assert "corrupt" in installer.registry_load_error


class _FakeWs:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


class _FakeConn:
    def __init__(self):
        self.ws = _FakeWs()


def _run_plugin_list(monkeypatch, installer) -> dict:
    from aether_engine.routes import websocket as ws_routes
    from aether_engine.state import engine_state

    monkeypatch.setattr(engine_state, "plugin_installer", installer)
    conn = _FakeConn()
    msg = Event(type=EventType.PLUGIN_LIST_REQUEST,
                request_id="list-1", payload={})
    asyncio.run(ws_routes._handle_plugin_list_request(conn, msg))
    assert len(conn.ws.sent) == 1
    reply = Event.from_json(conn.ws.sent[0])
    assert reply.type == EventType.PLUGIN_LIST_RESPONSE
    return reply.payload


def test_plugin_list_reply_carries_the_registry_load_error(
        tmp_path, monkeypatch):
    payload = _run_plugin_list(monkeypatch, _corrupt_installer(tmp_path))
    assert payload["plugins"] == []
    assert payload["registry_load_error"] is not None
    assert "corrupt" in payload["registry_load_error"]


def test_plugin_list_reply_reports_no_error_for_a_healthy_registry(
        tmp_path, monkeypatch):
    installer = PluginInstaller(tmp_path / "plugins")
    payload = _run_plugin_list(monkeypatch, installer)
    assert payload["plugins"] == []
    assert payload["registry_load_error"] is None
