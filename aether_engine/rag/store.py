"""SQLite store for the workspace prose index (Epic 8, E8-3.2).

The prose index is its OWN store (``~/.aether/workspace_rag.db``):
it does NOT switch when the Epic 4 memory backend switches — no
memory backend offers file ingestion (verified at grill time), so
workspace RAG reuses only the native embedding pipeline, never the
memory store. Documents are references to live files (path + mtime
+ sha256); chunks carry the embedded text spans with 1-based line
numbers that ``workspace_search`` citations resolve back to disk.

Schema per PRD §E8-3.2, migrated via ``PRAGMA user_version = 1``.
One deliberate addition: a tiny ``rag_meta`` table for index-level
bookkeeping (which embedder produced the current vectors, which
roots the user consented to index). Switching embedders drops and
recreates ``prose_chunks`` — vectors are incomparable across
models, so a switch always means a full re-index.

``mtime`` stores ``st_mtime_ns``: still an INTEGER per the schema,
but nanosecond resolution keeps the incremental fast path honest
for same-second, same-size edits.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from aether_engine.embedding_codec import decode_embedding, encode_embedding

logger = logging.getLogger("aether_engine.rag.store")

_DEFAULT_DB_PATH = Path.home() / ".aether" / "workspace_rag.db"

_META_EMBEDDER = "embedder"
_META_CONSENT_PREFIX = "consent:"


def document_id(workspace_root: str, file_path: str) -> str:
    """Stable document id for a (root, relative path) pair, so a
    rescan upserts the same row instead of duplicating it."""
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"{workspace_root}\n{file_path}").hex


class WorkspaceRagStore:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._initialized = False

    # -- plumbing ----------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def init_db(self) -> None:
        """Create the schema (PRAGMA user_version = 1). Idempotent."""
        if self._initialized:
            return
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA user_version")
            row = cur.fetchone()
            current_version = row[0] if row else 0
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prose_documents (
                    id TEXT PRIMARY KEY,
                    workspace_root TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    mtime INTEGER NOT NULL,
                    file_size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    status TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prose_chunks (
                    id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    line_start INTEGER NOT NULL,
                    line_end INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    embedding BLOB,
                    FOREIGN KEY(doc_id) REFERENCES prose_documents(id)
                        ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rag_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_prose_docs_path"
                " ON prose_documents(workspace_root, file_path)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_prose_chunks_doc"
                " ON prose_chunks(doc_id)")
            if current_version < 1:
                conn.execute("PRAGMA user_version = 1")
            conn.commit()
        self._initialized = True

    # -- meta (embedder identity + indexing consent) -----------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        self.init_db()
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM rag_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.init_db()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO rag_meta (key, value) VALUES (?, ?)",
                (key, value))
            conn.commit()

    def embedder(self) -> str | None:
        """The embedder that produced the stored vectors, if any."""
        return self.get_meta(_META_EMBEDDER)

    def set_embedder(self, embedder: str) -> None:
        self.set_meta(_META_EMBEDDER, embedder)

    def has_consent(self, workspace_root: str) -> bool:
        return self.get_meta(_META_CONSENT_PREFIX + workspace_root) == "1"

    def grant_consent(self, workspace_root: str) -> None:
        self.set_meta(_META_CONSENT_PREFIX + workspace_root, "1")

    # -- documents -----------------------------------------------------

    def upsert_document(self, workspace_root: str, file_path: str, *,
                        mtime: int, file_size: int, sha256: str,
                        status: str) -> str:
        self.init_db()
        doc_id = document_id(workspace_root, file_path)
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO prose_documents
                       (id, workspace_root, file_path, mtime, file_size,
                        sha256, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       mtime = excluded.mtime,
                       file_size = excluded.file_size,
                       sha256 = excluded.sha256,
                       status = excluded.status""",
                (doc_id, workspace_root, file_path, mtime, file_size,
                 sha256, status))
            conn.commit()
        return doc_id

    def get_document(self, workspace_root: str,
                     file_path: str) -> dict[str, Any] | None:
        self.init_db()
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM prose_documents"
                " WHERE workspace_root = ? AND file_path = ?",
                (workspace_root, file_path)).fetchone()
        return dict(row) if row else None

    def list_documents(self, workspace_root: str | None = None,
                       ) -> list[dict[str, Any]]:
        self.init_db()
        with self._get_conn() as conn:
            if workspace_root is None:
                rows = conn.execute(
                    "SELECT * FROM prose_documents"
                    " ORDER BY workspace_root, file_path").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM prose_documents"
                    " WHERE workspace_root = ? ORDER BY file_path",
                    (workspace_root,)).fetchall()
        return [dict(r) for r in rows]

    def set_document_status(self, doc_id: str, status: str) -> None:
        self.init_db()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE prose_documents SET status = ? WHERE id = ?",
                (status, doc_id))
            conn.commit()

    def delete_document(self, doc_id: str) -> bool:
        self.init_db()
        with self._get_conn() as conn:
            cur = conn.execute(
                "DELETE FROM prose_documents WHERE id = ?", (doc_id,))
            conn.commit()
        return cur.rowcount > 0

    # -- chunks --------------------------------------------------------

    def replace_chunks(self, doc_id: str, chunks: list[dict[str, Any]]) -> None:
        """Replace a document's chunks wholesale (re-chunk on change).

        Chunk dicts: ``chunk_index``, ``line_start``, ``line_end``,
        ``content``, and optional ``embedding`` (list[float] | None).
        Chunk ids are deterministic per (doc, index, content hash),
        so identical content keeps identical ids across rescans.
        """
        self.init_db()
        with self._get_conn() as conn:
            conn.execute("DELETE FROM prose_chunks WHERE doc_id = ?", (doc_id,))
            for c in chunks:
                digest = hashlib.sha256(c["content"].encode("utf-8")).hexdigest()
                chunk_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{doc_id}\n{c['chunk_index']}\n{digest}").hex
                embedding = c.get("embedding")
                conn.execute(
                    """INSERT INTO prose_chunks
                           (id, doc_id, chunk_index, line_start, line_end,
                            content, embedding)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (chunk_id, doc_id, c["chunk_index"], c["line_start"],
                     c["line_end"], c["content"],
                     encode_embedding(embedding) if embedding else None))
            conn.commit()

    def chunks_for_document(self, doc_id: str) -> list[dict[str, Any]]:
        self.init_db()
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM prose_chunks WHERE doc_id = ?"
                " ORDER BY chunk_index", (doc_id,)).fetchall()
        return [self._chunk_dict(r) for r in rows]

    def pending_chunks(self, doc_id: str | None = None,
                       limit: int | None = None) -> list[dict[str, Any]]:
        """Chunks still waiting for an embedding (the backfill queue).

        These are the queue the circuit-breaker design protects:
        endpoint down → chunks sit here, never lost, and drain on the
        next backfill once the endpoint answers again.
        """
        self.init_db()
        sql = ("SELECT c.*, d.workspace_root, d.file_path"
               " FROM prose_chunks c"
               " JOIN prose_documents d ON d.id = c.doc_id"
               " WHERE c.embedding IS NULL")
        params: list[Any] = []
        if doc_id is not None:
            sql += " AND c.doc_id = ?"
            params.append(doc_id)
        sql += " ORDER BY d.file_path, c.chunk_index"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._chunk_dict(r) for r in rows]

    def set_chunk_embedding(self, chunk_id: str, embedding: list[float]) -> None:
        self.init_db()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE prose_chunks SET embedding = ? WHERE id = ?",
                (encode_embedding(embedding), chunk_id))
            conn.commit()

    def search_candidates(self, workspace_roots: list[str] | None,
                          ) -> list[dict[str, Any]]:
        """Searchable chunks (non-excluded docs) for the given roots.

        Both search legs read this set: chunks with an embedding feed
        the semantic leg; every chunk feeds the lexical leg, so a
        partially-indexed workspace still answers.

        ``workspace_roots`` is required and explicit (S2-M2): there
        is deliberately NO unscoped mode. ``None`` or an empty list
        means "no workspace granted" and returns no candidates —
        never every indexed workspace in the store. A caller that
        legitimately searches several roots passes them all here.
        """
        self.init_db()
        if not workspace_roots:
            return []
        marks = ", ".join("?" for _ in workspace_roots)
        sql = ("SELECT c.*, d.workspace_root, d.file_path, d.status AS doc_status"
               " FROM prose_chunks c"
               " JOIN prose_documents d ON d.id = c.doc_id"
               " WHERE d.status != 'excluded'"
               f" AND d.workspace_root IN ({marks})")
        params: list[Any] = list(workspace_roots)
        sql += " ORDER BY d.file_path, c.chunk_index"
        with self._get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._chunk_dict(r) for r in rows]

    @staticmethod
    def _chunk_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        # S4-M13: only the tagged safe format decodes; a legacy
        # pickle blob reads as None ("no embedding" — the chunk
        # rejoins the backfill queue) and is never unpickled.
        d["embedding"] = decode_embedding(d.get("embedding"))
        return d

    # -- aggregates ----------------------------------------------------

    def counts(self, workspace_root: str | None = None) -> dict[str, int]:
        """File/chunk tallies for the status contract.

        ``total_files`` counts documents that belong to the index
        (status != 'excluded') — the denominator ``indexed_files``
        progresses against; excluded files are reported separately.
        """
        self.init_db()
        where = " WHERE workspace_root = ?" if workspace_root else ""
        params = [workspace_root] if workspace_root else []
        with self._get_conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM prose_documents"
                f"{where} {'AND' if where else 'WHERE'} status != 'excluded'",
                params).fetchone()[0]
            indexed = conn.execute(
                "SELECT COUNT(*) FROM prose_documents"
                f"{where} {'AND' if where else 'WHERE'} status = 'indexed'",
                params).fetchone()[0]
            if workspace_root:
                chunks = conn.execute(
                    "SELECT COUNT(*) FROM prose_chunks c"
                    " JOIN prose_documents d ON d.id = c.doc_id"
                    " WHERE d.workspace_root = ?",
                    params).fetchone()[0]
            else:
                chunks = conn.execute(
                    "SELECT COUNT(*) FROM prose_chunks").fetchone()[0]
        return {
            "total_files": int(total),
            "indexed_files": int(indexed),
            "total_chunks": int(chunks),
        }

    def indexed_roots(self) -> list[str]:
        """Roots holding at least one non-excluded document."""
        self.init_db()
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT workspace_root FROM prose_documents"
                " WHERE status != 'excluded' ORDER BY workspace_root").fetchall()
        return [r["workspace_root"] for r in rows]

    # -- embedder switch -------------------------------------------------

    def reset_chunks_for_embedder(self, embedder: str) -> None:
        """Drop every chunk and mark indexed docs pending (PRD: a
        switch drops and recreates ``prose_chunks`` — the chunks are
        re-derived from the live files by the next index pass, and
        documents keep their file metadata so the re-index is a
        re-chunk + re-embed, not a rescan of what's excluded)."""
        self.init_db()
        with self._get_conn() as conn:
            conn.execute("DELETE FROM prose_chunks")
            conn.execute(
                "UPDATE prose_documents SET status = 'pending'"
                " WHERE status = 'indexed'")
            conn.execute(
                "INSERT OR REPLACE INTO rag_meta (key, value) VALUES (?, ?)",
                (_META_EMBEDDER, embedder))
            conn.commit()
        logger.info("Prose index reset for embedder switch to %s", embedder)
