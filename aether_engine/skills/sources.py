"""Skill sources — where registry skills come from (Epic 7, E7-F09).

A *source* is anything that can show a catalogue of skills and hand
over one skill's files on request. The workflow (skill_registry.py)
only ever talks to this interface, so a remote registry can slot in
later behind the same two methods without the CLI or the lockfile
changing shape.

Concrete v1 adapters:

- ``LocalDirectorySource`` — a directory holding one sub-directory
  per skill (``<root>/<name>/SKILL.md``), or a directory that is
  itself a single skill. Fully functional offline; this is the
  adapter the reproducible workflow is exercised against.

- ``GitHubSource`` — recognised so lockfiles may pin ``owner/repo``
  sources (as the developer artifact ``skills-lock.json`` does), but
  this build defines NO remote registry endpoint and never assumes
  network access: catalogue/fetch raise SkillSourceError loudly.
  Nothing is ever fabricated — no guessed catalogue, no stub skill.

Offline honesty is a design rule here, not an error path: a source
that cannot be read raises SkillSourceError with the reason, and the
CLI turns that into a non-zero exit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from aether_engine.skills.manifest import SkillManifest

logger = logging.getLogger("aether_engine.skills.sources")


class SkillSourceError(Exception):
    """A source cannot be read, or does not hold what was asked for."""


@dataclass(frozen=True)
class CatalogueEntry:
    name: str
    description: str
    version: str


class SkillSource(Protocol):
    """The two operations every skill source must provide."""

    def catalogue(self) -> list[CatalogueEntry]:
        """All skills this source offers (parsed, named, described)."""
        ...

    def fetch(self, name: str, skill_path: str | None = None) -> Path:
        """Return the directory holding skill ``name``'s files.

        The returned directory is source-owned; callers copy out of
        it, never mutate it. Raises SkillSourceError if the skill is
        absent or is not the skill that was asked for.
        """
        ...


def manifest_for(skill_dir: Path) -> SkillManifest | None:
    """Parse a skill directory's manifest (SKILL.md wins, then JSON)."""
    md = skill_dir / "SKILL.md"
    if md.exists():
        try:
            return SkillManifest.from_skill_md(md)
        except Exception as exc:  # noqa: BLE001 — discovery precedent: log + skip
            logger.warning("Unparseable SKILL.md at %s: %s", md, exc)
            return None
    manifest_json = skill_dir / "manifest.json"
    if manifest_json.exists():
        try:
            return SkillManifest.from_file(manifest_json)
        except Exception as exc:  # noqa: BLE001 — discovery precedent: log + skip
            logger.warning("Unparseable manifest.json at %s: %s", manifest_json, exc)
            return None
    return None


def _looks_like_skill_dir(path: Path) -> bool:
    return path.is_dir() and (
        (path / "SKILL.md").exists() or (path / "manifest.json").exists())


class LocalDirectorySource:
    """Skills laid out on disk: ``<root>/<name>/SKILL.md`` (or root itself)."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _require_root(self) -> None:
        if not self.root.is_dir():
            raise SkillSourceError(
                f"local skill source {self.root} does not exist or is "
                "not a directory")

    def _candidate_dirs(self) -> list[Path]:
        if _looks_like_skill_dir(self.root):
            return [self.root]
        return sorted(
            (p for p in self.root.iterdir() if _looks_like_skill_dir(p)),
            key=lambda p: p.name)

    def catalogue(self) -> list[CatalogueEntry]:
        self._require_root()
        entries = []
        for skill_dir in self._candidate_dirs():
            manifest = manifest_for(skill_dir)
            if manifest is None:
                continue
            entries.append(CatalogueEntry(
                name=manifest.name,
                description=manifest.description,
                version=manifest.version))
        return entries

    def fetch(self, name: str, skill_path: str | None = None) -> Path:
        self._require_root()
        if skill_path:
            skill_dir = self._resolve_skill_path(skill_path)
        else:
            skill_dir = None
            for candidate in self._candidate_dirs():
                manifest = manifest_for(candidate)
                if manifest is not None and manifest.name == name:
                    skill_dir = candidate
                    break
            if skill_dir is None:
                raise SkillSourceError(
                    f"skill {name!r} not found in local source {self.root}")
        manifest = manifest_for(skill_dir)
        if manifest is None:
            raise SkillSourceError(
                f"skill at {skill_dir} has no readable SKILL.md/manifest.json")
        if manifest.name != name:
            raise SkillSourceError(
                f"source was asked for skill {name!r} but {skill_dir} "
                f"declares {manifest.name!r}; refusing to install a "
                "different skill under the requested name")
        return skill_dir

    def _resolve_skill_path(self, skill_path: str) -> Path:
        resolved = (self.root / skill_path).resolve()
        try:
            resolved.relative_to(self.root.resolve())
        except ValueError:
            raise SkillSourceError(
                f"skillPath {skill_path!r} escapes source {self.root}") from None
        if resolved.is_file():
            resolved = resolved.parent
        if not _looks_like_skill_dir(resolved):
            raise SkillSourceError(
                f"skillPath {skill_path!r} in source {self.root} does "
                "not resolve to a skill directory")
        return resolved


class GitHubSource:
    """A pinned ``owner/repo`` source — recognised, not yet fetchable.

    This build defines no Aether registry endpoint and never assumes
    network access, so every read fails loudly (see module docstring).
    The adapter exists so the lockfile format and the CLI can name
    the source type truthfully instead of pretending it is local.
    """

    def __init__(self, locator: str):
        self.locator = locator

    def _unavailable(self) -> SkillSourceError:
        return SkillSourceError(
            f"github skill source {self.locator!r} cannot be read: this "
            "build has no remote registry access (no registry endpoint "
            "is configured and network access is not assumed). Pin the "
            "skill from a local source, or add registry support before "
            "installing from github sources.")

    def catalogue(self) -> list[CatalogueEntry]:
        raise self._unavailable()

    def fetch(self, name: str, skill_path: str | None = None) -> Path:
        raise self._unavailable()


def open_source(
    source_type: str,
    locator: str,
    *,
    base_dir: Path | None = None,
) -> SkillSource:
    """Build the adapter for a (source_type, locator) pair.

    Relative local locators resolve against ``base_dir`` (the
    lockfile's directory) so a project lockfile can name a vendored
    source tree beside it; absolute locators are used as-is.
    """
    if source_type == "local":
        root = Path(locator)
        if not root.is_absolute() and base_dir is not None:
            root = base_dir / root
        return LocalDirectorySource(root)
    if source_type == "github":
        return GitHubSource(locator)
    raise SkillSourceError(
        f"unknown skill source type {source_type!r} "
        "(supported: 'local', 'github')")
