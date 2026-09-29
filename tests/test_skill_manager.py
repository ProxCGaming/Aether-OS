"""
Tests for Agent Skills spec alignment -- SKILL.md-based discovery & injection.

Covers:
  - SkillManifest.from_skill_md: YAML parsing, required fields, optional fields
  - SkillManager._rebuild_index: filename priority, manifest.json fallback, name mismatch warn
  - SkillManager.load_skill_content: frontmatter stripping, flexible filenames
  - SkillManager.load_skill_resource: security path-escape guard, happy path
  - SkillManager.list_skill_scripts: empty when no scripts/, populated when scripts/ exists
  - active_skills: state-based injection verify via manager content loading
"""

import json
import textwrap
from pathlib import Path

import pytest

from aether_engine.skills.manifest import SkillManifest
from aether_engine.skills.manager import SkillManager


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

MINIMAL_SKILL_MD = textwrap.dedent("""\
    ---
    name: my-skill
    description: A test skill for unit testing.
    ---

    ## Instructions
    Do stuff.
""")

FULL_SKILL_MD = textwrap.dedent("""\
    ---
    name: full-skill
    description: Full spec skill with all optional fields.
    license: MIT
    compatibility: ">=1.0.0"
    allowed-tools: run_command view_file
    metadata:
      version: "2.3.1"
      author: Test Author
      auto_match: "false"
    ---

    ## Full Instructions
    These are the real instructions the agent should see.
""")


def make_skill_dir(base: Path, dir_name: str, filename: str, content: str) -> Path:
    skill_dir = base / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / filename).write_text(content, encoding="utf-8")
    return skill_dir


# ---------------------------------------------------------------------------
# SkillManifest.from_skill_md
# ---------------------------------------------------------------------------

class TestFromSkillMd:
    def test_minimal_required_fields(self, tmp_path):
        path = tmp_path / "SKILL.md"
        path.write_text(MINIMAL_SKILL_MD, encoding="utf-8")
        m = SkillManifest.from_skill_md(path)
        assert m.name == "my-skill"
        assert m.description == "A test skill for unit testing."

    def test_slash_command_auto_generated(self, tmp_path):
        path = tmp_path / "SKILL.md"
        path.write_text(MINIMAL_SKILL_MD, encoding="utf-8")
        m = SkillManifest.from_skill_md(path)
        assert "/my-skill" in m.trigger.slash_commands

    def test_auto_match_default_true(self, tmp_path):
        path = tmp_path / "SKILL.md"
        path.write_text(MINIMAL_SKILL_MD, encoding="utf-8")
        m = SkillManifest.from_skill_md(path)
        assert m.trigger.auto_match is True

    def test_full_optional_fields(self, tmp_path):
        path = tmp_path / "SKILL.md"
        path.write_text(FULL_SKILL_MD, encoding="utf-8")
        m = SkillManifest.from_skill_md(path)
        assert m.name == "full-skill"
        assert m.license == "MIT"
        assert m.compatibility == ">=1.0.0"
        assert m.version == "2.3.1"
        assert m.author == "Test Author"
        assert m.trigger.auto_match is False

    def test_allowed_tools_parsed(self, tmp_path):
        path = tmp_path / "SKILL.md"
        path.write_text(FULL_SKILL_MD, encoding="utf-8")
        m = SkillManifest.from_skill_md(path)
        assert "run_command" in m.requirements.tools
        assert "view_file" in m.requirements.tools

    def test_missing_name_raises(self, tmp_path):
        path = tmp_path / "SKILL.md"
        path.write_text("---\ndescription: No name.\n---\nBody\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Missing required"):
            SkillManifest.from_skill_md(path)

    def test_no_frontmatter_raises(self, tmp_path):
        path = tmp_path / "SKILL.md"
        path.write_text("Just plain markdown, no YAML.", encoding="utf-8")
        with pytest.raises(ValueError, match="No YAML frontmatter"):
            SkillManifest.from_skill_md(path)

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            SkillManifest.from_skill_md(tmp_path / "nonexistent.md")


# ---------------------------------------------------------------------------
# SkillManager._rebuild_index -- discovery priority
# ---------------------------------------------------------------------------

class TestRebuildIndex:
    def _sm(self, base: Path) -> SkillManager:
        return SkillManager(workspace_dirs=[base])

    def test_discovers_skill_md(self, tmp_path):
        make_skill_dir(tmp_path, "my-skill", "SKILL.md", MINIMAL_SKILL_MD)
        assert "my-skill" in [s.name for s in self._sm(tmp_path).list_all()]

    def test_discovers_underscore_filename(self, tmp_path):
        content = MINIMAL_SKILL_MD.replace("name: my-skill", "name: alt-skill")
        make_skill_dir(tmp_path, "alt-skill", "alt-skill_SKILL.md", content)
        assert "alt-skill" in [s.name for s in self._sm(tmp_path).list_all()]

    def test_discovers_dash_skill_md(self, tmp_path):
        content = MINIMAL_SKILL_MD.replace("name: my-skill", "name: dash-skill")
        make_skill_dir(tmp_path, "dash-skill", "dash-skill-skill.md", content)
        assert "dash-skill" in [s.name for s in self._sm(tmp_path).list_all()]

    def test_skill_md_priority_over_manifest_json(self, tmp_path):
        skill_dir = make_skill_dir(tmp_path, "my-skill", "SKILL.md", MINIMAL_SKILL_MD)
        (skill_dir / "manifest.json").write_text(
            json.dumps({"name": "my-skill", "description": "FROM MANIFEST JSON"})
        )
        sm = self._sm(tmp_path)
        summaries = {s.name: s for s in sm.list_all()}
        assert summaries["my-skill"].description == "A test skill for unit testing."

    def test_fallback_to_manifest_json(self, tmp_path):
        skill_dir = tmp_path / "legacy-skill"
        skill_dir.mkdir()
        (skill_dir / "manifest.json").write_text(
            json.dumps({"name": "legacy-skill", "description": "Legacy"})
        )
        assert "legacy-skill" in [s.name for s in self._sm(tmp_path).list_all()]

    def test_slash_map_populated(self, tmp_path):
        make_skill_dir(tmp_path, "my-skill", "SKILL.md", MINIMAL_SKILL_MD)
        resolved = self._sm(tmp_path).resolve_slash_command("/my-skill")
        assert resolved is not None and resolved.name == "my-skill"

    def test_unknown_slash_command_is_none(self, tmp_path):
        assert self._sm(tmp_path).resolve_slash_command("/ghost") is None

    def test_name_mismatch_still_indexed(self, tmp_path):
        content = MINIMAL_SKILL_MD.replace("name: my-skill", "name: other-name")
        make_skill_dir(tmp_path, "my-skill", "SKILL.md", content)
        assert "other-name" in [s.name for s in self._sm(tmp_path).list_all()]


# ---------------------------------------------------------------------------
# SkillManager.load_skill_content -- frontmatter stripping
# ---------------------------------------------------------------------------

class TestLoadSkillContent:
    def _sm(self, base: Path) -> SkillManager:
        return SkillManager(workspace_dirs=[base])

    def test_frontmatter_stripped(self, tmp_path):
        make_skill_dir(tmp_path, "my-skill", "SKILL.md", MINIMAL_SKILL_MD)
        content = self._sm(tmp_path).load_skill_content("my-skill")
        assert "name: my-skill" not in content
        assert "## Instructions" in content

    def test_body_preserved(self, tmp_path):
        make_skill_dir(tmp_path, "my-skill", "SKILL.md", MINIMAL_SKILL_MD)
        assert "Do stuff." in self._sm(tmp_path).load_skill_content("my-skill")

    def test_content_cached(self, tmp_path):
        make_skill_dir(tmp_path, "my-skill", "SKILL.md", MINIMAL_SKILL_MD)
        sm = self._sm(tmp_path)
        assert sm.load_skill_content("my-skill") is sm.load_skill_content("my-skill")

    def test_unknown_skill_empty(self, tmp_path):
        assert self._sm(tmp_path).load_skill_content("ghost") == ""

    def test_loads_underscore_filename(self, tmp_path):
        content = MINIMAL_SKILL_MD.replace("name: my-skill", "name: alt-skill")
        make_skill_dir(tmp_path, "alt-skill", "alt-skill_SKILL.md", content)
        c = self._sm(tmp_path).load_skill_content("alt-skill")
        assert "## Instructions" in c


# ---------------------------------------------------------------------------
# SkillManager.load_skill_resource -- path-escape security
# ---------------------------------------------------------------------------

class TestLoadSkillResource:
    def _sm_with_ref(self, tmp_path: Path) -> SkillManager:
        skill_dir = make_skill_dir(tmp_path, "my-skill", "SKILL.md", MINIMAL_SKILL_MD)
        ref = skill_dir / "references"
        ref.mkdir()
        (ref / "REFERENCE.md").write_text("# Reference Content", encoding="utf-8")
        return SkillManager(workspace_dirs=[tmp_path])

    def test_loads_reference_file(self, tmp_path):
        sm = self._sm_with_ref(tmp_path)
        assert "Reference Content" in sm.load_skill_resource("my-skill", "references/REFERENCE.md")

    def test_path_traversal_blocked(self, tmp_path):
        sm = self._sm_with_ref(tmp_path)
        assert sm.load_skill_resource("my-skill", "../../etc/passwd") == ""

    def test_nonexistent_returns_empty(self, tmp_path):
        sm = self._sm_with_ref(tmp_path)
        assert sm.load_skill_resource("my-skill", "references/ghost.md") == ""

    def test_unknown_skill_returns_empty(self, tmp_path):
        sm = self._sm_with_ref(tmp_path)
        assert sm.load_skill_resource("ghost-skill", "SKILL.md") == ""


# ---------------------------------------------------------------------------
# SkillManager.list_skill_scripts
# ---------------------------------------------------------------------------

class TestListSkillScripts:
    def test_no_scripts_dir(self, tmp_path):
        make_skill_dir(tmp_path, "my-skill", "SKILL.md", MINIMAL_SKILL_MD)
        sm = SkillManager(workspace_dirs=[tmp_path])
        assert sm.list_skill_scripts("my-skill") == []

    def test_scripts_discovered(self, tmp_path):
        skill_dir = make_skill_dir(tmp_path, "my-skill", "SKILL.md", MINIMAL_SKILL_MD)
        scripts = skill_dir / "scripts"
        scripts.mkdir()
        (scripts / "migrate.py").write_text("print('ok')", encoding="utf-8")
        (scripts / "backup.sh").write_text("#!/bin/sh", encoding="utf-8")
        sm = SkillManager(workspace_dirs=[tmp_path])
        names = [f.name for f in sm.list_skill_scripts("my-skill")]
        assert "migrate.py" in names
        assert "backup.sh" in names

    def test_unknown_skill_empty(self, tmp_path):
        assert SkillManager(workspace_dirs=[tmp_path]).list_skill_scripts("ghost") == []


# ---------------------------------------------------------------------------
# active_skills -- content injection simulation
# ---------------------------------------------------------------------------

class TestActiveSkillsInjection:
    """Simulate the node-side active_skills injection logic."""

    def test_active_skill_injected_without_frontmatter(self, tmp_path):
        make_skill_dir(tmp_path, "my-skill", "SKILL.md", MINIMAL_SKILL_MD)
        sm = SkillManager(workspace_dirs=[tmp_path])

        active_skills = ["my-skill"]
        sections = []
        for name in active_skills:
            c = sm.load_skill_content(name)
            if c:
                sections.append(f"--- SKILL: {name} ---\n{c}\n--- END SKILL ---")

        assert len(sections) == 1
        injected = sections[0]
        assert "Do stuff." in injected
        # Frontmatter should NOT appear after the SKILL header
        skill_body = injected.split("--- SKILL: my-skill ---\n", 1)[1]
        assert "name: my-skill" not in skill_body

    def test_multiple_active_skills(self, tmp_path):
        make_skill_dir(tmp_path, "my-skill", "SKILL.md", MINIMAL_SKILL_MD)
        content2 = MINIMAL_SKILL_MD.replace("name: my-skill", "name: second-skill").replace(
            "A test skill for unit testing.", "Second skill."
        )
        make_skill_dir(tmp_path, "second-skill", "SKILL.md", content2)
        sm = SkillManager(workspace_dirs=[tmp_path])

        active_skills = ["my-skill", "second-skill"]
        sections = [f"--- SKILL: {n} ---\n{sm.load_skill_content(n)}\n--- END ---" for n in active_skills if sm.load_skill_content(n)]
        assert len(sections) == 2
