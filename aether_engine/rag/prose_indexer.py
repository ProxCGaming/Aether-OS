"""Prose indexer for workspace RAG (Epic 8, E8-3.2; grill Q16/Q17).

The deliberate-retrieval layer of workspace RAG: an embedding index
over the workspace's PROSE ONLY — Markdown, docs, READMEs, notes,
text. Code is NEVER embedded (the repo map from E8-3.1 covers code);
.binaries, lockfiles, build output and dependency folders are never
indexed, ``.gitignore`` is always respected, and a 512 KB per-file
cap applies. Excluded prose-shaped files are recorded with reasons
so the indexing panel (E8-3.3) can show what is out and why.

Embeddings are local-first through the ONE configured embedder
(``episodic.resolve_embedder`` — ``qwen3-embedding:0.6b`` via Ollama
by default), the same embedder episodic memory uses, through the
same circuit breaker (``episodic.embed_text``). When the endpoint
is down, chunks stay ``pending`` in the store — queued, never lost —
and ``backfill_pending`` drains the queue when it answers again.

Indexing is consent-first per workspace: the first index waits for
the user's go (``grant_consent``), with the estimate (file count,
total size, destination) shown before it. Consent persists in the
store. After consent, a polling watcher keeps the index
incrementally fresh (changed → re-embed, deleted → removed).
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from aether_engine.config import UserConfig, load_config
from aether_engine.memory import episodic
from aether_engine.rag.repo_map import _DEFAULT_IGNORE_DIRS, _Gitignore
from aether_engine.rag.store import WorkspaceRagStore
from aether_engine.tools.paths import looks_binary

logger = logging.getLogger("aether_engine.rag.prose_indexer")

# Prose extensions (Q16). Anything else — .py, .ts, .json, … — is not
# a candidate and is never embedded. Extensionless README-style
# files are candidates by name (see _is_prose_candidate).
PROSE_EXTENSIONS = {
    ".md", ".markdown", ".mdown", ".txt", ".text", ".rst", ".adoc", ".org",
}

LOCKFILE_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock",
    "poetry.lock", "cargo.lock", "pipfile.lock", "composer.lock",
    "gemfile.lock", "go.sum",
}

MAX_FILE_BYTES = 512_000  # 512 KB per-file cap (Q16)
CHUNK_SIZE = 1500         # chars per chunk (PRD §E8-3.2)
CHUNK_OVERLAP = 200       # chars of overlap between neighbours
_WATCH_INTERVAL_S = 2.0


class ConsentRequired(Exception):
    """Raised when indexing is attempted for a workspace the user
    has not consented to index yet (Q17: first index waits)."""


def _norm_root(root: str | Path) -> str:
    return str(Path(root).expanduser())


# ---------------------------------------------------------------------------
# Chunking: line-aware windows of <= CHUNK_SIZE chars, neighbours
# overlapping by up to CHUNK_OVERLAP chars of trailing lines. Spans
# are 1-based inclusive line numbers and always satisfy
# content == "\n".join(text.split("\n")[line_start-1:line_end]).
# ---------------------------------------------------------------------------

@dataclass
class ProseChunk:
    chunk_index: int
    line_start: int
    line_end: int
    content: str


def chunk_text(text: str, *, chunk_size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> list[ProseChunk]:
    """Split *text* into line-aligned chunks (see module contract).

    A single line longer than ``chunk_size`` is hard-split into
    overlapping pieces that all carry that line's number. Empty or
    whitespace-only text yields no chunks.
    """
    if not text.strip():
        return []
    lines = text.split("\n")
    n = len(lines)
    chunks: list[ProseChunk] = []
    idx = 0
    start = 0
    while start < n:
        end = start
        length = 0
        while end < n:
            add = len(lines[end]) + (1 if end > start else 0)
            if end > start and length + add > chunk_size:
                break
            length += add
            end += 1
            if length >= chunk_size:
                break
        content = "\n".join(lines[start:end])
        if len(content) > chunk_size and end - start == 1:
            # One over-long line: hard-split with overlap.
            line = lines[start]
            step = max(1, chunk_size - overlap)
            pos = 0
            while pos < len(line):
                piece = line[pos:pos + chunk_size]
                chunks.append(ProseChunk(idx, start + 1, start + 1, piece))
                idx += 1
                if pos + chunk_size >= len(line):
                    break
                pos += step
            start = end
            continue
        chunks.append(ProseChunk(idx, start + 1, end, content))
        idx += 1
        if end >= n:
            break
        # Next window starts as late as possible while keeping up to
        # `overlap` chars of this window's trailing lines.
        next_start = end
        while next_start - 1 > start:
            cand = next_start - 1
            if len("\n".join(lines[cand:end])) <= overlap:
                next_start = cand
            else:
                break
        start = max(next_start, start + 1)
    return chunks


# ---------------------------------------------------------------------------
# Scanning: prose discovery + exclusions with reasons
# ---------------------------------------------------------------------------

@dataclass
class ScanEntry:
    rel_path: str   # posix, relative to the workspace root
    abspath: Path
    size: int


@dataclass
class ExcludedEntry:
    rel_path: str
    reason: str     # gitignore | lockfile | too_large | binary | symlink_escape


@dataclass
class ScanResult:
    indexable: list[ScanEntry] = field(default_factory=list)
    excluded: list[ExcludedEntry] = field(default_factory=list)


def _is_prose_candidate(name: str) -> bool:
    p = Path(name)
    if p.suffix.lower() in PROSE_EXTENSIONS:
        return True
    return p.suffix == "" and p.name.lower().startswith("readme")


def scan_workspace(root: str | Path) -> ScanResult:
    """Walk *root* once, classifying files for the index.

    Directory pruning matches the repo map (E8-3.1): the built-in
    ignore dirs plus root ``.gitignore`` dir rules. Non-prose files
    are silently skipped — they were never candidates; prose-shaped
    files that fail a fence are reported in ``excluded`` with the
    reason, which is what the status contract surfaces.
    """
    root_path = Path(root).expanduser()
    result = ScanResult()
    if not root_path.is_dir():
        return result
    # Containment doctrine (tools/paths.py resolve_contained): judge
    # every candidate by its fully RESOLVED path against the resolved
    # root — a symlink inside the workspace pointing outside it must
    # never be read, chunked or embedded (F-04). A symlink resolving
    # INSIDE the root is legitimate and indexes normally.
    resolved_root = root_path.resolve()
    ignore = _Gitignore(root_path)
    for dirpath, dirnames, filenames in os.walk(root_path):
        rel_dir = Path(dirpath).relative_to(root_path)
        kept = []
        for d in dirnames:
            rel_d = (rel_dir / d).as_posix()
            if d in _DEFAULT_IGNORE_DIRS or ignore.matches(rel_d, is_dir=True):
                continue
            dir_abspath = Path(dirpath) / d
            if dir_abspath.is_symlink():
                # Never descend through a linked directory (os.walk
                # would not follow it by default, but prune it
                # explicitly so the boundary does not depend on that
                # default); an escaping link is reported, an internal
                # one is pruned silently — its target is reached via
                # its real path.
                if not dir_abspath.resolve().is_relative_to(resolved_root):
                    result.excluded.append(
                        ExcludedEntry(rel_d, "symlink_escape"))
                continue
            kept.append(d)
        dirnames[:] = kept
        for name in sorted(filenames):
            rel = (rel_dir / name).as_posix()
            abspath = Path(dirpath) / name
            if ignore.matches(rel, is_dir=False):
                if _is_prose_candidate(name):
                    result.excluded.append(ExcludedEntry(rel, "gitignore"))
                continue
            if name.lower() in LOCKFILE_NAMES:
                result.excluded.append(ExcludedEntry(rel, "lockfile"))
                continue
            if not _is_prose_candidate(name):
                continue
            # Resolve BEFORE stat()/open(): both follow symlinks, so
            # containment must be proven first or the outside file's
            # bytes (and even its size) would already have leaked in.
            if not abspath.resolve().is_relative_to(resolved_root):
                result.excluded.append(ExcludedEntry(rel, "symlink_escape"))
                continue
            try:
                size = abspath.stat().st_size
            except OSError:
                continue
            if size > MAX_FILE_BYTES:
                result.excluded.append(ExcludedEntry(rel, "too_large"))
                continue
            try:
                with abspath.open("rb") as fh:
                    head = fh.read(8192)
            except OSError:
                continue
            if looks_binary(head):
                result.excluded.append(ExcludedEntry(rel, "binary"))
                continue
            result.indexable.append(ScanEntry(rel, abspath, size))
    result.indexable.sort(key=lambda e: e.rel_path)
    result.excluded.sort(key=lambda e: e.rel_path)
    return result


# ---------------------------------------------------------------------------
# The indexer
# ---------------------------------------------------------------------------

class ProseIndexer:
    """Owns indexing for every consented workspace over one store."""

    def __init__(self, store: WorkspaceRagStore | None = None, *,
                 config: UserConfig | None = None,
                 embedder: str | None = None):
        self.store = store if store is not None else WorkspaceRagStore()
        self._config = config
        self._embedder_override = embedder
        self.paused = False
        self._indexing: set[str] = set()
        self._watchers: dict[str, asyncio.Task] = {}
        self._last_excluded: dict[str, list[dict[str, str]]] = {}

    # -- embedder identity -------------------------------------------

    @property
    def embedder(self) -> str:
        """The active embedder's bare name (shared resolution)."""
        return self._embedder_override or episodic.resolve_embedder(self._config)

    def _effective_config(self) -> UserConfig | None:
        if self._embedder_override:
            base = self._config or load_config()
            return replace(base, embedding_model=self._embedder_override)
        return self._config

    def set_embedder(self, embedder: str) -> None:
        """Switch the active embedder. Vectors are incomparable
        across models, so a real switch drops every stored chunk
        (full re-index on the next pass) — PRD §E8-3.2."""
        embedder = str(embedder or "").strip()
        if not embedder:
            return
        if embedder == self.embedder and self.store.embedder() == embedder:
            return
        self._embedder_override = embedder
        if self.store.embedder() != embedder:
            self.store.reset_chunks_for_embedder(embedder)

    def _reconcile_embedder(self) -> None:
        recorded = self.store.embedder()
        if recorded is None:
            self.store.set_embedder(self.embedder)
        elif recorded != self.embedder:
            self.store.reset_chunks_for_embedder(self.embedder)

    # -- consent --------------------------------------------------------

    def has_consent(self, root: str | Path) -> bool:
        return self.store.has_consent(_norm_root(root))

    def grant_consent(self, root: str | Path) -> None:
        self.store.grant_consent(_norm_root(root))

    # -- estimate (the consent preview) ---------------------------------

    def estimate(self, root: str | Path) -> dict[str, int]:
        scan = scan_workspace(root)
        return {
            "estimated_files": len(scan.indexable),
            "estimated_bytes": sum(e.size for e in scan.indexable),
        }

    # -- indexing ---------------------------------------------------------

    async def index_workspace(self, root: str | Path, *,
                              progress_cb=None) -> dict[str, Any]:
        """One incremental index pass over *root*.

        Unchanged files (same mtime/size, already indexed) are
        skipped; changed files are re-chunked and re-embedded;
        deleted files are removed; excluded files are recorded with
        their status. Files whose embeddings fail stay ``pending``
        — queued for backfill, never lost. Raises ConsentRequired
        when the user has not approved indexing this root.
        """
        root_s = _norm_root(root)
        if not self.has_consent(root_s):
            raise ConsentRequired(
                f"Indexing not consented for workspace: {root_s}")
        if root_s in self._indexing:
            counts = self.store.counts(root_s)
            return {"workspace_root": root_s, "skipped": True, **counts}
        self._indexing.add(root_s)
        try:
            return await self._index_pass(root_s, progress_cb=progress_cb)
        finally:
            self._indexing.discard(root_s)

    async def _index_pass(self, root_s: str, *, progress_cb) -> dict[str, Any]:
        self._reconcile_embedder()
        scan = scan_workspace(root_s)
        self._last_excluded[root_s] = [
            {"file_path": e.rel_path, "reason": e.reason} for e in scan.excluded
        ]
        # Excluded files are recorded (status only, never chunks).
        for e in scan.excluded:
            doc_id = self.store.upsert_document(
                root_s, e.rel_path, mtime=0, file_size=0, sha256="",
                status="excluded")
            self.store.replace_chunks(doc_id, [])
        known = {e.rel_path for e in scan.indexable} | {
            e.rel_path for e in scan.excluded}
        for doc in self.store.list_documents(root_s):
            if doc["file_path"] not in known:
                self.store.delete_document(doc["id"])

        total = len(scan.indexable)
        for done, entry in enumerate(scan.indexable, start=1):
            await self._index_file(root_s, entry)
            await self._emit_progress(
                progress_cb, workspace_root=root_s, indexed=done,
                total=total, current_file=entry.rel_path, status="indexing")
        counts = self.store.counts(root_s)
        return {"workspace_root": root_s, **counts}

    async def _index_file(self, root_s: str, entry: ScanEntry) -> None:
        # Defence in depth (F-04): scan_workspace already proved
        # containment, but re-prove it here before any read — an
        # entry can be constructed directly, and a path can be
        # swapped for an escaping symlink between scan and read.
        resolved_root = Path(root_s).expanduser().resolve()
        if not entry.abspath.resolve().is_relative_to(resolved_root):
            logger.warning(
                "Skipping %s: resolved path escapes workspace %s",
                entry.rel_path, root_s)
            return
        try:
            st = entry.abspath.stat()
        except OSError:
            return
        doc = self.store.get_document(root_s, entry.rel_path)
        if (doc is not None and doc["status"] == "indexed"
                and doc["mtime"] == st.st_mtime_ns
                and doc["file_size"] == st.st_size):
            return  # fast path: unchanged and fully embedded
        try:
            data = entry.abspath.read_bytes()
        except OSError:
            return
        sha = hashlib.sha256(data).hexdigest()
        if doc is not None and doc["sha256"] == sha:
            # Content unchanged (touch / mtime-only change): refresh
            # the stat fields, keep the chunks, drain any pending.
            doc_id = self.store.upsert_document(
                root_s, entry.rel_path, mtime=st.st_mtime_ns,
                file_size=st.st_size, sha256=sha, status=doc["status"])
            if not self.store.chunks_for_document(doc_id):
                await self._chunk_and_embed(root_s, doc_id, data)
            else:
                await self._embed_pending(doc_id)
                self._refresh_doc_status(doc_id)
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            doc_id = self.store.upsert_document(
                root_s, entry.rel_path, mtime=st.st_mtime_ns,
                file_size=st.st_size, sha256=sha, status="excluded")
            self.store.replace_chunks(doc_id, [])
            return
        doc_id = self.store.upsert_document(
            root_s, entry.rel_path, mtime=st.st_mtime_ns,
            file_size=st.st_size, sha256=sha, status="pending")
        await self._chunk_and_embed(root_s, doc_id, data, text=text)

    async def _chunk_and_embed(self, root_s: str, doc_id: str, data: bytes,
                               *, text: str | None = None) -> None:
        if text is None:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                self.store.set_document_status(doc_id, "excluded")
                self.store.replace_chunks(doc_id, [])
                return
        chunks = chunk_text(text)
        self.store.replace_chunks(doc_id, [
            {"chunk_index": c.chunk_index, "line_start": c.line_start,
             "line_end": c.line_end, "content": c.content}
            for c in chunks
        ])
        await self._embed_pending(doc_id)
        self._refresh_doc_status(doc_id)

    async def _embed_pending(self, doc_id: str, *,
                             limit: int | None = None) -> int:
        """Embed one document's pending chunks via the shared
        pipeline. Returns how many were embedded this call. A failed
        embed leaves its chunk pending — the queue is the design."""
        done = 0
        for chunk in self.store.pending_chunks(doc_id=doc_id, limit=limit):
            vec = await episodic.embed_text(
                chunk["content"], config=self._effective_config(),
                context="workspace_index")
            if vec is None:
                continue
            self.store.set_chunk_embedding(chunk["id"], vec)
            done += 1
        return done

    def _refresh_doc_status(self, doc_id: str) -> None:
        chunks = self.store.chunks_for_document(doc_id)
        pending = any(c["embedding"] is None for c in chunks)
        self.store.set_document_status(
            doc_id, "pending" if pending else "indexed")

    async def backfill_pending(self, limit: int | None = None) -> dict[str, int]:
        """Drain the pending-embedding queue across all workspaces.

        Called by the watcher each cycle and available on demand:
        when the embedding endpoint comes back, everything queued
        while it was down embeds without any file being re-scanned.
        """
        pending = self.store.pending_chunks(limit=limit)
        embedded = 0
        touched: set[str] = set()
        for chunk in pending:
            vec = await episodic.embed_text(
                chunk["content"], config=self._effective_config(),
                context="workspace_backfill")
            if vec is None:
                continue
            self.store.set_chunk_embedding(chunk["id"], vec)
            embedded += 1
            touched.add(chunk["doc_id"])
        for doc_id in touched:
            self._refresh_doc_status(doc_id)
        return {
            "embedded": embedded,
            "pending": len(self.store.pending_chunks()),
        }

    # -- status (the WS STATUS contract core) ----------------------------

    def status(self, root: str | Path | None = None) -> dict[str, Any]:
        if root is not None:
            root_s = _norm_root(root)
            counts = self.store.counts(root_s)
            excluded = self._excluded_for(root_s)
            indexing = root_s in self._indexing
        else:
            counts = self.store.counts(None)
            excluded = []
            for r in self.store.indexed_roots() + [
                    d["workspace_root"]
                    for d in self.store.list_documents(None)
                    if d["status"] == "excluded"]:
                excluded.extend(self._excluded_for(r))
            indexing = bool(self._indexing)
        if self.paused:
            status = "paused"
        elif indexing:
            status = "indexing"
        else:
            status = "idle"
        indexed_docs = [
            {"file_path": d["file_path"], "file_size": d["file_size"], "status": d["status"]}
            for d in (self.store.list_documents(root_s) if root is not None else self.store.list_documents(None))
            if d["status"] in ("indexed", "pending")
        ]
        return {
            "status": status,
            "total_files": counts["total_files"],
            "indexed_files": counts["indexed_files"],
            "indexed_docs": indexed_docs,
            "excluded_files": excluded,
            "total_chunks": counts["total_chunks"],
            "embedder": self.embedder,
            "paused": self.paused,
        }

    def _excluded_for(self, root_s: str) -> list[dict[str, str]]:
        by_path = {
            d["file_path"]: {"file_path": d["file_path"], "reason": "excluded"}
            for d in self.store.list_documents(root_s)
            if d["status"] == "excluded"
        }
        for entry in self._last_excluded.get(root_s, []):
            by_path[entry["file_path"]] = entry
        if not by_path and Path(root_s).is_dir():
            scan = scan_workspace(root_s)
            self._last_excluded[root_s] = [
                {"file_path": e.rel_path, "reason": e.reason} for e in scan.excluded
            ]
            for entry in self._last_excluded[root_s]:
                by_path[entry["file_path"]] = entry
        return [by_path[k] for k in sorted(by_path)]

    # -- watcher -----------------------------------------------------------

    @property
    def watching_roots(self) -> list[str]:
        return sorted(
            r for r, t in self._watchers.items() if not t.done())

    def start_watching(self, root: str | Path) -> None:
        root_s = _norm_root(root)
        existing = self._watchers.get(root_s)
        if existing is not None and not existing.done():
            return
        self._watchers[root_s] = asyncio.create_task(self._watch_loop(root_s))

    async def stop_watching(self, root: str | Path | None = None) -> None:
        roots = [_norm_root(root)] if root is not None else list(self._watchers)
        for r in roots:
            task = self._watchers.pop(r, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _watch_loop(self, root_s: str) -> None:
        while True:
            try:
                if not self.paused and self.has_consent(root_s):
                    await self.index_workspace(root_s)
                    await self.backfill_pending()
            except asyncio.CancelledError:
                raise
            except Exception:  # a bad pass must not kill the watcher
                logger.exception("Workspace index watch pass failed for %s", root_s)
            await asyncio.sleep(_WATCH_INTERVAL_S)

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    # -- progress ----------------------------------------------------------

    @staticmethod
    async def _emit_progress(cb, **payload) -> None:
        if cb is None:
            return
        result = cb(**payload)
        if inspect.iscoroutine(result):
            await result


# ---------------------------------------------------------------------------
# Process-wide default (the WS handlers and the tool share one
# indexer, hence one store and one watcher set)
# ---------------------------------------------------------------------------

_default_indexer: ProseIndexer | None = None


def get_prose_indexer() -> ProseIndexer:
    global _default_indexer
    if _default_indexer is None:
        _default_indexer = ProseIndexer()
    return _default_indexer
