"""E0-01: a failed plugin upgrade must not destroy the previous install.

The pre-fix installer backed up only the package directory. Skills were
overwritten in place in the global skills dir, same-named MCP configs
were overwritten in the global MCP registry, and the in-memory plugin
entry was replaced before the registry save — so a failure at (or
after) any of those points left the previously working plugin gone or
half-replaced, while the error still claimed "rolled back". The
existing rollback test (tests/test_plugin_install_rollback.py) only
covers a failed *fresh* install, so it misses this seam entirely.

These tests install a v1 plugin (skill + MCP server), then attempt an
upgrade to v2 with a failure injected at a realistic point, and assert
the v1 installation is *exactly* intact afterwards — package files,
global skill content, MCP config (memory + disk), and the plugin
registry entry (memory + disk, including the enabled flag). A final
test pins the success path: a valid v2 replaces v1 cleanly, with no
stale v1-only skill or MCP server lingering.

Isolation mirrors tests/test_plugin_mcp_validation.py: the global MCP
registry is swapped for a tmp-path registry and Path.home is patched,
so the real ~/.aether is never touched.
"""
import json
import shutil
from pathlib import Path

import pytest

import aether_engine.mcp.registry as reg_module
from aether_engine.mcp.registry import MCPRegistry
from aether_engine.plugins.installer import PluginInstaller


@pytest.fixture
def isolated_installer(tmp_path, monkeypatch):
    """Installer + MCP registry isolated to tmp_path (no real ~/.aether)."""
    monkeypatch.setattr(reg_module, "GLOBAL_MCP_REGISTRY",
                        MCPRegistry(config_path=tmp_path / "mcp_servers.json"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return PluginInstaller(tmp_path / "plugins")


def _plugin_source(tmp_path, dirname, *, version, skills, mcp_servers):
    """Build a plugin source dir for plugin "UpPlugin".

    ``skills`` maps skill name -> SKILL.md content; the skill file for
    ``<name>`` is written as ``<name>.md``. ``mcp_servers`` maps server
    name -> args list; the config file is ``<name>.json`` with command
    "echo". A package data file ``data.txt`` carries the version tag,
    and ``legacy.txt`` exists in every version's package so the success
    test can make v2 drop it.
    """
    source_dir = tmp_path / "source" / dirname
    source_dir.mkdir(parents=True)
    manifest = {
        "name": "UpPlugin",
        "version": version,
        "skills": [{"name": name, "file_path": f"{name}.md"} for name in skills],
        "mcp_servers": [
            {"name": name, "file_path": f"{name}.json"} for name in mcp_servers
        ],
    }
    (source_dir / "manifest.json").write_text(json.dumps(manifest))
    for name, text in skills.items():
        (source_dir / f"{name}.md").write_text(text)
    for name, args in mcp_servers.items():
        (source_dir / f"{name}.json").write_text(
            json.dumps({"command": "echo", "args": args}))
    (source_dir / "data.txt").write_text(f"data {version}")
    (source_dir / "legacy.txt").write_text(f"legacy {version}")
    return source_dir


def _install_v1(tmp_path, installer):
    source = _plugin_source(
        tmp_path, "v1", version="1.0",
        skills={"upskill": "skill v1", "oldskill": "old skill v1"},
        mcp_servers={"up-srv": ["v1"], "old-srv": ["old-v1"]},
    )
    installer.install(installer.prepare_install(str(source)))
    return source


def _v2_source(tmp_path):
    return _plugin_source(
        tmp_path, "v2", version="2.0",
        skills={"upskill": "skill v2"},
        mcp_servers={"up-srv": ["v2"]},
    )


def _skill_text(tmp_path, name):
    return (tmp_path / ".aether" / "skills" / name / "SKILL.md").read_text()


def _mcp_disk_args(tmp_path, name):
    data = json.loads((tmp_path / "mcp_servers.json").read_text())
    return data[name]["args"]


def _assert_v1_intact(tmp_path, installer):
    """Every observable surface still shows the working v1 install."""
    # Package directory: v1 files, no v2 marker files.
    plugin_dir = installer.plugins_dir / "UpPlugin"
    assert (plugin_dir / "data.txt").read_text() == "data 1.0"
    assert json.loads((plugin_dir / "manifest.json").read_text())["version"] == "1.0"
    # Global skills: v1 content for both skills (oldskill not deleted).
    assert _skill_text(tmp_path, "upskill") == "skill v1"
    assert _skill_text(tmp_path, "oldskill") == "old skill v1"
    # MCP registry, memory and disk: v1 configs, both servers present.
    servers = reg_module.GLOBAL_MCP_REGISTRY.list_servers()
    assert servers["up-srv"]["args"] == ["v1"]
    assert servers["old-srv"]["args"] == ["old-v1"]
    assert _mcp_disk_args(tmp_path, "up-srv") == ["v1"]
    assert _mcp_disk_args(tmp_path, "old-srv") == ["old-v1"]
    # Plugin registry entry, memory and disk: v1, still disabled.
    entry = installer.get_all()["UpPlugin"]
    assert entry["manifest"]["version"] == "1.0"
    assert entry["enabled"] is False
    assert sorted(Path(p).name for p in entry["installed_skills"]) == [
        "oldskill", "upskill"]
    assert sorted(entry["installed_mcp"]) == ["old-srv", "up-srv"]
    disk_entry = json.loads(installer.registry_file.read_text())["UpPlugin"]
    assert disk_entry["manifest"]["version"] == "1.0"
    assert disk_entry["enabled"] is False
    # No rollback debris left behind.
    assert not (installer.plugins_dir / "UpPlugin_backup").exists()
    assert not (installer.plugins_dir / "UpPlugin_snapshot").exists()


def _disable_v1(installer):
    """Record v1 as disabled, so entry preservation includes the flag."""
    installer._registry["UpPlugin"]["enabled"] = False
    installer._save_registry()


def test_failed_upgrade_at_registry_save_restores_v1(
        tmp_path, isolated_installer, monkeypatch):
    """The review's repro: OSError injected at the plugin registry save."""
    _install_v1(tmp_path, isolated_installer)
    _disable_v1(isolated_installer)

    original_save = PluginInstaller._save_registry
    armed = {"fail": True}

    def flaky_save(self):
        if armed["fail"]:
            raise OSError("SYNTHETIC registry save failure")
        return original_save(self)

    monkeypatch.setattr(PluginInstaller, "_save_registry", flaky_save)
    with pytest.raises(RuntimeError, match="rolled back"):
        isolated_installer.install(
            isolated_installer.prepare_install(str(_v2_source(tmp_path))))
    armed["fail"] = False

    _assert_v1_intact(tmp_path, isolated_installer)


def test_failed_upgrade_mid_skill_copy_restores_v1(
        tmp_path, isolated_installer, monkeypatch):
    """Failure on v2's second skill copy, after the first overwrote v1.

    v2's "upskill" copy succeeds — overwriting v1's SKILL.md in the
    global skills dir — then the copy for the brand-new "newskill"
    raises. Pre-fix rollback deletes the upskill dir outright (it is in
    installed_skills) and v1's content is nowhere: destroyed.
    """
    _install_v1(tmp_path, isolated_installer)
    _disable_v1(isolated_installer)

    v2 = _plugin_source(
        tmp_path, "v2skill", version="2.0",
        skills={"upskill": "skill v2", "newskill": "new skill v2"},
        mcp_servers={"up-srv": ["v2"]},
    )

    original_copy2 = shutil.copy2

    def failing_copy2(src, dst, *args, **kwargs):
        if "newskill" in str(dst) and "SKILL.md" in str(dst):
            raise OSError("SYNTHETIC skill copy failure")
        return original_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "copy2", failing_copy2)
    with pytest.raises(RuntimeError, match="rolled back"):
        isolated_installer.install(
            isolated_installer.prepare_install(str(v2)))

    _assert_v1_intact(tmp_path, isolated_installer)
    # The half-installed v2-only skill must not linger either.
    assert not (tmp_path / ".aether" / "skills" / "newskill").exists()


def test_failed_upgrade_at_mcp_save_restores_v1(
        tmp_path, isolated_installer, monkeypatch):
    """Failure while persisting v2's MCP config (transient: fails once).

    add_server mutates the in-memory registry before _save_config, so a
    save failure leaves memory and disk split; rollback must re-add the
    v1 config through the registry so both agree again.
    """
    _install_v1(tmp_path, isolated_installer)
    _disable_v1(isolated_installer)

    original_save_config = MCPRegistry._save_config
    calls = {"n": 0}

    def flaky_save_config(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("SYNTHETIC MCP config save failure")
        return original_save_config(self)

    monkeypatch.setattr(MCPRegistry, "_save_config", flaky_save_config)
    with pytest.raises(RuntimeError, match="rolled back"):
        isolated_installer.install(
            isolated_installer.prepare_install(str(_v2_source(tmp_path))))

    _assert_v1_intact(tmp_path, isolated_installer)


def test_successful_upgrade_replaces_v1_cleanly(tmp_path, isolated_installer):
    """Valid v2: new content everywhere, no v1-only artefacts lingering."""
    _install_v1(tmp_path, isolated_installer)

    v2 = _v2_source(tmp_path)
    # v2's package drops the legacy file.
    (v2 / "legacy.txt").unlink()
    isolated_installer.install(isolated_installer.prepare_install(str(v2)))

    plugin_dir = isolated_installer.plugins_dir / "UpPlugin"
    assert (plugin_dir / "data.txt").read_text() == "data 2.0"
    assert not (plugin_dir / "legacy.txt").exists()
    # Skills: v2 content; the v1-only skill is gone, not lingering.
    assert _skill_text(tmp_path, "upskill") == "skill v2"
    assert not (tmp_path / ".aether" / "skills" / "oldskill").exists()
    # MCP: v2 config; the v1-only server is gone from memory and disk.
    servers = reg_module.GLOBAL_MCP_REGISTRY.list_servers()
    assert servers["up-srv"]["args"] == ["v2"]
    assert "old-srv" not in servers
    assert "old-srv" not in json.loads((tmp_path / "mcp_servers.json").read_text())
    # Registry entry describes v2 exactly.
    entry = isolated_installer.get_all()["UpPlugin"]
    assert entry["manifest"]["version"] == "2.0"
    assert [Path(p).name for p in entry["installed_skills"]] == ["upskill"]
    assert entry["installed_mcp"] == ["up-srv"]
    # No backup/snapshot debris.
    assert not (isolated_installer.plugins_dir / "UpPlugin_backup").exists()
    assert not (isolated_installer.plugins_dir / "UpPlugin_snapshot").exists()
