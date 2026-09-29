import pytest
from pathlib import Path
from aether_engine.skills.manifest import SkillManifest
from aether_engine.skills.manager import SkillManager

def test_skill_manifest_from_skill_md(tmp_path):
    skill_content = """---
name: test-skill
description: A test skill for testing.
metadata:
  version: 1.2.0
  author: Test Author
  auto_match: false
allowed-tools: read_file run_command
---
# Instructions
Do something.
"""
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text(skill_content, encoding="utf-8")
    
    manifest = SkillManifest.from_skill_md(skill_file)
    assert manifest.name == "test-skill"
    assert manifest.description == "A test skill for testing."
    assert manifest.version == "1.2.0"
    assert manifest.author == "Test Author"
    assert manifest.trigger.auto_match is False
    assert manifest.trigger.slash_commands == ["/test-skill"]
    assert manifest.requirements.tools == ["read_file", "run_command"]

def test_skill_manager_indexing(tmp_path):
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "test-skill_SKILL.md"
    skill_file.write_text("---\nname: test-skill\ndescription: Test\n---", encoding="utf-8")
    
    manager = SkillManager(global_dir=tmp_path / "global", workspace_dirs=[tmp_path])
    
    skills = manager.list_all()
    assert len(skills) == 1
    assert skills[0].name == "test-skill"
    assert manager.resolve_slash_command("/test-skill") is not None

def test_skill_manager_strip_frontmatter(tmp_path):
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: test-skill\ndescription: Test\n---\n# Body\nContent", encoding="utf-8")
    
    manager = SkillManager(global_dir=tmp_path / "global", workspace_dirs=[tmp_path])
    content = manager.load_skill_content("test-skill")
    assert content == "# Body\nContent"

def test_skill_manager_list_scripts_and_resources(tmp_path):
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: test-skill\n---", encoding="utf-8")
    
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "test.py").write_text("print('test')", encoding="utf-8")
    
    manager = SkillManager(global_dir=tmp_path / "global", workspace_dirs=[tmp_path])
    scripts = manager.list_skill_scripts("test-skill")
    assert len(scripts) == 1
    assert scripts[0].name == "test.py"
    
    res = manager.load_skill_resource("test-skill", "scripts/test.py")
    assert res == "print('test')"
