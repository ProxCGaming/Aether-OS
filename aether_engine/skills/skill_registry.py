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
    LOCKFILE_NAME,
    SkillLockError,
    SkillPin,
    hash_skill_dir,
    is_safe_skill_name,
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


def _unsafe_name_outcome(name: str) -> InstallOutcome:
    return InstallOutcome(
        name, False, "refused",
        f"refusing skill name {name!r}: a skill name must be a "
        "single path segment (letters, digits, '.', '_' or '-', not "
        "starting with '.') — it is joined into filesystem paths at "
        "install time, so a name carrying separators or '..' is a "
        "traversal attempt, never a skill")


def pin_status(pin: SkillPin, skills_root: Path) -> str:
    """Truthful state of one pin against the install target."""
    if not is_safe_skill_name(pin.name):
        # N-03: never resolve an unsafe name against the filesystem —
        # ``skills_root / "../victim"`` would report on a directory
        # outside the root. An unsafe pin cannot be installed, so it
        # is missing by definition.
        return STATUS_MISSING
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
    if not is_safe_skill_name(name):
        # N-03: the last fence before the path joins below — both
        # ``target`` and ``staging`` are built from the raw name, and
        # the staging branch rmtree()s what it finds there.
        raise SkillSourceError(
            f"refusing to install skill {name!r}: the name is not a "
            "single safe path segment, so it cannot be joined into "
            "the skills root")
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
    if not is_safe_skill_name(pin.name):
        return _unsafe_name_outcome(pin.name)
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
    expected_hash: str | None = None,
) -> InstallOutcome:
    """Resolve ``name`` from ``source``, pin it, and materialise it.

    The pin is written only AFTER the content is in place (or proven
    already in place), so the lockfile never claims content the disk
    does not hold.

    ``expected_hash`` (Wave 5, D4-6): the content hash the user
    reviewed on the install card. When given and the source now
    hashes to something else, the install is refused — what gets
    installed is never different from what was approved.
    """
    if not is_safe_skill_name(name):
        return _unsafe_name_outcome(name)
    try:
        source_dir = source.fetch(name, skill_path)
    except SkillSourceError as exc:
        return InstallOutcome(name, False, "failed", str(exc))
    content_hash = hash_skill_dir(source_dir)
    if expected_hash is not None and content_hash != expected_hash:
        return InstallOutcome(
            name, False, "refused",
            f"the source content for skill {name!r} changed since "
            f"it was reviewed (reviewed pin {expected_hash}, source "
            f"now hashes to {content_hash}); search and review it "
            "again before installing")
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


# ---------------------------------------------------------------------------
# Wave 5 (D4-6) — the Settings UI's view of this same workflow.
#
# The panel is a second front-end over the CLI engine above, never a
# second installer: search reads a source's catalogue, prepare
# fetches + hashes WITHOUT writing anything (the pin summary the
# install card shows), and install goes through install_named with
# the reviewed hash. There is deliberately no default source: a
# search without a named source is the CLI's error, preserved.

def search_catalogue(
    source_type: str,
    locator: str,
    query: str,
    *,
    base_dir: Path | None = None,
) -> list[dict]:
    """Catalogue entries of one named source, as plain dicts.

    Raises SkillSourceError with the source's own honest reason
    (a github source in this build, a missing directory) — the
    caller surfaces it verbatim, never as an empty result.
    """
    source = open_source(source_type, locator, base_dir=base_dir)
    return [
        {"name": e.name, "description": e.description, "version": e.version}
        for e in search_source(source, query)
    ]


def prepare_named(
    name: str,
    source_type: str,
    locator: str,
    *,
    base_dir: Path | None = None,
    skill_path: str | None = None,
) -> dict:
    """Fetch + hash one skill WITHOUT installing it (the pin card).

    Returns the summary the install card renders: identity, the
    source it will be pinned from, and the content hash the
    eventual install is checked against. Raises SkillSourceError
    when the source cannot produce the skill.
    """
    if not is_safe_skill_name(name):
        raise SkillSourceError(
            f"refusing skill name {name!r}: a skill name must be a "
            "single path segment (letters, digits, '.', '_' or '-', "
            "not starting with '.')")
    source = open_source(source_type, locator, base_dir=base_dir)
    source_dir = source.fetch(name, skill_path)
    manifest = manifest_for(source_dir)
    return {
        "name": name,
        "description": manifest.description if manifest else "",
        "version": manifest.version if manifest else None,
        "source_type": source_type,
        "source": locator,
        "hash": hash_skill_dir(source_dir),
    }


def _lockfile_scopes(manager) -> list[tuple[Path, Path]]:
    """(skills root, lockfile) pairs for every root a manager scans.

    Mirrors the CLI's scope resolution: the user scope's lockfile
    sits beside the global skills root; a workspace root
    (``<project>/.agents/skills``) is pinned by the project's own
    lockfile at ``<project>/skills.lock.json``.
    """
    scopes: list[tuple[Path, Path]] = []
    global_dir = Path(manager.global_dir)
    scopes.append((global_dir, global_dir.parent / LOCKFILE_NAME))
    for workspace_dir in manager.workspace_dirs or []:
        root = Path(workspace_dir)
        scopes.append((root, root.parent.parent / LOCKFILE_NAME))
    return scopes


def pin_states(manager) -> dict[str, dict]:
    """{skill name: pin summary + status} for the manager's skills.

    A skill is annotated only when a lockfile for ITS root pins it
    — a pin is a fact about one root's lockfile, so the lookup goes
    through the directory the skill actually loaded from. Status
    is the registry's own vocabulary (installed / missing /
    drifted); "drifted" is the panel's "modified vs pin". A scope
    whose lockfile cannot be read contributes no pins (the CLI
    stays the loud surface for a corrupt lockfile; the list itself
    must not break over an annotation).
    """
    pins_by_root: dict[Path, dict[str, tuple[SkillPin, str]]] = {}
    for root, lockfile_path in _lockfile_scopes(manager):
        try:
            pins_by_root[root] = {
                pin.name: (pin, status)
                for pin, status in list_pins(lockfile_path, root)
            }
        except SkillLockError:
            pins_by_root[root] = {}
    states: dict[str, dict] = {}
    for root, pin_map in pins_by_root.items():
        for name, (pin, status) in pin_map.items():
            skill_dir = manager.skill_dir(name)
            if skill_dir is None or Path(skill_dir).parent != root:
                continue
            states[name] = {
                "status": status,
                "source_type": pin.source_type,
                "source": pin.source,
                "ref": pin.ref,
                "hash": pin.computed_hash,
            }
    return states


def annotated_skill_dicts(manager) -> list[dict]:
    """``list_all()`` summaries as dicts, each with its ``pin``.

    The list payload's per-skill pin annotation (D4-6): the pin
    dict from :func:`pin_states`, or None for an unpinned skill —
    unpinned is a normal state (hand-placed skills, plugin-bundled
    skills), rendered as absence, never as an error.
    """
    pins = pin_states(manager)
    return [
        {**summary.__dict__, "pin": pins.get(summary.name)}
        for summary in manager.list_all()
    ]
