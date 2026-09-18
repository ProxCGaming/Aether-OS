import pytest
import shutil
import json
import zipfile
from pathlib import Path
from aether_engine.plugins.installer import PluginInstaller

@pytest.fixture
def temp_installer(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    return PluginInstaller(plugins_dir)

def test_install_rollback_on_failure(tmp_path: Path, temp_installer: PluginInstaller, monkeypatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    
    manifest = {
        "name": "BadPlugin",
        "version": "1.0",
        "skills": [{"name": "sk1", "file_path": "sk1.md"}]
    }
    (source_dir / "manifest.json").write_text(json.dumps(manifest))
    (source_dir / "sk1.md").write_text("dummy")
    
    # Mock shutil.copy2 to raise an exception during skill copy
    original_copy2 = shutil.copy2
    def failing_copy2(src, dst, *args, **kwargs):
        if "SKILL.md" in str(dst):
            raise OSError("Injected disk failure")
        return original_copy2(src, dst, *args, **kwargs)
    
    monkeypatch.setattr(shutil, "copy2", failing_copy2)
    
    prepare_data = temp_installer.prepare_install(str(source_dir))
    
    with pytest.raises(RuntimeError, match="rolled back"):
        temp_installer.install(prepare_data)
        
    # Verify rollback
    assert not (temp_installer.plugins_dir / "BadPlugin").exists()
    assert "BadPlugin" not in temp_installer.get_all()
