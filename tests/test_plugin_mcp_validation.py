"""Issue 008: plugin MCP servers are validated before install.

A plugin manifest may declare MCP servers (CapabilityDef with a
``file_path`` pointing at a JSON config inside the package). The
installer must validate every declared server against the same schema
as issue 001 (``validate_mcp_server_config``) *before* adding anything
to the registry — validate first, add second — and a bad manifest must
be rejected with a named error, leaving no half-added server behind.

The global MCP registry is swapped for a tmp-path registry in these
tests so the real ``~/.aether/mcp_servers.json`` is never touched.
"""
import json
from pathlib import Path

import pytest

import aether_engine.mcp.registry as reg_module
from aether_engine.mcp.registry import MCPConfigError, MCPRegistry
from aether_engine.plugins.installer import PluginInstaller
from aether_engine.plugins.manifest import PluginManifest


@pytest.fixture
def isolated_installer(tmp_path, monkeypatch):
    """Installer + MCP registry isolated to tmp_path (no real ~/.aether)."""
    monkeypatch.setattr(reg_module, "GLOBAL_MCP_REGISTRY",
                        MCPRegistry(config_path=tmp_path / "mcp_servers.json"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return PluginInstaller(tmp_path / "plugins")


def _plugin_source(tmp_path, name, mcp_servers, configs):
    """Build a plugin source dir. ``configs`` maps file name -> JSON text."""
    source_dir = tmp_path / "source" / name
    source_dir.mkdir(parents=True)
    manifest = {"name": name, "version": "1.0", "mcp_servers": mcp_servers}
    (source_dir / "manifest.json").write_text(json.dumps(manifest))
    for fname, text in configs.items():
        (source_dir / fname).write_text(text)
    return source_dir


def _good_config():
    return {"command": "echo", "args": ["hello"]}


def test_prepare_rejects_both_transports(tmp_path, isolated_installer):
    source = _plugin_source(
        tmp_path, "BadPlugin",
        [{"name": "bad-srv", "file_path": "mcp.json"}],
        {"mcp.json": json.dumps({"command": "x", "url": "http://127.0.0.1:1/mcp"})},
    )
    with pytest.raises(MCPConfigError, match="exactly one transport"):
        isolated_installer.prepare_install(str(source))


def test_install_rejects_bad_config_before_any_mutation(tmp_path, isolated_installer):
    """install() itself validates first: nothing is created or registered."""
    manifest = PluginManifest(**{
        "name": "BadPlugin", "version": "1.0",
        "mcp_servers": [{"name": "bad-srv", "file_path": "mcp.json"}],
    })
    source = _plugin_source(
        tmp_path, "BadPlugin",
        [{"name": "bad-srv", "file_path": "mcp.json"}],
        {"mcp.json": json.dumps({"command": "x", "url": "http://127.0.0.1:1/mcp"})},
    )
    prepare_data = {"source": str(source), "manifest": manifest.model_dump()}
    with pytest.raises(RuntimeError, match="rolled back"):
        isolated_installer.install(prepare_data)
    assert not (isolated_installer.plugins_dir / "BadPlugin").exists()
    assert "bad-srv" not in reg_module.GLOBAL_MCP_REGISTRY.list_servers()
    assert "BadPlugin" not in isolated_installer.get_all()


def test_install_validates_all_servers_before_adding_any(tmp_path, isolated_installer):
    """First server good, second bad: NEITHER may be left in the registry."""
    source = _plugin_source(
        tmp_path, "MixedPlugin",
        [{"name": "good-srv", "file_path": "good.json"},
         {"name": "bad-srv", "file_path": "bad.json"}],
        {"good.json": json.dumps(_good_config()),
         "bad.json": json.dumps({"command": "x", "url": "http://127.0.0.1:1/mcp"})},
    )
    with pytest.raises(MCPConfigError, match="exactly one transport"):
        isolated_installer.prepare_install(str(source))
    assert "good-srv" not in reg_module.GLOBAL_MCP_REGISTRY.list_servers()
    assert "bad-srv" not in reg_module.GLOBAL_MCP_REGISTRY.list_servers()
    assert not (isolated_installer.plugins_dir / "MixedPlugin").exists()


def test_install_rejects_corrupt_mcp_config_json(tmp_path, isolated_installer):
    """Corrupt JSON must be a named error, never a silent dummy server."""
    source = _plugin_source(
        tmp_path, "CorruptPlugin",
        [{"name": "corrupt-srv", "file_path": "mcp.json"}],
        {"mcp.json": "{not json"},
    )
    with pytest.raises((MCPConfigError, ValueError), match="not valid JSON"):
        isolated_installer.prepare_install(str(source))
    assert "corrupt-srv" not in reg_module.GLOBAL_MCP_REGISTRY.list_servers()
    assert "dummy" not in str(reg_module.GLOBAL_MCP_REGISTRY.list_servers())


def test_install_rejects_missing_mcp_config_file(tmp_path, isolated_installer):
    source = _plugin_source(
        tmp_path, "MissingPlugin",
        [{"name": "gone-srv", "file_path": "nope.json"}],
        {},
    )
    with pytest.raises((MCPConfigError, ValueError), match="not found"):
        isolated_installer.prepare_install(str(source))
    assert "gone-srv" not in reg_module.GLOBAL_MCP_REGISTRY.list_servers()


def test_install_rejects_bad_server_name(tmp_path, isolated_installer):
    source = _plugin_source(
        tmp_path, "BadNamePlugin",
        [{"name": "bad name!", "file_path": "mcp.json"}],
        {"mcp.json": json.dumps(_good_config())},
    )
    with pytest.raises(MCPConfigError, match="Invalid MCP server name"):
        isolated_installer.prepare_install(str(source))


def test_install_rejects_server_with_no_config_file(tmp_path, isolated_installer):
    source = _plugin_source(
        tmp_path, "NoConfigPlugin",
        [{"name": "nocfg-srv"}],
        {},
    )
    with pytest.raises((MCPConfigError, ValueError), match="file_path"):
        isolated_installer.prepare_install(str(source))


def test_install_with_good_mcp_config_registers_server(tmp_path, isolated_installer):
    source = _plugin_source(
        tmp_path, "GoodPlugin",
        [{"name": "good-srv", "file_path": "mcp.json"}],
        {"mcp.json": json.dumps(_good_config())},
    )
    prepare_data = isolated_installer.prepare_install(str(source))
    isolated_installer.install(prepare_data)
    assert "good-srv" in reg_module.GLOBAL_MCP_REGISTRY.list_servers()
    assert "GoodPlugin" in isolated_installer.get_all()
    assert (isolated_installer.plugins_dir / "GoodPlugin").exists()


def test_failed_validation_leaves_previous_install_intact(tmp_path, isolated_installer):
    """A bad re-install must not clobber the previous good version."""
    good_source = _plugin_source(
        tmp_path, "KeepPlugin",
        [{"name": "keep-srv", "file_path": "mcp.json"}],
        {"mcp.json": json.dumps(_good_config())},
    )
    isolated_installer.install(isolated_installer.prepare_install(str(good_source)))
    marker = isolated_installer.plugins_dir / "KeepPlugin" / "keep.txt"
    marker.write_text("v1")

    bad_source = _plugin_source(
        tmp_path, "KeepPlugin2",
        [{"name": "bad-srv", "file_path": "mcp.json"}],
        {"mcp.json": json.dumps({"url": ""})},
    )
    # Same plugin name, bad MCP config: prepare must reject before install.
    bad_manifest = {"name": "KeepPlugin", "version": "2.0",
                    "mcp_servers": [{"name": "bad-srv", "file_path": "mcp.json"}]}
    (bad_source / "manifest.json").write_text(json.dumps(bad_manifest))
    with pytest.raises(MCPConfigError):
        isolated_installer.prepare_install(str(bad_source))

    assert marker.read_text() == "v1"
    assert "keep-srv" in reg_module.GLOBAL_MCP_REGISTRY.list_servers()
    assert "bad-srv" not in reg_module.GLOBAL_MCP_REGISTRY.list_servers()
