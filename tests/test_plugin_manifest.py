import pytest
import json
from pathlib import Path
from pydantic import ValidationError
from aether_engine.plugins.manifest import PluginManifest, parse_manifest

def test_valid_manifest(tmp_path: Path):
    manifest_data = {
        "name": "TestPlugin",
        "version": "1.0.0",
        "description": "A test plugin",
        "skills": [{"name": "test_skill", "file_path": "test.md"}],
        "tools": [{"name": "test_tool"}],
        "mcp_servers": []
    }
    m_path = tmp_path / "manifest.json"
    m_path.write_text(json.dumps(manifest_data))
    
    manifest = parse_manifest(m_path)
    assert manifest.name == "TestPlugin"
    assert manifest.version == "1.0.0"
    assert len(manifest.skills) == 1
    assert manifest.skills[0].name == "test_skill"
    assert len(manifest.tools) == 1
    assert len(manifest.mcp_servers) == 0

def test_missing_required_fields():
    with pytest.raises(ValidationError):
        PluginManifest(**{"name": "NoVersionPlugin"})

def test_invalid_manifest_path():
    with pytest.raises(FileNotFoundError):
        parse_manifest(Path("/nonexistent/manifest.json"))
