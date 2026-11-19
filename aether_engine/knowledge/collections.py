"""Owner collections: configuration + indexing jobs (P4d, step 3).

A **collection** is a named set of folders the owner adds. This
module is the engine-level management layer a UI (or any other
caller) drives — there is deliberately no UI in this programme:

* the **config surface** is ``collections.json`` in the knowledge
  dir, recording each collection's folders;
* :func:`create_collection` and :func:`add_folder` are the
  add-folder flow: register the folder, then run the ingestion job
  (:func:`aether_engine.knowledge.ingest.index_collection`,
  incremental by file hash) over all of the collection's folders
  — with one documented exception: growing a collection from one
  folder to several re-indexes the first folder once, because its
  files' identities gain the folder prefix (see
  ``knowledge.ingest``);
* :func:`list_collections` / :func:`get_collection` report stats
  (folders, docs, chunks, embedder, last indexed) merged from the
  registry and each collection's index manifest — the stats the
  plan has the engine surface.

Indexing stays **owner-triggered**: nothing here watches folders
or indexes in the background, and registering a folder never reads
it until the job runs as part of the same call the owner made.

The registry is the owner's *declared* collections. An index can
also exist without a registry entry (built by calling
``index_collection`` directly, as the slice's own tests do); such
an index is still searchable through ``search_knowledge_base`` but
is not listed here — this module manages declared collections,
it does not invent declarations for indexes it did not create.

Removing a collection removes its registry entry only; the
persisted index stays on disk (deleting the owner's data is not
this layer's call) and can be re-declared later or removed by hand.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aether_engine.config import UserConfig
from aether_engine.knowledge.ingest import IndexStats, index_collection, load_manifest

logger = logging.getLogger("aether_engine.knowledge.collections")

#: The registry file, at the root of the knowledge dir.
REGISTRY_NAME = "collections.json"

# Collection names become directory names under the knowledge dir,
# so they are single safe path segments: letters, digits, spaces,
# '-' and '_', starting with a letter or digit. Anything else
# (separators, dots, '..') is refused rather than sanitised —
# a renamed collection is a surprise, a refused name is a message.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]*$")


class UnknownCollectionError(RuntimeError):
    """No collection with that name is registered."""


class CollectionExistsError(RuntimeError):
    """A collection with that name is already registered."""


@dataclass
class CollectionInfo:
    """One registered collection, registry + index manifest merged."""

    name: str
    folders: list[str] = field(default_factory=list)
    indexed: bool = False
    embedder: str | None = None
    indexed_at: str | None = None
    doc_count: int = 0
    chunk_count: int = 0


def default_knowledge_dir() -> Path:
    """Where collections live: ``~/.aether/knowledge/``."""
    return Path.home() / ".aether" / "knowledge"


def _resolve_knowledge_dir(knowledge_dir: Path | None) -> Path:
    return Path(knowledge_dir).expanduser() if knowledge_dir else default_knowledge_dir()


def validate_collection_name(name: str) -> str:
    """The collection name, cleaned — or ValueError saying why not."""
    cleaned = (name or "").strip()
    if not cleaned or not _NAME_RE.match(cleaned):
        raise ValueError(
            f"invalid collection name {name!r}: use letters, digits, "
            f"spaces, '-' or '_', starting with a letter or digit — "
            f"the name becomes a folder name and cannot contain "
            f"path separators or dots.")
    return cleaned


def _validate_folder(folder: str | Path) -> str:
    """The folder as an absolute path string — or an honest error."""
    path = Path(folder).expanduser()
    if not path.is_dir():
        raise FileNotFoundError(f"folder does not exist: {path}")
    return str(path.resolve())


def _registry_path(knowledge_dir: Path) -> Path:
    return knowledge_dir / REGISTRY_NAME


def load_registry(knowledge_dir: Path | None = None) -> dict[str, Any]:
    """The raw collections registry (``{}`` when none exists yet)."""
    path = _registry_path(_resolve_knowledge_dir(knowledge_dir))
    if not path.is_file():
        return {"version": 1, "collections": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "collections": {}}
    if not isinstance(data, dict) or not isinstance(data.get("collections"), dict):
        return {"version": 1, "collections": {}}
    return data


def _save_registry(knowledge_dir: Path, registry: dict[str, Any]) -> None:
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    _registry_path(knowledge_dir).write_text(
        json.dumps(registry, indent=2), encoding="utf-8")


def _entry(registry: dict[str, Any], name: str) -> dict[str, Any]:
    entry = registry.get("collections", {}).get(name)
    if entry is None:
        known = ", ".join(sorted(registry.get("collections", {}))) or "none"
        raise UnknownCollectionError(
            f"no knowledge collection named {name!r} is registered "
            f"(registered: {known}).")
    return entry


def _info(knowledge_dir: Path, name: str, entry: dict[str, Any]) -> CollectionInfo:
    manifest = load_manifest(knowledge_dir / name)
    info = CollectionInfo(
        name=name,
        folders=[str(f) for f in entry.get("folders") or []],
        indexed=manifest is not None,
    )
    if manifest is not None:
        info.embedder = manifest.get("embedder")
        info.indexed_at = manifest.get("indexed_at")
        info.doc_count = int(manifest.get("doc_count") or 0)
        info.chunk_count = int(manifest.get("chunk_count") or 0)
    return info


def list_collections(knowledge_dir: Path | None = None) -> list[CollectionInfo]:
    """Every registered collection with its stats, sorted by name."""
    root = _resolve_knowledge_dir(knowledge_dir)
    registry = load_registry(root)
    return [
        _info(root, name, registry["collections"][name])
        for name in sorted(registry["collections"])
    ]


def get_collection(name: str, knowledge_dir: Path | None = None) -> CollectionInfo:
    """One registered collection's stats (UnknownCollectionError)."""
    root = _resolve_knowledge_dir(knowledge_dir)
    registry = load_registry(root)
    return _info(root, name, _entry(registry, name))


async def _run_index_job(
    name: str,
    folders: list[str],
    *,
    knowledge_dir: Path,
    config: UserConfig | None,
    force: bool = False,
) -> IndexStats:
    """Run the ingestion job for a collection and refresh readers.

    The search tool caches loaded collections; a re-index changes
    the manifest (last-indexed time, counts), so the cache is
    dropped — a stale answer must stay detectable as stale, never
    silently served from a pre-reindex snapshot.
    """
    stats = await index_collection(
        folders, knowledge_dir / name, config=config, force=force)
    from aether_engine.tools.knowledge_search import clear_knowledge_base_cache

    clear_knowledge_base_cache()
    return stats


async def create_collection(
    name: str,
    folders: str | Path | list[str | Path],
    *,
    knowledge_dir: Path | None = None,
    config: UserConfig | None = None,
) -> IndexStats:
    """Register a new collection and run its first indexing job.

    ``folders`` is one folder or the list forming the collection.
    Returns the job's stats; per-file failures are in
    ``stats.outcomes``, never hidden.
    """
    root = _resolve_knowledge_dir(knowledge_dir)
    name = validate_collection_name(name)
    if isinstance(folders, (str, Path)):
        folders = [folders]
    resolved = [_validate_folder(f) for f in folders]
    if not resolved:
        raise ValueError("a collection needs at least one folder.")
    registry = load_registry(root)
    if name in registry["collections"]:
        raise CollectionExistsError(
            f"a knowledge collection named {name!r} already exists; "
            f"add the folder to it with add_folder instead.")
    now = datetime.now(UTC).isoformat()
    registry["collections"][name] = {
        "folders": resolved, "created_at": now, "updated_at": now,
    }
    _save_registry(root, registry)
    return await _run_index_job(name, resolved, knowledge_dir=root, config=config)


async def add_folder(
    name: str,
    folder: str | Path,
    *,
    knowledge_dir: Path | None = None,
    config: UserConfig | None = None,
) -> IndexStats:
    """Add a folder to a collection and re-index incrementally.

    Adding a folder that is already registered changes nothing in
    the config; the job still runs (it is incremental, so an
    unchanged collection costs a hash pass, not a re-embedding).
    """
    root = _resolve_knowledge_dir(knowledge_dir)
    registry = load_registry(root)
    entry = _entry(registry, name)
    resolved = _validate_folder(folder)
    folders = [str(f) for f in entry.get("folders") or []]
    if resolved not in folders:
        folders.append(resolved)
        entry["folders"] = folders
        entry["updated_at"] = datetime.now(UTC).isoformat()
        _save_registry(root, registry)
    return await _run_index_job(name, folders, knowledge_dir=root, config=config)


async def remove_folder(
    name: str,
    folder: str | Path,
    *,
    knowledge_dir: Path | None = None,
    config: UserConfig | None = None,
) -> IndexStats:
    """Remove a folder from a collection and re-index.

    The removed folder's documents leave the index in the same job
    (the incremental pass drops files it no longer sees). A
    collection must keep at least one folder — remove the whole
    collection instead of emptying it.
    """
    root = _resolve_knowledge_dir(knowledge_dir)
    registry = load_registry(root)
    entry = _entry(registry, name)
    resolved = str(Path(folder).expanduser().resolve())
    folders = [str(f) for f in entry.get("folders") or []]
    if resolved not in folders:
        raise ValueError(
            f"folder {resolved!r} is not part of collection {name!r} "
            f"(its folders: {', '.join(folders) or 'none'}).")
    remaining = [f for f in folders if f != resolved]
    if not remaining:
        raise ValueError(
            f"removing {resolved!r} would leave collection {name!r} "
            f"with no folders; remove the collection instead.")
    entry["folders"] = remaining
    entry["updated_at"] = datetime.now(UTC).isoformat()
    _save_registry(root, registry)
    return await _run_index_job(name, remaining, knowledge_dir=root, config=config)


async def reindex_collection(
    name: str,
    *,
    knowledge_dir: Path | None = None,
    config: UserConfig | None = None,
    force: bool = False,
) -> IndexStats:
    """Re-run a collection's indexing job over its registered folders.

    Incremental by file hash unless ``force=True`` — the required
    move after an embedder change (see
    :class:`aether_engine.knowledge.ingest.EmbedderMismatchError`).
    """
    root = _resolve_knowledge_dir(knowledge_dir)
    registry = load_registry(root)
    entry = _entry(registry, name)
    folders = [str(f) for f in entry.get("folders") or []]
    return await _run_index_job(
        name, folders, knowledge_dir=root, config=config, force=force)


def remove_collection(name: str, knowledge_dir: Path | None = None) -> None:
    """Unregister a collection (its index stays on disk — see module
    docstring). UnknownCollectionError when it was never registered."""
    root = _resolve_knowledge_dir(knowledge_dir)
    registry = load_registry(root)
    _entry(registry, name)
    del registry["collections"][name]
    _save_registry(root, registry)
