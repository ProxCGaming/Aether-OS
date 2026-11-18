"""Plugin ``tools`` refusal: manifests declaring tools are refused.

Per ADR 0035 a plugin is a bundle of skills + MCP servers (+
agents / hooks) — never in-engine code. A tool a plugin wants to
offer must arrive through an MCP server the plugin bundles. Before
this refusal, a manifest could declare ``tools`` and the installer
recorded their names while nothing ever made them callable; now
``prepare_install`` refuses such a manifest with a message naming
the MCP route, and ``install`` re-checks authoritatively before
any mutation.

The global MCP registry is swapped for a tmp-path registry so the
real ``~/.aether/mcp_servers.json`` is never touched (the pattern
from test_plugin_mcp_validation.py).
"""
import json
import zipfile
from pathlib import Path

import pytest

import aether_engine.mcp.registry as reg_module
from aether_engine.mcp.registry import MCPRegistry
from aether_engine.plugins.installer import PluginInstaller
from aether_engine.plugins.manifest import PluginManifest


@pytest.fixture
def isolated_installer(tmp_path, monkeypatch):
    """Installer + MCP registry isolated to tmp_path (no real ~/.aether)."""
    monkeypatch.setattr(reg_module, "GLOBAL_MCP_REGISTRY",
                        MCPRegistry(config_path=tmp_path / "mcp_servers.json"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return PluginInstaller(tmp_path / "plugins")


def _plugin_source(tmp_path, name, manifest_extra, files=None):
    source_dir = tmp_path / "source" / name
    source_dir.mkdir(parents=True)
    manifest = {"name": name, "version": "1.0", **manifest_extra}
    (source_dir / "manifest.json").write_text(json.dumps(manifest))
    for fname, text in (files or {}).items():
        (source_dir / fname).write_text(text)
    return source_dir


def test_prepare_refuses_manifest_declaring_tools(tmp_path, isolated_installer):
    source = _plugin_source(
        tmp_path, "ToolsPlugin",
        {"tools": [{"name": "do-thing"}]},
    )
    with pytest.raises(ValueError, match="MCP server"):
        isolated_installer.prepare_install(str(source))
    assert "ToolsPlugin" not in isolated_installer.get_all()
    assert not (isolated_installer.plugins_dir / "ToolsPlugin").exists()


def test_prepare_refuses_tools_in_zip_before_archive_checks(tmp_path, isolated_installer):
    # The refusal fires on the parsed manifest, before zip archive
    # validation — a tools-declaring zip gets the tools refusal, not
    # an unsigned-archive complaint.
    zip_path = tmp_path / "ToolsZip.zip"
    manifest = {"name": "ToolsZip", "version": "1.0",
                "tools": [{"name": "do-thing"}]}
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
    with pytest.raises(ValueError, match="cannot contribute tools"):
        isolated_installer.prepare_install(str(zip_path))
    assert "ToolsZip" not in isolated_installer.get_all()


def test_install_refuses_tools_before_any_mutation(tmp_path, isolated_installer):
    """install() re-checks: hand-built prepare data cannot sneak tools in."""
    source = _plugin_source(tmp_path, "SneakyPlugin", {})
    manifest = PluginManifest(
        name="SneakyPlugin", version="1.0", tools=[{"name": "do-thing"}])
    prepare_data = {"source": str(source), "manifest": manifest.model_dump()}
    with pytest.raises(RuntimeError, match="MCP server"):
        isolated_installer.install(prepare_data)
    assert not (isolated_installer.plugins_dir / "SneakyPlugin").exists()
    assert "SneakyPlugin" not in isolated_installer.get_all()


def test_plugin_without_tools_still_installs_and_its_server_is_confirmed(
        tmp_path, isolated_installer):
    """The guard: skills + MCP servers (no tools) install as before,
    and the bundled server carries the confirmed marker — the plugin
    approval is its confirmation, so the auto-connect gate admits it."""
    source = _plugin_source(
        tmp_path, "GoodPlugin",
        {"mcp_servers": [{"name": "good-srv", "file_path": "mcp.json"}]},
        {"mcp.json": json.dumps({"command": "echo", "args": ["hello"]})},
    )
    prepare_data = isolated_installer.prepare_install(str(source))
    isolated_installer.install(prepare_data)
    assert "GoodPlugin" in isolated_installer.get_all()
    server = reg_module.GLOBAL_MCP_REGISTRY.list_servers()["good-srv"]
    assert server["confirmed"] is True
