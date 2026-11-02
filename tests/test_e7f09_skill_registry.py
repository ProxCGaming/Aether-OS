"""
E7-F09 — skill registry CLI + lockfile workflow (addendum omp #28).

The owner-approved addendum promises: "Skill registry CLI + lockfile:
install/search/list over `skills.lock.json`, pinned and reproducible."
Before this slice, no lockfile reader/writer and no skill command
existed anywhere in the product (the tracked root `skills-lock.json`
is a developer artifact nothing consumes).

These tests drive the real seams:
  - the lockfile module (round-trip fidelity, corrupt = loud refusal),
  - the CLI entry point `aether_engine.skills.cli:main` (the function
    the pyproject console script and run_skills.py both call), plus one
    subprocess run through `python -m aether_engine.skills.cli`,
  - a fresh SkillManager over the install target (the skill must be
    genuinely discoverable — catalogue + body — not just copied).

Everything is local and offline: the source is a fixture directory.
GitHub-typed sources must fail loudly, never fabricate a catalogue.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from aether_engine.skills import lockfile as lockfile_mod
from aether_engine.skills.cli import main as cli_main
from aether_engine.skills.lockfile import (
    SkillLockError,
    SkillPin,
    hash_skill_dir,
    load_lockfile,
    save_lockfile,
)
from aether_engine.skills.manager import SkillManager

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

DEMO_SKILL_MD = textwrap.dedent("""\
    ---
    name: demo-skill
    description: A demonstration skill for registry tests.
    metadata:
      version: "1.2.0"
    ---

    ## Demo Instructions
    DEMO BODY: greet the user warmly.
""")

OTHER_SKILL_MD = textwrap.dedent("""\
    ---
    name: other-skill
    description: Builds weekly reporting digests.
    metadata:
      version: "0.9.1"
    ---

    ## Reporting Instructions
    OTHER BODY: summarise the week.
""")

MULTI_SKILL_MD = textwrap.dedent("""\
    ---
    name: multi-file
    description: A skill with bundled reference files.
    ---

    ## Multi Instructions
    MULTI BODY: see references.
""")


def make_source(root: Path) -> Path:
    """A local registry source: one directory per skill."""
    src = root / "source"
    for dirname, body in (
        ("demo-skill", DEMO_SKILL_MD),
        ("other-skill", OTHER_SKILL_MD),
        ("multi-file", MULTI_SKILL_MD),
    ):
        d = src / dirname
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    notes = src / "multi-file" / "references"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "NOTES.md").write_text("reference payload\n", encoding="utf-8")
    return src


@pytest.fixture()
def layout(tmp_path: Path) -> dict[str, Path]:
    return {
        "source": make_source(tmp_path),
        "lockfile": tmp_path / "skills.lock.json",
        "skills_dir": tmp_path / "installed",
    }


def run_cli(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = cli_main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def base_args(layout: dict[str, Path]) -> list[str]:
    return [
        "--lockfile", str(layout["lockfile"]),
        "--skills-dir", str(layout["skills_dir"]),
    ]


# ---------------------------------------------------------------------------
# Lockfile: round-trip + corrupt handling
# ---------------------------------------------------------------------------

def test_lockfile_round_trip_preserves_pins(tmp_path: Path):
    path = tmp_path / "skills.lock.json"
    pins = {
        "demo-skill": SkillPin(
            name="demo-skill", source="/some/source", source_type="local",
            ref="1.2.0", skill_path=None, computed_hash="ab" * 32),
        "repo-skill": SkillPin(
            name="repo-skill", source="owner/repo", source_type="github",
            ref=None, skill_path="skills/repo-skill",
            computed_hash="cd" * 32),
    }
    save_lockfile(path, pins)
    assert load_lockfile(path) == pins

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["skills"]["demo-skill"]["sourceType"] == "local"
    assert raw["skills"]["demo-skill"]["computedHash"] == "ab" * 32
    assert raw["skills"]["repo-skill"]["skillPath"] == "skills/repo-skill"


def test_lockfile_missing_file_is_empty_not_an_error(tmp_path: Path):
    assert load_lockfile(tmp_path / "nope" / "skills.lock.json") == {}


@pytest.mark.parametrize("body", [
    "{not json",
    json.dumps(["a", "list"]),
    json.dumps({"version": 1, "skills": ["not", "a", "map"]}),
    json.dumps({"version": 1, "skills": {"x": {"source": "s"}}}),  # no hash
    json.dumps({"skills": {}}),  # no version
])
def test_lockfile_corrupt_is_refused_loudly(tmp_path: Path, body: str):
    path = tmp_path / lockfile_mod.LOCKFILE_NAME
    path.write_text(body, encoding="utf-8")
    with pytest.raises(SkillLockError):
        load_lockfile(path)
    # The corrupt file is evidence: it must survive untouched.
    assert path.read_text(encoding="utf-8") == body


def test_hash_skill_dir_covers_all_files(tmp_path: Path):
    d = tmp_path / "skill"
    (d / "references").mkdir(parents=True)
    (d / "SKILL.md").write_text(DEMO_SKILL_MD, encoding="utf-8")
    before = hash_skill_dir(d)
    (d / "references" / "NOTES.md").write_text("x\n", encoding="utf-8")
    assert hash_skill_dir(d) != before  # sibling files are pinned too
    (d / "SKILL.md").write_text(DEMO_SKILL_MD + " ", encoding="utf-8")
    assert hash_skill_dir(d) != before


# ---------------------------------------------------------------------------
# CLI: install one skill by name, then replay from the lockfile
# ---------------------------------------------------------------------------

def test_install_named_writes_pin_and_materialises(layout, capsys):
    code, out, _ = run_cli(
        capsys, *base_args(layout), "install", "demo-skill",
        "--source", str(layout["source"]))
    assert code == 0
    assert "demo-skill" in out

    pins = load_lockfile(layout["lockfile"])
    assert set(pins) == {"demo-skill"}
    pin = pins["demo-skill"]
    assert pin.source_type == "local"
    assert pin.source == str(layout["source"])
    assert pin.ref == "1.2.0"  # the manifest version at install time
    installed_dir = layout["skills_dir"] / "demo-skill"
    assert pin.computed_hash == hash_skill_dir(installed_dir)

    # A fresh manager over the install target genuinely discovers it.
    manager = SkillManager(global_dir=layout["skills_dir"])
    assert "demo-skill" in manager.catalogue()
    assert "DEMO BODY" in manager.load_skill_content("demo-skill")


def test_install_multi_file_skill_copies_everything(layout, capsys):
    code, _, _ = run_cli(
        capsys, *base_args(layout), "install", "multi-file",
        "--source", str(layout["source"]))
    assert code == 0
    notes = layout["skills_dir"] / "multi-file" / "references" / "NOTES.md"
    assert notes.read_text(encoding="utf-8") == "reference payload\n"


def test_install_replay_from_lockfile_is_reproducible(layout, capsys):
    code, _, _ = run_cli(
        capsys, *base_args(layout), "install", "demo-skill",
        "--source", str(layout["source"]))
    assert code == 0
    # Simulate a fresh machine: wipe the install target, replay pins.
    import shutil
    shutil.rmtree(layout["skills_dir"])
    code, out, _ = run_cli(capsys, *base_args(layout), "install")
    assert code == 0
    assert "demo-skill" in out
    manager = SkillManager(global_dir=layout["skills_dir"])
    assert "DEMO BODY" in manager.load_skill_content("demo-skill")


def test_install_is_idempotent(layout, capsys):
    args = [*base_args(layout), "install", "demo-skill",
            "--source", str(layout["source"])]
    assert run_cli(capsys, *args)[0] == 0
    code, out, _ = run_cli(capsys, *args)
    assert code == 0
    assert "already" in out.lower()
    code, out, _ = run_cli(capsys, *base_args(layout), "install")
    assert code == 0
    assert "already" in out.lower()


def test_install_refuses_to_clobber_hand_placed_skill(layout, capsys):
    target = layout["skills_dir"] / "demo-skill"
    target.mkdir(parents=True)
    hand_made = DEMO_SKILL_MD.replace("DEMO BODY", "HAND-MADE BODY")
    (target / "SKILL.md").write_text(hand_made, encoding="utf-8")

    code, _, err = run_cli(
        capsys, *base_args(layout), "install", "demo-skill",
        "--source", str(layout["source"]))
    assert code != 0
    assert "demo-skill" in err
    # Untouched, and no pin was recorded for content we did not install.
    assert (target / "SKILL.md").read_text(encoding="utf-8") == hand_made
    assert load_lockfile(layout["lockfile"]) == {}


def test_install_reports_drift_and_never_overwrites(layout, capsys):
    assert run_cli(
        capsys, *base_args(layout), "install", "demo-skill",
        "--source", str(layout["source"]))[0] == 0
    installed = layout["skills_dir"] / "demo-skill" / "SKILL.md"
    installed.write_text(DEMO_SKILL_MD + "\nlocal edit\n", encoding="utf-8")

    code, _, err = run_cli(capsys, *base_args(layout), "install")
    assert code != 0
    assert "demo-skill" in err
    assert "local edit" in installed.read_text(encoding="utf-8")


def test_install_replay_fails_when_source_no_longer_matches_pin(layout, capsys):
    assert run_cli(
        capsys, *base_args(layout), "install", "demo-skill",
        "--source", str(layout["source"]))[0] == 0
    import shutil
    shutil.rmtree(layout["skills_dir"])
    # The source moved after pinning: the pin cannot be reproduced.
    src_file = layout["source"] / "demo-skill" / "SKILL.md"
    src_file.write_text(DEMO_SKILL_MD + "\nsource moved on\n", encoding="utf-8")
    code, _, err = run_cli(capsys, *base_args(layout), "install")
    assert code != 0
    assert "demo-skill" in err
    assert not (layout["skills_dir"] / "demo-skill").exists()


def test_install_unknown_skill_name_fails_loudly(layout, capsys):
    code, _, err = run_cli(
        capsys, *base_args(layout), "install", "no-such-skill",
        "--source", str(layout["source"]))
    assert code != 0
    assert "no-such-skill" in err


# ---------------------------------------------------------------------------
# CLI: list truthfulness
# ---------------------------------------------------------------------------

def test_list_reports_missing_installed_and_drifted(layout, capsys):
    # Pin all three states up front by installing, then mutating.
    for name in ("demo-skill", "other-skill"):
        assert run_cli(
            capsys, *base_args(layout), "install", name,
            "--source", str(layout["source"]))[0] == 0
    import shutil
    shutil.rmtree(layout["skills_dir"] / "other-skill")  # pinned, missing
    edited = layout["skills_dir"] / "demo-skill" / "SKILL.md"
    edited.write_text(DEMO_SKILL_MD + "\ndrift\n", encoding="utf-8")

    code, out, _ = run_cli(capsys, *base_args(layout), "list")
    assert code == 0
    assert "other-skill: missing" in out
    assert "demo-skill: drifted" in out

    # Repair the drift: list must flip back to installed.
    edited.write_text(DEMO_SKILL_MD, encoding="utf-8")
    code, out, _ = run_cli(capsys, *base_args(layout), "list")
    assert code == 0
    assert "demo-skill: installed" in out


def test_list_with_no_lockfile_is_empty_and_ok(layout, capsys):
    code, out, _ = run_cli(capsys, *base_args(layout), "list")
    assert code == 0
    assert "demo-skill" not in out


# ---------------------------------------------------------------------------
# CLI: search over a source catalogue
# ---------------------------------------------------------------------------

def test_search_matches_name_and_description(layout, capsys):
    code, out, _ = run_cli(
        capsys, "search", "reporting", "--source", str(layout["source"]))
    assert code == 0
    assert "other-skill" in out
    assert "demo-skill" not in out

    code, out, _ = run_cli(
        capsys, "search", "demo", "--source", str(layout["source"]))
    assert code == 0
    assert "demo-skill" in out
    assert "demonstration" in out.lower()


def test_search_no_match_is_empty_success(layout, capsys):
    code, out, _ = run_cli(
        capsys, "search", "zzz-nothing", "--source", str(layout["source"]))
    assert code == 0
    assert "skill" not in out.lower()


def test_search_missing_source_fails_loudly(layout, capsys, tmp_path):
    code, _, err = run_cli(
        capsys, "search", "demo", "--source", str(tmp_path / "no-such-dir"))
    assert code != 0
    assert err.strip() != ""


def test_github_source_fails_loudly_never_fabricates(layout, capsys):
    code, out, err = run_cli(
        capsys, "search", "demo", "--source", "emilkowalski/skill",
        "--source-type", "github")
    assert code != 0
    assert "demo-skill" not in out  # no fabricated catalogue
    assert err.strip() != ""

    code, _, err = run_cli(
        capsys, *base_args(layout), "install", "emil-design-eng",
        "--source", "emilkowalski/skill", "--source-type", "github")
    assert code != 0
    assert err.strip() != ""
    assert load_lockfile(layout["lockfile"]) == {}


# ---------------------------------------------------------------------------
# Scopes + the real process entry point
# ---------------------------------------------------------------------------

def test_project_scope_uses_project_lockfile_and_agents_skills(tmp_path, capsys):
    source = make_source(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    code, _, _ = run_cli(
        capsys, "--project", str(project), "install", "demo-skill",
        "--source", str(source))
    assert code == 0
    assert (project / "skills.lock.json").exists()
    installed_root = project / ".agents" / "skills"
    assert (installed_root / "demo-skill" / "SKILL.md").exists()
    manager = SkillManager(global_dir=tmp_path / "empty-global",
                           workspace_dirs=[installed_root])
    assert "DEMO BODY" in manager.load_skill_content("demo-skill")


def test_console_entry_point_via_python_module(tmp_path):
    source = make_source(tmp_path)
    lockfile = tmp_path / "skills.lock.json"
    skills_dir = tmp_path / "installed"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-m", "aether_engine.skills.cli",
         "--lockfile", str(lockfile), "--skills-dir", str(skills_dir),
         "install", "demo-skill", "--source", str(source)],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT,
        timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr
    assert (skills_dir / "demo-skill" / "SKILL.md").exists()
    assert set(load_lockfile(lockfile)) == {"demo-skill"}
