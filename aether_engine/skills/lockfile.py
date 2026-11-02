"""Pinned skill lockfile — ``skills.lock.json`` (Epic 7, E7-F09).

The owner-approved plan addendum (omp #28) promises a skill registry
CLI with install/search/list over a *pinned, reproducible* lockfile.
This module is the lockfile half: the on-disk format, its reader and
writer, and the content hash a pin is anchored to.

Format (deliberately the same family as the tracked developer
artifact ``skills-lock.json`` — same field names — so the two are
recognisably one format; the product file adds ``ref``):

    {
      "version": 1,
      "skills": {
        "<name>": {
          "source": "<locator>",
          "sourceType": "local" | "github",
          "ref": "<version or ref the pin was resolved at>",
          "skillPath": "<optional subpath inside the source>",
          "computedHash": "<sha256 digest of the whole skill dir>"
        }
      }
    }

``computedHash`` covers EVERY file in the skill directory (Aether
skills bundle scripts and reference files, not just SKILL.md): it is
the sha256 of the sorted stream of (relative path, bytes) pairs.
Two trees with the same digest are byte-identical in structure and
content, which is what makes a pin reproducible — and drift from it
detectable.

Failure discipline (the house rule, cf. E6-F07 / E7-F04): a missing
lockfile is simply empty (install creates it), but a lockfile that
exists and cannot be parsed or validated is REFUSED loudly via
SkillLockError — never silently treated as empty, and never modified
by the failed read. Saves are atomic (temp file + fsync + replace),
so an interrupted write leaves the previous valid lockfile intact.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

LOCKFILE_NAME = "skills.lock.json"
LOCKFILE_VERSION = 1


class SkillLockError(Exception):
    """The lockfile exists but cannot be trusted, or cannot be saved."""


@dataclass(frozen=True)
class SkillPin:
    """One pinned skill: where it came from + what its content is."""

    name: str
    source: str
    source_type: str
    ref: str | None
    skill_path: str | None
    computed_hash: str


def hash_skill_dir(skill_dir: Path) -> str:
    """Digest of a whole skill directory (see module docstring)."""
    digest = hashlib.sha256()
    for file_path in sorted(
        (p for p in skill_dir.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(skill_dir).as_posix(),
    ):
        rel = file_path.relative_to(skill_dir).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_lockfile(path: Path) -> dict[str, SkillPin]:
    """Read pins from ``path``. Missing file -> {}. Corrupt -> raise."""
    if not path.exists():
        return {}
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SkillLockError(f"could not read lockfile {path}: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillLockError(
            f"lockfile {path} is not valid JSON ({exc}); "
            "refusing to treat it as empty") from exc
    if not isinstance(data, dict) or data.get("version") != LOCKFILE_VERSION:
        raise SkillLockError(
            f"lockfile {path} has an unsupported shape or version; "
            f"expected version {LOCKFILE_VERSION}")
    skills = data.get("skills")
    if not isinstance(skills, dict):
        raise SkillLockError(
            f"lockfile {path}: 'skills' must be an object mapping "
            "skill names to pins")
    pins: dict[str, SkillPin] = {}
    for name, entry in skills.items():
        pins[name] = _parse_pin(path, name, entry)
    return pins


def _parse_pin(path: Path, name: str, entry: object) -> SkillPin:
    if not isinstance(entry, dict):
        raise SkillLockError(
            f"lockfile {path}: pin for {name!r} is not an object")
    source = entry.get("source")
    source_type = entry.get("sourceType")
    computed_hash = entry.get("computedHash")
    ref = entry.get("ref")
    skill_path = entry.get("skillPath")
    if not isinstance(source, str) or not source:
        raise SkillLockError(
            f"lockfile {path}: pin for {name!r} has no usable 'source'")
    if not isinstance(source_type, str) or not source_type:
        raise SkillLockError(
            f"lockfile {path}: pin for {name!r} has no usable 'sourceType'")
    if not isinstance(computed_hash, str) or not computed_hash:
        raise SkillLockError(
            f"lockfile {path}: pin for {name!r} has no 'computedHash' — "
            "an unpinned entry is not a pin")
    if ref is not None and not isinstance(ref, str):
        raise SkillLockError(
            f"lockfile {path}: pin for {name!r} has a non-string 'ref'")
    if skill_path is not None and not isinstance(skill_path, str):
        raise SkillLockError(
            f"lockfile {path}: pin for {name!r} has a non-string 'skillPath'")
    return SkillPin(
        name=name, source=source, source_type=source_type, ref=ref,
        skill_path=skill_path, computed_hash=computed_hash)


def save_lockfile(path: Path, pins: dict[str, SkillPin]) -> None:
    """Write pins atomically (temp + fsync + replace, as E6-F07/E7-F04)."""
    data = {
        "version": LOCKFILE_VERSION,
        "skills": {
            name: _pin_to_json(pin)
            for name, pin in sorted(pins.items())
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise SkillLockError(f"could not save lockfile {path}: {exc}") from exc


def _pin_to_json(pin: SkillPin) -> dict[str, str]:
    entry = {
        "source": pin.source,
        "sourceType": pin.source_type,
        "computedHash": pin.computed_hash,
    }
    if pin.ref is not None:
        entry["ref"] = pin.ref
    if pin.skill_path is not None:
        entry["skillPath"] = pin.skill_path
    # Stable, human-diffable key order matching the developer artifact.
    ordered = {"source": entry["source"], "sourceType": entry["sourceType"]}
    if "ref" in entry:
        ordered["ref"] = entry["ref"]
    if "skillPath" in entry:
        ordered["skillPath"] = entry["skillPath"]
    ordered["computedHash"] = entry["computedHash"]
    return ordered
