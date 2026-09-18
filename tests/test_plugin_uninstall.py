import pytest
import json
from pathlib import Path
from aether_engine.plugins.installer import PluginInstaller

@pytest.fixture
def temp_installer(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    return PluginInstaller(plugins_dir)

def test_uninstall_removes_everything(tmp_path: Path, temp_installer: PluginInstaller):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    
    manifest = {
        "name": "GoodPlugin",
        "version": "1.0",
        "skills": [{"name": "sk1", "file_path": "sk1.md"}],
        "tools": [{"name": "tool1"}]
    }
    (source_dir / "manifest.json").write_text(json.dumps(manifest))
    (source_dir / "sk1.md").write_text("skill_content")
    
    prepare_data = temp_installer.prepare_install(str(source_dir))
    temp_installer.install(prepare_data)
    
    # Verify installed
    assert (temp_installer.plugins_dir / "GoodPlugin").exists()
    assert "GoodPlugin" in temp_installer.get_all()
    assert (Path.home() / ".aether" / "skills" / "sk1" / "SKILL.md").exists()
    
    # Now uninstall
    temp_installer.uninstall("GoodPlugin")
    
    # Verify uninstalled
    assert not (temp_installer.plugins_dir / "GoodPlugin").exists()
    assert "GoodPlugin" not in temp_installer.get_all()
    assert not (Path.home() / ".aether" / "skills" / "sk1").exists()

