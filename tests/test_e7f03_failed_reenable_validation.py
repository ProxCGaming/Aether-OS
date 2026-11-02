"""E7-F03: a failed plugin re-enable restores skills before validation
while leaving the plugin persisted disabled.

Toggle's enable branch moved every held skill back into the scan root
and DELETED the holding directory *before* re-validating the installed
manifest/MCP config. When validation then failed, the moves were never
rolled back: the handler reported failure and the plugin stayed
``enabled=False`` in memory and on disk — but a fresh skill manager
AND an explicit live refresh both loaded the skill body from the scan
root (audit repro, checks/epic7-boundary-observations/
failed-reenable.json: disable a plugin, corrupt only its installed
server.json, re-enable the same version).

The promise (E7-03; ADR 0035): re-enable must validate BEFORE
restoring/re-adding capabilities and rebuild coherently. These tests
pin the fixed behaviour at the real seams (real installer, real
GLOBAL_SKILL_MANAGER / GLOBAL_MCP_REGISTRY seams patched to tmp
roots, the E7-F02 pattern): a failed re-enable leaves the holding
area intact and the skill unreachable from any manager, and fixing
the config makes a later re-enable succeed fully.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import aether_engine.mcp.registry as reg_module
import aether_engine.skills.manager as skills_module
from aether_engine.mcp.registry import MCPConfigError, MCPRegistry
from aether_engine.plugins.installer import PluginInstaller
from aether_engine.skills.manager import SkillManager

PLUGIN = "ReenablePlugin"
SKILL = "reenable-skill"
SLASH = f"/{SKILL}"
BODY = "SYNTHETIC_REENABLE_BODY"
SERVER = "reenable-server"


def _skill_md(name: str, body: str) -> str:
    return (
        f"---\nname: {name}\ndescription: {name} description\n---\n\n{body}\n"
    )


def _make_plugin_source(base: Path, *, with_skill: bool = True) -> Path:
    source = base / "pkg-src"
    source.mkdir()
    manifest: dict = {
        "name": PLUGIN,
        "version": "1.0",
        "mcp_servers": [{"name": SERVER, "file_path": "server.json"}],
    }
    if with_skill:
        manifest["skills"] = [{"name": SKILL, "file_path": "skill.md"}]
        (source / "skill.md").write_text(_skill_md(SKILL, BODY))
    (source / "manifest.json").write_text(json.dumps(manifest))
    (source / "server.json").write_text(json.dumps(
        {"command": "echo", "args": ["hello"]}
    ))
    return source


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Tmp home, tmp MCP registry, tmp-rooted live skill manager."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    registry = MCPRegistry(config_path=tmp_path / "mcp_servers.json")
    monkeypatch.setattr(reg_module, "GLOBAL_MCP_REGISTRY", registry)
    skills_root = tmp_path / ".aether" / "skills"
    live = SkillManager(global_dir=skills_root, workspace_dirs=[])
    monkeypatch.setattr(skills_module, "GLOBAL_SKILL_MANAGER", live)
    installer = PluginInstaller(tmp_path / "plugins")
    return _Env(tmp_path, installer, registry, live, skills_root)


class _Env:
    def __init__(self, tmp_path, installer, registry, live, skills_root):
        self.tmp_path = tmp_path
        self.installer = installer
        self.registry = registry
        self.live = live
        self.skills_root = skills_root

    @property
    def holding(self) -> Path:
        return self.skills_root / ".disabled-by-plugin" / PLUGIN

    @property
    def installed_server_json(self) -> Path:
        return self.tmp_path / "plugins" / PLUGIN / "server.json"

    def make_and_install(self, **kwargs) -> None:
        source = _make_plugin_source(self.tmp_path, **kwargs)
        self.installer.install(self.installer.prepare_install(str(source)))

    def persisted_enabled(self) -> bool:
        """Enabled flag as re-read from disk by a fresh installer."""
        fresh = PluginInstaller(self.tmp_path / "plugins")
        return fresh.get_all()[PLUGIN]["enabled"]

    def fresh_manager(self) -> SkillManager:
        return SkillManager(global_dir=self.skills_root, workspace_dirs=[])

    def corrupt_installed_server_json(self) -> None:
        self.installed_server_json.write_text("{ this is not valid json")

    def repair_installed_server_json(self) -> None:
        self.installed_server_json.write_text(json.dumps(
            {"command": "echo", "args": ["hello"]}
        ))

    def assert_skill_unreachable(self) -> None:
        """No manager — live (after explicit refresh) or fresh — may
        advertise, resolve or serve the held skill."""
        self.live.refresh()
        assert self.live.resolve_slash_command(SLASH) is None
        assert SKILL not in self.live.catalogue()
        assert self.live.load_skill_content(SKILL) == ""
        fresh = self.fresh_manager()
        assert fresh.resolve_slash_command(SLASH) is None
        assert SKILL not in fresh.catalogue()
        assert fresh.load_skill_content(SKILL) == ""


# ---------------------------------------------------------------------------
# The defect: failed re-enable must restore nothing
# ---------------------------------------------------------------------------


def test_failed_reenable_leaves_skills_held_and_unreachable(env):
    env.make_and_install()
    # Installed state: skill live in the scan root, server registered.
    assert (env.skills_root / SKILL / "SKILL.md").exists()
    assert BODY in env.live.load_skill_content(SKILL)
    assert env.registry.get_server(SERVER) is not None

    # Disable: skills move into the holding area, server removed.
    env.installer.toggle(PLUGIN, False)
    assert env.persisted_enabled() is False
    assert (env.holding / SKILL / "SKILL.md").exists()
    assert not (env.skills_root / SKILL).exists()
    assert env.registry.get_server(SERVER) is None
    env.assert_skill_unreachable()

    # Corrupt ONLY the installed server.json, then re-enable.
    env.corrupt_installed_server_json()
    with pytest.raises(ValueError, match="server.json"):
        env.installer.toggle(PLUGIN, True)

    # The failure is reported and the plugin stays disabled, in
    # memory and on disk...
    assert env.installer.get_all()[PLUGIN]["enabled"] is False
    assert env.persisted_enabled() is False

    # ...and NOTHING was restored: the holding area is intact, the
    # scan root is unchanged, no manager can reach the skill, and
    # the MCP server was not re-registered.
    assert env.holding.exists()
    assert (env.holding / SKILL / "SKILL.md").exists()
    assert not (env.skills_root / SKILL).exists()
    env.assert_skill_unreachable()
    assert env.registry.get_server(SERVER) is None


def test_failed_reenable_then_repair_then_reenable_succeeds(env):
    env.make_and_install()
    env.installer.toggle(PLUGIN, False)
    env.corrupt_installed_server_json()
    with pytest.raises(ValueError, match="server.json"):
        env.installer.toggle(PLUGIN, True)
    assert env.persisted_enabled() is False
    assert (env.holding / SKILL / "SKILL.md").exists()

    # The owner fixes the installed config; the retry must rebuild
    # coherently: skills restored, server re-registered, flag saved.
    env.repair_installed_server_json()
    env.installer.toggle(PLUGIN, True)

    assert env.installer.get_all()[PLUGIN]["enabled"] is True
    assert env.persisted_enabled() is True
    assert not env.holding.exists()
    assert (env.skills_root / SKILL / "SKILL.md").exists()
    assert env.live.resolve_slash_command(SLASH) is not None
    assert BODY in env.live.load_skill_content(SKILL)
    fresh = env.fresh_manager()
    assert BODY in fresh.load_skill_content(SKILL)
    assert env.registry.get_server(SERVER) is not None


def test_failed_reenable_via_schema_invalid_config_restores_nothing(env):
    """Validation failure from validate_mcp_server_config (valid JSON,
    refused shape) is gated the same way as a JSON parse failure."""
    env.make_and_install()
    env.installer.toggle(PLUGIN, False)
    env.installed_server_json.write_text(json.dumps(
        {"command": "echo", "url": "http://127.0.0.1:1/mcp"}
    ))
    with pytest.raises(MCPConfigError):
        env.installer.toggle(PLUGIN, True)

    assert env.persisted_enabled() is False
    assert (env.holding / SKILL / "SKILL.md").exists()
    assert not (env.skills_root / SKILL).exists()
    env.assert_skill_unreachable()
    assert env.registry.get_server(SERVER) is None


# ---------------------------------------------------------------------------
# Controls: behaviour that must not change
# ---------------------------------------------------------------------------


def test_clean_disable_reenable_roundtrip_unchanged(env):
    env.make_and_install()
    env.installer.toggle(PLUGIN, False)
    assert env.persisted_enabled() is False
    env.assert_skill_unreachable()
    assert env.registry.get_server(SERVER) is None

    env.installer.toggle(PLUGIN, True)
    assert env.persisted_enabled() is True
    assert not env.holding.exists()
    assert (env.skills_root / SKILL / "SKILL.md").exists()
    assert env.live.resolve_slash_command(SLASH) is not None
    assert BODY in env.live.load_skill_content(SKILL)
    assert env.registry.get_server(SERVER) is not None


def test_failed_reenable_without_held_skills_behaves_as_before(env):
    """A plugin with an MCP server but no skills: nothing is held, so
    a validation failure on re-enable has nothing to restore — the
    outcome (raise, still disabled, server unregistered) is the
    pre-fix behaviour too."""
    env.make_and_install(with_skill=False)
    assert env.registry.get_server(SERVER) is not None

    env.installer.toggle(PLUGIN, False)
    assert env.persisted_enabled() is False
    assert not env.holding.exists()
    assert env.registry.get_server(SERVER) is None

    env.corrupt_installed_server_json()
    with pytest.raises(ValueError, match="server.json"):
        env.installer.toggle(PLUGIN, True)

    assert env.installer.get_all()[PLUGIN]["enabled"] is False
    assert env.persisted_enabled() is False
    assert env.registry.get_server(SERVER) is None
