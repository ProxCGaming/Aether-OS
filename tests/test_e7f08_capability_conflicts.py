"""E7-F08: capability conflicts silently overwrite and remove another
enabled plugin's registrations; promised conflict handling is absent.

Pre-fix behaviour (audit repro, checks/epic7-boundary-observations/
plugin-registration-collision.json + skill-priority-conflict.json):

- install copied skills into a bare global skill name and assigned
  MCP configs by bare server name, with no check against another
  plugin's claims — plugin B's body/config silently replaced A's;
- disable/uninstall removed those bare names without checking the
  current owner, so disabling A removed B's body/server while B
  stayed enabled;
- skill discovery overwrote by name in scan order with no body
  comparison or warning, ignoring the manifests' numeric injection
  priority;
- capability validation refused logical ``<ns>/<name>`` names.

These tests pin the fixed behaviour at the real seams (the live
manager is the module-global ``GLOBAL_SKILL_MANAGER``, tmp-rooted —
the E6-F04/E7-F02 pattern):

- a different-body/different-config claim by another plugin (or over
  unowned user content) REFUSES the install, naming the conflict —
  nothing is overwritten;
- identical bodies/configs collapse into a shared claim; teardown
  only removes a registration when no other ENABLED plugin claims it
  and the live content still matches what the departing plugin
  installed (and never removes pre-existing user content);
- a plugin disabled before a takeover cannot re-enable over, or
  uninstall through, the new owner's registrations;
- discovery resolves differing-body same-name skills by injection
  priority (ties keep the documented scan-order override) and logs a
  warning; identical bodies collapse quietly;
- ``<ns>/<name>`` capability names validate and install end-to-end
  without ever becoming a filesystem path.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

import aether_engine.mcp.registry as reg_module
import aether_engine.skills.manager as skills_module
from aether_engine.mcp.registry import MCPRegistry, validate_mcp_server_config
from aether_engine.plugins.installer import PluginInstaller
from aether_engine.plugins.manifest import PluginManifest
from aether_engine.skills.manager import SkillManager

SKILL = "shared-skill"
SERVER = "shared-server"
BODY_A = "SYNTHETIC_BODY_A"
BODY_B = "SYNTHETIC_BODY_B"
MARKER_A = "marker-a"
MARKER_B = "marker-b"


def _skill_md(name: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {name} description\n---\n\n{body}\n"


def _server_config(marker: str) -> dict:
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": [marker],
        "timeout": 10,
    }


def _make_plugin_source(
    base: Path,
    dirname: str,
    plugin_name: str,
    *,
    skill_name: str = SKILL,
    skill_body: str = BODY_A,
    server_name: str | None = SERVER,
    server_marker: str = MARKER_A,
    version: str = "1.0",
) -> Path:
    source = base / dirname
    source.mkdir()
    manifest: dict = {"name": plugin_name, "version": version, "skills": [], "mcp_servers": []}
    if skill_name is not None:
        manifest["skills"].append({"name": skill_name, "file_path": "skill.md"})
        (source / "skill.md").write_text(_skill_md(skill_name, skill_body))
    if server_name is not None:
        manifest["mcp_servers"].append({"name": server_name, "file_path": "server.json"})
        (source / "server.json").write_text(json.dumps(_server_config(server_marker)))
    (source / "manifest.json").write_text(json.dumps(manifest))
    return source


@pytest.fixture()
def env(tmp_path, monkeypatch):
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

    def install(self, source: Path):
        self.installer.install(self.installer.prepare_install(str(source)))

    def skill_body(self) -> str:
        return self.live.load_skill_content(SKILL)

    def server_marker(self):
        cfg = self.registry.get_server(SERVER)
        return cfg["args"][0] if cfg else None

    def entry(self, plugin: str) -> dict:
        return self.installer.get_all()[plugin]


# ---------------------------------------------------------------------------
# 1. Different-content claims by another plugin refuse the install
# ---------------------------------------------------------------------------


def test_install_conflicting_plugin_is_refused_and_owner_untouched(env):
    src_a = _make_plugin_source(env.tmp_path, "pkg-a", "PluginA")
    env.install(src_a)
    assert BODY_A in env.skill_body()
    assert env.server_marker() == MARKER_A

    src_b = _make_plugin_source(
        env.tmp_path, "pkg-b", "PluginB",
        skill_body=BODY_B, server_marker=MARKER_B,
    )
    # Fail fast at prepare (before approval) …
    with pytest.raises(ValueError, match="(?i)conflict"):
        env.installer.prepare_install(str(src_b))
    # … and authoritatively at install.
    with pytest.raises(RuntimeError, match="(?i)conflict"):
        env.installer.install({"source": str(src_b),
                               "manifest": json.loads((src_b / "manifest.json").read_text())})

    # Nothing was overwritten: A's body and config are live, B absent.
    assert "PluginB" not in env.installer.get_all()
    assert BODY_A in env.skill_body()
    assert env.server_marker() == MARKER_A
    assert (env.skills_root / SKILL / "SKILL.md").exists()


def test_install_conflicting_with_unowned_user_content_is_refused(env):
    # Hand-placed user skill + a user-added server, no plugin claims.
    user_dir = env.skills_root / SKILL
    user_dir.mkdir(parents=True)
    (user_dir / "SKILL.md").write_text(_skill_md(SKILL, "USER_BODY"))
    env.registry.add_server(SERVER, _server_config("user-marker"))
    env.live.refresh()
    assert "USER_BODY" in env.skill_body()

    src = _make_plugin_source(env.tmp_path, "pkg-a", "PluginA")
    with pytest.raises(ValueError, match="(?i)conflict"):
        env.installer.prepare_install(str(src))
    with pytest.raises(RuntimeError, match="(?i)conflict"):
        env.installer.install({"source": str(src),
                               "manifest": json.loads((src / "manifest.json").read_text())})

    assert "PluginA" not in env.installer.get_all()
    assert "USER_BODY" in env.skill_body()
    assert env.server_marker() == "user-marker"


# ---------------------------------------------------------------------------
# 2. Identical content collapses into a shared claim; teardown is
#    ownership-checked
# ---------------------------------------------------------------------------


def test_identical_claim_collapses_and_teardown_respects_shared_ownership(env):
    src_a = _make_plugin_source(env.tmp_path, "pkg-a", "PluginA")
    src_b = _make_plugin_source(env.tmp_path, "pkg-b", "PluginB")  # identical body + config
    env.install(src_a)
    env.install(src_b)  # must NOT refuse: same body, same config
    assert BODY_A in env.skill_body()
    assert env.server_marker() == MARKER_A

    # Disabling A must not remove what enabled B still owns.
    env.installer.toggle("PluginA", False)
    assert env.entry("PluginA")["enabled"] is False
    assert BODY_A in env.skill_body()
    assert env.server_marker() == MARKER_A

    # Re-enabling A over the shared claim works (nothing was moved).
    env.installer.toggle("PluginA", True)
    assert BODY_A in env.skill_body()
    assert env.server_marker() == MARKER_A

    # Uninstalling A still leaves B's registrations in place.
    env.installer.uninstall("PluginA")
    assert BODY_A in env.skill_body()
    assert env.server_marker() == MARKER_A

    # Uninstalling the last enabled claimant removes them.
    env.installer.uninstall("PluginB")
    assert env.skill_body() == ""
    assert env.registry.get_server(SERVER) is None
    assert not (env.skills_root / SKILL).exists()


def test_preexisting_identical_user_skill_survives_plugin_uninstall(env):
    user_dir = env.skills_root / SKILL
    user_dir.mkdir(parents=True)
    (user_dir / "SKILL.md").write_text(_skill_md(SKILL, BODY_A))
    env.registry.add_server(SERVER, _server_config(MARKER_A))
    env.live.refresh()

    src_a = _make_plugin_source(env.tmp_path, "pkg-a", "PluginA")
    env.install(src_a)  # identical to the user's content: collapse

    env.installer.uninstall("PluginA")
    # The user's own skill + server predate the plugin and stay.
    assert (env.skills_root / SKILL / "SKILL.md").exists()
    assert BODY_A in env.skill_body()
    assert env.server_marker() == MARKER_A


# ---------------------------------------------------------------------------
# 3. Takeover while disabled: re-enable refuses, uninstall cannot
#    reach through to the new owner
# ---------------------------------------------------------------------------


def test_takeover_blocks_reenable_and_uninstall_of_previous_owner(env):
    src_a = _make_plugin_source(env.tmp_path, "pkg-a", "PluginA")
    env.install(src_a)
    env.installer.toggle("PluginA", False)
    # Sole owner disabled: artefacts are out of the live world.
    assert env.skill_body() == ""
    assert env.registry.get_server(SERVER) is None
    holding = env.skills_root / ".disabled-by-plugin" / "PluginA" / SKILL
    assert holding.exists()

    # B legitimately takes the names over with different content.
    src_b = _make_plugin_source(
        env.tmp_path, "pkg-b", "PluginB",
        skill_body=BODY_B, server_marker=MARKER_B,
    )
    env.install(src_b)
    assert BODY_B in env.skill_body()
    assert env.server_marker() == MARKER_B

    # Re-enabling A must refuse rather than overwrite B — and leave
    # A's held skills exactly where they were.
    with pytest.raises(Exception, match="(?i)conflict"):
        env.installer.toggle("PluginA", True)
    assert env.entry("PluginA")["enabled"] is False
    assert holding.exists()
    assert BODY_B in env.skill_body()
    assert env.server_marker() == MARKER_B

    # Uninstalling A removes A's own artefacts (including its held
    # skills) but cannot touch B's live registrations.
    env.installer.uninstall("PluginA")
    assert "PluginA" not in env.installer.get_all()
    assert not holding.exists()
    assert BODY_B in env.skill_body()
    assert env.server_marker() == MARKER_B
    assert env.entry("PluginB")["enabled"] is True


def test_upgrade_of_own_claim_still_allowed(env):
    src_v1 = _make_plugin_source(env.tmp_path, "pkg-v1", "PluginA", version="1.0")
    env.install(src_v1)
    src_v2 = _make_plugin_source(
        env.tmp_path, "pkg-v2", "PluginA",
        skill_body=BODY_B, server_marker=MARKER_B, version="2.0",
    )
    env.install(src_v2)
    assert BODY_B in env.skill_body()
    assert env.server_marker() == MARKER_B


# ---------------------------------------------------------------------------
# 4. Discovery: priority decides differing-body conflicts, with a
#    warning; identical bodies collapse quietly
# ---------------------------------------------------------------------------


def _discovery_root(base: Path, spec: list[tuple[str, int, str]]) -> Path:
    """spec: (dir_name, priority, body). Dirs created in spec order."""
    root = base / "skills"
    root.mkdir(parents=True)
    for dir_name, priority, body in spec:
        d = root / dir_name
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps({
            "name": "shared-disc",
            "description": "discovery conflict probe",
            "injection": {"priority": priority},
        }))
        # No frontmatter: SKILL.md parse fails over to manifest.json,
        # and load_skill_content serves this file's raw body.
        (d / "SKILL.md").write_text(body)
    return root


def test_discovery_priority_decides_differing_bodies(caplog, tmp_path):
    # Creation/scan order puts the LOW priority skill last; pre-fix
    # the later-scanned body won regardless of priority.
    root = _discovery_root(tmp_path, [
        ("aaa-first", 9, "HIGH_PRIORITY_BODY"),
        ("zzz-last", 1, "LOW_PRIORITY_BODY"),
    ])
    with caplog.at_level(logging.WARNING, logger="aether_engine.skills.manager"):
        mgr = SkillManager(global_dir=root, workspace_dirs=[])
    assert "HIGH_PRIORITY_BODY" in mgr.load_skill_content("shared-disc")
    assert any(
        "conflict" in r.message.lower() and "shared-disc" in r.message
        for r in caplog.records
    ), "a differing-body conflict must be surfaced as a warning"


def test_discovery_priority_flip_flips_winner(tmp_path):
    root = _discovery_root(tmp_path, [
        ("aaa-first", 1, "LOW_PRIORITY_BODY"),
        ("zzz-last", 9, "HIGH_PRIORITY_BODY"),
    ])
    mgr = SkillManager(global_dir=root, workspace_dirs=[])
    assert "HIGH_PRIORITY_BODY" in mgr.load_skill_content("shared-disc")


def test_discovery_identical_bodies_collapse_quietly(caplog, tmp_path):
    root = _discovery_root(tmp_path, [
        ("aaa-first", 9, "SAME_BODY"),
        ("zzz-last", 1, "SAME_BODY"),
    ])
    with caplog.at_level(logging.WARNING, logger="aether_engine.skills.manager"):
        mgr = SkillManager(global_dir=root, workspace_dirs=[])
    assert "SAME_BODY" in mgr.load_skill_content("shared-disc")
    assert not any(
        "conflict" in r.message.lower() for r in caplog.records
    ), "identical bodies must collapse without a conflict warning"


# ---------------------------------------------------------------------------
# 5. Logical <ns>/<name> addressing
# ---------------------------------------------------------------------------


def test_qualified_capability_names_validate():
    manifest = PluginManifest(name="AcmePlugin", version="1.0", skills=[{"name": "acme/deep-search", "file_path": "skill.md"}], mcp_servers=[{"name": "acme/deep-server", "file_path": "server.json"}], tools=[{"name": "acme/deep-tool"}])
    assert manifest.skills[0].name == "acme/deep-search"

    for bad in ("a/b/c", "a//b", "/a", "a/", "../x", "a/../x", "a\\b"):
        with pytest.raises(ValueError):
            PluginManifest(name="AcmePlugin", version="1.0", skills=[{"name": bad, "file_path": "skill.md"}])
    # The plugin's own name is a directory: never qualified.
    with pytest.raises(ValueError):
        PluginManifest(name="acme/plugin", version="1.0")

    cfg = validate_mcp_server_config(
        "acme/deep-server", {"command": "npx", "transport": "stdio"})
    assert cfg["transport"] == "stdio"
    with pytest.raises(ValueError):
        validate_mcp_server_config("a/b/c", {"command": "npx"})


def test_qualified_skill_and_server_install_end_to_end(env):
    src = _make_plugin_source(
        env.tmp_path, "pkg-acme", "AcmePlugin",
        skill_name="acme/deep-search", skill_body="QUALIFIED_BODY",
        server_name="acme/deep-server", server_marker=MARKER_A,
    )
    env.install(src)

    # The qualified name is logical only: exactly one new single-
    # segment directory inside the skills root, no nested path.
    new_dirs = {p.name for p in env.skills_root.iterdir() if p.is_dir()}
    assert len(new_dirs) == 1
    only = next(iter(new_dirs))
    assert "/" not in only and "\\" not in only

    # Addressable under its qualified name everywhere.
    assert "QUALIFIED_BODY" in env.live.load_skill_content("acme/deep-search")
    assert env.registry.get_server("acme/deep-server") is not None

    env.installer.uninstall("AcmePlugin")
    assert env.live.load_skill_content("acme/deep-search") == ""
    assert env.registry.get_server("acme/deep-server") is None
    assert not (env.skills_root / only).exists()
