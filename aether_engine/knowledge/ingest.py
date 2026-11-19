"""Ingestion + index for knowledge collections (P4d, steps 1–2).

A collection's index is built from a source folder by LlamaIndex
readers (PDF / Markdown / text / DOCX / code), chunked with
``TokenTextSplitter``, embedded through Aether's one local-first
embedder (``knowledge.embeddings``), and persisted as a
``VectorStoreIndex`` under the collection's directory next to a
``manifest.json``.

The manifest is the index's admin record: which embedder built it,
when it was last indexed, and the sha256 of every indexed file.
Re-running ingestion is **incremental by file hash** — unchanged
files are not re-read or re-embedded, changed files are re-embedded
and swapped in, deleted files are removed. The trigger for a run
(owner action, folder watch) is a later slice (P4d step 3); this
module is the engine-side job it will call.

Embedder identity is part of the contract: an index records the
embedder that built it, and opening or extending an index whose
recorded embedder differs from the configured one raises
:class:`EmbedderMismatchError` — a different embedder is a
different vector space, so the honest move is a full re-index,
never a silent mix. ``force=True`` performs that full re-index.

A file whose chunks cannot be embedded (endpoint down) is recorded
as failed in the run's stats and left out — or, if it was already
indexed, its previous version stays. Nothing is ever stored
unembedded and reported as indexed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from llama_index.core import SimpleDirectoryReader, VectorStoreIndex
from llama_index.core.node_parser import TokenTextSplitter

from aether_engine.config import UserConfig
from aether_engine.knowledge.embeddings import AetherEmbedding
from aether_engine.memory import episodic

logger = logging.getLogger("aether_engine.knowledge.ingest")

#: File types a knowledge collection indexes (P4d pipeline: PDF /
#: MD / TXT / DOCX / code). Anything else in a source folder is not
#: a candidate and is never read.
SUPPORTED_EXTENSIONS = {
    ".pdf", ".md", ".markdown", ".txt", ".text", ".rst", ".docx",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".java", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".rb", ".sh",
}

#: Directories never descended into during a source-folder scan.
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".aether"}

#: Per-file cap: a larger candidate is skipped and recorded, never
#: partially read.
MAX_FILE_BYTES = 5 * 1024 * 1024

CHUNK_SIZE = 512
CHUNK_OVERLAP = 64

# Chunking is token-windowed at word boundaries (TokenTextSplitter),
# NOT sentence-aware: llama-index's SentenceSplitter tokenises
# sentences through NLTK's punkt data, which it downloads at first
# use — a local-first engine that must index offline cannot depend
# on a runtime download (and the download is refused outright in
# restricted-network environments). Word-boundary windows keep
# every chunk self-contained enough for retrieval; citations carry
# exact char spans either way.

MANIFEST_NAME = "manifest.json"
INDEX_SUBDIR = "index"


class EmbedderMismatchError(RuntimeError):
    """The index on disk was built by a different embedder."""


@dataclass
class FileOutcome:
    """What one run did with one source file."""

    path: str  # collection-relative source path
    status: str  # indexed | unchanged | removed | skipped | failed
    chunks: int = 0
    reason: str = ""


@dataclass
class IndexStats:
    """The result of one ingestion run (also the manifest's shape)."""

    collection: str
    source_root: str
    embedder: str
    indexed_at: str
    doc_count: int
    chunk_count: int
    outcomes: list[FileOutcome] = field(default_factory=list)


def manifest_path(collection_dir: Path) -> Path:
    return collection_dir / MANIFEST_NAME


def load_manifest(collection_dir: Path) -> dict[str, Any] | None:
    """The collection's manifest, or None when it was never indexed."""
    path = manifest_path(collection_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def check_embedder(manifest: dict[str, Any], *, config: UserConfig | None = None) -> str:
    """The configured embedder, verified against the manifest.

    Raises :class:`EmbedderMismatchError` when the index on disk
    was built by a different embedder than the one now configured.
    """
    configured = episodic.resolve_embedder(config)
    recorded = str(manifest.get("embedder") or "")
    if recorded and recorded != configured:
        raise EmbedderMismatchError(
            f"this index was built with embedder {recorded!r} but the "
            f"configured embedder is {configured!r}; a different "
            f"embedder is a different vector space, so the collection "
            f"must be fully re-indexed (ingest with force=True) "
            f"before it can be searched or extended.")
    return configured


def _scan_source(source_root: Path) -> list[Path]:
    """Supported files under the source root, in stable order."""
    found: list[Path] = []
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS or part.startswith(".")
               for part in path.relative_to(source_root).parts[:-1]):
            continue
        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
            found.append(path)
    return found


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


async def _nodes_for_file(
    path: Path,
    rel: str,
    splitter: TokenTextSplitter,
    embed_model: AetherEmbedding,
) -> list:
    """Read, chunk and embed one file. Raises on any failure."""
    documents = SimpleDirectoryReader(input_files=[str(path)]).load_data()
    nodes = []
    for document in documents:
        # The relative path IS the document identity: incremental
        # updates delete/insert by it, and citations resolve to it.
        document.doc_id = rel
        document.metadata["source_path"] = rel
        file_nodes = splitter.get_nodes_from_documents([document])
        for position, node in enumerate(file_nodes):
            node.metadata["source_path"] = rel
            node.metadata["chunk_index"] = position
        nodes.extend(file_nodes)
    for node in nodes:
        node.embedding = await embed_model.aget_text_embedding(
            node.get_content(metadata_mode="none"))
    return nodes


async def index_collection(
    source_root: str | Path,
    collection_dir: str | Path,
    *,
    config: UserConfig | None = None,
    force: bool = False,
) -> IndexStats:
    """Index (or incrementally re-index) one collection.

    ``source_root`` is the folder whose supported files form the
    collection; ``collection_dir`` is where the persisted index and
    manifest live. With ``force=True`` any existing index is
    discarded and rebuilt — the required move after an embedder
    change (see :class:`EmbedderMismatchError`).
    """
    source_root = Path(source_root).expanduser()
    collection_dir = Path(collection_dir).expanduser()
    if not source_root.is_dir():
        raise FileNotFoundError(f"source folder does not exist: {source_root}")
    collection_dir.mkdir(parents=True, exist_ok=True)
    persist_dir = collection_dir / INDEX_SUBDIR

    embed_model = AetherEmbedding(config=config)
    manifest = None if force else load_manifest(collection_dir)
    if manifest is not None:
        check_embedder(manifest, config=config)
    old_files: dict[str, dict[str, Any]] = (
        dict(manifest.get("files") or {}) if manifest else {})

    loaded_existing = bool(
        not force and manifest is not None and persist_dir.is_dir())
    if loaded_existing:
        from llama_index.core import StorageContext, load_index_from_storage

        index = load_index_from_storage(
            StorageContext.from_defaults(persist_dir=str(persist_dir)),
            embed_model=embed_model)
    else:
        index = VectorStoreIndex(nodes=[], embed_model=embed_model)

    splitter = TokenTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP, separator=" ")
    outcomes: list[FileOutcome] = []
    new_files: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()

    for path in _scan_source(source_root):
        rel = path.relative_to(source_root).as_posix()
        seen.add(rel)
        try:
            size = path.stat().st_size
        except OSError as exc:
            outcomes.append(FileOutcome(rel, "failed", reason=str(exc)))
            if rel in old_files:
                new_files[rel] = old_files[rel]
            continue
        if size > MAX_FILE_BYTES:
            outcomes.append(FileOutcome(
                rel, "skipped",
                reason=f"{size} bytes exceeds the {MAX_FILE_BYTES}-byte cap"))
            continue
        digest = _sha256(path)
        previous = old_files.get(rel)
        if previous and previous.get("sha256") == digest and not force:
            outcomes.append(FileOutcome(
                rel, "unchanged", chunks=int(previous.get("chunks") or 0)))
            new_files[rel] = previous
            continue
        try:
            nodes = await _nodes_for_file(path, rel, splitter, embed_model)
        except Exception as exc:  # noqa: BLE001 — per-file isolation: one unreadable or unembeddable file is recorded as failed and must not abort the run for the rest
            logger.warning("knowledge ingest failed for %s: %s", rel, exc)
            outcomes.append(FileOutcome(rel, "failed", reason=str(exc)))
            if previous:  # the previous version stays indexed
                new_files[rel] = previous
            continue
        if previous and loaded_existing:
            index.delete_ref_doc(rel, delete_from_docstore=True)
        if nodes:
            index.insert_nodes(nodes)
        outcomes.append(FileOutcome(rel, "indexed", chunks=len(nodes)))
        new_files[rel] = {"sha256": digest, "chunks": len(nodes)}

    for rel in sorted(set(old_files) - seen):
        index.delete_ref_doc(rel, delete_from_docstore=True)
        outcomes.append(FileOutcome(rel, "removed"))

    index.storage_context.persist(persist_dir=str(persist_dir))
    indexed_at = datetime.now(UTC).isoformat()
    stats = IndexStats(
        collection=collection_dir.name,
        source_root=str(source_root),
        embedder=embed_model.model_name,
        indexed_at=indexed_at,
        doc_count=len(new_files),
        chunk_count=sum(int(f.get("chunks") or 0) for f in new_files.values()),
        outcomes=outcomes,
    )
    manifest_path(collection_dir).write_text(json.dumps({
        "collection": stats.collection,
        "source_root": stats.source_root,
        "embedder": stats.embedder,
        "indexed_at": stats.indexed_at,
        "doc_count": stats.doc_count,
        "chunk_count": stats.chunk_count,
        "files": new_files,
    }, indent=2), encoding="utf-8")
    return stats
