"""Wave 2, N-03 [S2]: skill install path traversal.

``skills/skill_registry.py`` joined the skill name straight into
filesystem paths (``skills_root / name``, ``staging_root / name``) and
the lockfile accepted any dict key as a pin name, so a crafted
lockfile/manifest with ``name="../../victim"`` made install
``rmtree`` an arbitrary directory (via the staging path) and write
attacker skill content outside the skills root. Plugins got
``SAFE_NAME_RE`` (plugins/manifest.py) for exactly this reason;
skills never did. These tests pin the fence at every entry point —
lockfile load, ``install_named``, ``_materialise``, ``pin_status`` —
plus a healthy-name control proving installs still work.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aether_engine.skills import skill_registry
from aether_engine.skills.lockfile import (
    SkillLockError,
    SkillPin,
    hash_skill_dir,
    load_lockfile,
    save_lockfile,
)
from aether_engine.skills.sources import LocalDirectorySource, SkillSourceError


def _make_source(tmp_path: Path, declared_name: str) -> LocalDirectorySource:
    """A local source whose one skill declares ``declared_name``.

    The directory name is neutral; the *manifest* name is what the
    registry is asked for and what gets joined into install paths.
    """
    root = tmp_path / "source"
    skill_dir = root / "payload"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {declared_name}\ndescription: demo\n---\n\nBody.\n",
        encoding="utf-8")
    (skill_dir / "manifest.json").write_text(
        json.dumps({"name": declared_name, "version": "1.0.0"}),
        encoding="utf-8")
    return LocalDirectorySource(root)


def _victim(tmp_path: Path) -> Path:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("precious", encoding="utf-8")
    return victim


def test_lockfile_rejects_traversal_pin_name(tmp_path):
    lockfile = tmp_path / "skills.lock.json"
    data = {
        "version": 1,
        "skills": {
            "../../victim": {
                "source": "local",
                "sourceType": "local",
                "computedHash": "0" * 64,
            }
        },
    }
    lockfile.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SkillLockError):
        load_lockfile(lockfile)


def test_install_named_refuses_write_outside_skills_root(tmp_path):
    # ``../victim`` as the target resolves OUTSIDE the skills root:
    # an unfixed install materialises attacker content there.
    source = _make_source(tmp_path, "../victim")
    skills_root = tmp_path / "skills"
    outcome = skill_registry.install_named(
        "../victim", source,
        source_type="local", source_locator=str(tmp_path / "source"),
        lockfile_path=tmp_path / "skills.lock.json",
        skills_root=skills_root)
    assert not outcome.ok
    assert not (tmp_path / "victim").exists()


def test_install_named_spares_staging_victim(tmp_path):
    # ``../../victim`` as the STAGING path (skills_root/.aether-staging
    # + name) resolves onto the victim directory: an unfixed install
    # rmtree()s it before copying the attacker's content in.
    victim = _victim(tmp_path)
    source = _make_source(tmp_path, "../../victim")
    outcome = skill_registry.install_named(
        "../../victim", source,
        source_type="local", source_locator=str(tmp_path / "source"),
        lockfile_path=tmp_path / "skills.lock.json",
        skills_root=tmp_path / "skills")
    assert not outcome.ok
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "precious"


def test_materialise_refuses_unsafe_name(tmp_path):
    victim = _victim(tmp_path)
    source = _make_source(tmp_path, "../../victim")
    source_dir = source.fetch("../../victim")
    with pytest.raises(SkillSourceError):
        skill_registry._materialise(
            "../../victim", source_dir, hash_skill_dir(source_dir),
            tmp_path / "skills")
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "precious"


def test_install_pin_refuses_traversal_name(tmp_path):
    source = _make_source(tmp_path, "../victim")
    source_dir = source.fetch("../victim")
    pin = SkillPin(
        name="../victim", source=str(tmp_path / "source"),
        source_type="local", ref=None, skill_path=None,
        computed_hash=hash_skill_dir(source_dir))
    outcome = skill_registry.install_pin(pin, tmp_path / "skills")
    assert not outcome.ok
    assert not (tmp_path / "victim").exists()


def test_pin_status_never_reads_outside_root(tmp_path):
    _victim(tmp_path)
    # The skills root must exist, or the ``..`` never resolves and
    # even the unfixed code reports "missing" by accident.
    (tmp_path / "skills").mkdir()
    pin = SkillPin(
        name="../victim", source="local", source_type="local",
        ref=None, skill_path=None, computed_hash="0" * 64)
    # Must not report the victim dir as an installed/drifted skill —
    # and must not raise either: status is a read-only report.
    assert skill_registry.pin_status(pin, tmp_path / "skills") == (
        skill_registry.STATUS_MISSING)


def test_healthy_name_still_installs(tmp_path):
    source = _make_source(tmp_path, "demo-skill")
    skills_root = tmp_path / "skills"
    outcome = skill_registry.install_named(
        "demo-skill", source,
        source_type="local", source_locator=str(tmp_path / "source"),
        lockfile_path=tmp_path / "skills.lock.json",
        skills_root=skills_root)
    assert outcome.ok, outcome
    assert (skills_root / "demo-skill" / "SKILL.md").exists()
    pins = load_lockfile(tmp_path / "skills.lock.json")
    assert "demo-skill" in pins
    # save_lockfile round-trips the healthy pin unchanged.
    save_lockfile(tmp_path / "skills.lock.json", pins)
    assert "demo-skill" in load_lockfile(tmp_path / "skills.lock.json")
