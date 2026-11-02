"""Skill registry workflow — install / list / search (Epic 7, E7-F09).

The operations behind the CLI (cli.py), over the lockfile
(lockfile.py) and the sources (sources.py):

- ``install_named`` resolves one skill from a source, pins it in the
  lockfile (name + source + ref + content hash) and materialises it
  into the skills root a SkillManager scans.
- ``install_locked`` replays every pin in the lockfile — the
  reproducible path: a fresh machine with the same lockfile and the
  same sources gets byte-identical skills, or a loud failure.
- ``pin_status`` / ``list_pins`` report each pin truthfully:
  installed (content matches the pin), missing, or drifted (present
  but different — local edits or foreign content).
- ``search_source`` queries a source's catalogue.

No silent overwrite, anywhere (the E7-F08 rule, applied to plain
skills): if the target directory already holds DIFFERENT content —
hand-placed, locally edited, or an older pin's pristine copy — the
install is refused and reported; the existing content is never
touched and the pin is never updated past a failed materialisation.
Identical content collapses quietly into "already installed".
Changing a skill's version is therefore deliberately manual in v1:
remove the installed copy, then install again. There is no separate
update command in the promised install/search/list surface.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from aether_engine.skills.lockfile import (
    SkillPin,
    hash_skill_dir,
    load_lockfile,
    save_lockfile,
)
from aether_engine.skills.sources import (
    CatalogueEntry,
    SkillSource,
    SkillSourceError,
    manifest_for,
    open_source,
)

# Pin states reported by `list`.
STATUS_INSTALLED = "installed"
STATUS_MISSING = "missing"
STATUS_DRIFTED = "drifted"


@dataclass(frozen=True)
class InstallOutcome:
    name: str
    ok: bool
    action: str  # installed | already-installed | drifted | refused | failed
    detail: str


def pin_status(pin: SkillPin, skills_root: Path) -> str:
    """Truthful state of one pin against the install target."""
    target = skills_root / pin.name
    if not target.is_dir():
        return STATUS_MISSING
    if hash_skill_dir(target) == pin.computed_hash:
        return STATUS_INSTALLED
    return STATUS_DRIFTED


def list_pins(lockfile_path: Path, skills_root: Path) -> list[tuple[SkillPin, str]]:
    pins = load_lockfile(lockfile_path)
    return [(pin, pin_status(pin, skills_root)) for pin in pins.values()]


def search_source(source: SkillSource, query: str) -> list[CatalogueEntry]:
    """Case-insensitive substring search over name + description."""
    needle = query.strip().lower()
    entries = source.catalogue()
    if not needle:
        return entries
    return [
        e for e in entries
        if needle in e.name.lower() or needle in e.description.lower()
    ]


def _materialise(
    name: str,
    source_dir: Path,
    expected_hash: str,
    skills_root: Path,
) -> str:
    """Place ``source_dir``'s content at ``skills_root/name``.

    Returns "installed" or "already-installed". Raises
    SkillSourceError (refusal) when the target holds different
    content — it is left exactly as found.
    """
    target = skills_root / name
    if target.exists():
        if target.is_dir() and hash_skill_dir(target) == expected_hash:
            return "already-installed"
        raise SkillSourceError(
            f"refusing to overwrite {target}: it already contains "
            f"different content for skill {name!r} (hand-placed, "
            "locally edited, or another version). Remove it manually "
            "if you really want to replace it.")
    skills_root.mkdir(parents=True, exist_ok=True)
    # Stage inside a dot-directory the SkillManager never scans (it
    # only reads SKILL.md/manifest.json at a skill dir's top level),
    # verify the staged copy, then rename into place atomically.
    staging_root = skills_root / ".aether-staging"
    staging = staging_root / name
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(source_dir, staging, dirs_exist_ok=True)
        staged_hash = hash_skill_dir(staging)
        if staged_hash != expected_hash:
            raise SkillSourceError(
                f"staged copy of skill {name!r} failed verification "
                f"(expected hash {expected_hash}, got {staged_hash}); "
                "nothing was installed")
        os.replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        try:
            staging_root.rmdir()  # only succeeds when empty
        except OSError:
            pass
    return "installed"


def install_pin(
    pin: SkillPin,
    skills_root: Path,
    *,
    lockfile_dir: Path | None = None,
) -> InstallOutcome:
    """Materialise one existing pin (the reproducible replay path)."""
    try:
        source = open_source(pin.source_type, pin.source, base_dir=lockfile_dir)
        source_dir = source.fetch(pin.name, pin.skill_path)
    except SkillSourceError as exc:
        return InstallOutcome(pin.name, False, "failed", str(exc))
    source_hash = hash_skill_dir(source_dir)
    if source_hash != pin.computed_hash:
        return InstallOutcome(
            pin.name, False, "failed",
            f"source {pin.source!r} no longer matches the pin "
            f"(pin hash {pin.computed_hash}, source now hashes to "
            f"{source_hash}); the pinned content cannot be reproduced "
            "from this source")
    status = pin_status(pin, skills_root)
    if status == STATUS_INSTALLED:
        return InstallOutcome(pin.name, True, "already-installed",
                              "content matches the pin")
    if status == STATUS_DRIFTED:
        return InstallOutcome(
            pin.name, False, "drifted",
            f"installed copy at {skills_root / pin.name} differs from "
            "the pin (local edits or foreign content); reported, "
            "not overwritten")
    try:
        action = _materialise(pin.name, source_dir, pin.computed_hash, skills_root)
    except SkillSourceError as exc:
        return InstallOutcome(pin.name, False, "refused", str(exc))
    return InstallOutcome(pin.name, True, action, "installed from pin")


def install_locked(lockfile_path: Path, skills_root: Path) -> list[InstallOutcome]:
    """Replay every pin in the lockfile, in name order."""
    pins = load_lockfile(lockfile_path)
    return [
        install_pin(pin, skills_root, lockfile_dir=lockfile_path.parent)
        for _, pin in sorted(pins.items())
    ]


def install_named(
    name: str,
    source: SkillSource,
    *,
    source_type: str,
    source_locator: str,
    lockfile_path: Path,
    skills_root: Path,
    ref: str | None = None,
    skill_path: str | None = None,
) -> InstallOutcome:
    """Resolve ``name`` from ``source``, pin it, and materialise it.

    The pin is written only AFTER the content is in place (or proven
    already in place), so the lockfile never claims content the disk
    does not hold.
    """
    try:
        source_dir = source.fetch(name, skill_path)
    except SkillSourceError as exc:
        return InstallOutcome(name, False, "failed", str(exc))
    content_hash = hash_skill_dir(source_dir)
    try:
        action = _materialise(name, source_dir, content_hash, skills_root)
    except SkillSourceError as exc:
        return InstallOutcome(name, False, "refused", str(exc))
    manifest = manifest_for(source_dir)
    pin = SkillPin(
        name=name,
        source=source_locator,
        source_type=source_type,
        ref=ref if ref is not None else (manifest.version if manifest else None),
        skill_path=skill_path,
        computed_hash=content_hash,
    )
    pins = load_lockfile(lockfile_path)
    pins[name] = pin
    save_lockfile(lockfile_path, pins)
    return InstallOutcome(name, True, action, f"pinned at hash {content_hash}")
