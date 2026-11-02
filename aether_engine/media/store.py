"""MediaStore managing ~/.aether/media.db (Epic 8, E8-4.2).

In-place reference index for media items (snips, user-uploaded media, and agent-generated media).
Media files are never moved; the index points at their in-place filesystem paths.
"""
from __future__ import annotations

import json
import logging
import math
import mimetypes
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from aether_engine.embedding_codec import decode_embedding, encode_embedding

logger = logging.getLogger("aether_engine.media.store")

_DEFAULT_DB_PATH = Path.home() / ".aether" / "media.db"

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tiff", ".ico"}
_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".m4v"}

# Epic 8, E8-4.3: hybrid recall ranking mirrors episodic memory /
# workspace search — a lexical leg and a semantic leg merged with
# RRF. The semantic leg only admits cosine >= 0.3, the same floor
# episodic recall uses (EPISODIC_SIMILARITY_THRESHOLD): with the
# unified embedder, genuinely related media scores well above it
# while unrelated items stay below.
MEDIA_SIMILARITY_THRESHOLD = 0.3
_RRF_K = 60
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _unlink_media_file(file_path: str) -> bool:
    """Best-effort unlink of a media item's file (S4-M3).

    Returns True only when a regular file existed and was removed;
    a missing file or any failure is tolerated (warning-logged).
    """
    p = Path(file_path)
    try:
        if p.exists() and p.is_file():
            os.unlink(p)
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to delete underlying file {file_path}: {e}")
    return False


def _cosine(vec1: list[float], vec2: list[float]) -> float:
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    dot = sum(a * b for a, b in zip(vec1, vec2))
    n1 = math.sqrt(sum(a * a for a in vec1))
    n2 = math.sqrt(sum(b * b for b in vec2))
    if n1 == 0.0 or n2 == 0.0:
        return 0.0
    return dot / (n1 * n2)


class MediaStore:
    # Also reachable as MediaStore._unlink_media_file for callers/tests.
    _unlink_media_file = staticmethod(_unlink_media_file)

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._initialized = False

    def _get_conn(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        """Initialise database schema and run migrations (PRAGMA user_version = 1)."""
        if self._initialized:
            return

        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA user_version")
            row = cur.fetchone()
            current_version = row[0] if row else 0

            # Create media_items table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS media_items (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    message_id TEXT,
                    collection TEXT NOT NULL, -- 'user' or 'generated'
                    media_type TEXT NOT NULL, -- 'image' or 'video'
                    file_path TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    mime_type TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    probe_metadata_json TEXT,
                    summary_text TEXT,
                    transcript_text TEXT,
                    transcript_status TEXT,
                    tags_json TEXT DEFAULT '[]',
                    user_notes TEXT DEFAULT '',
                    created_at INTEGER NOT NULL,
                    analyzed_at INTEGER
                )
            """)

            # Create media_embeddings table (clean cascade seam for E8-4.3)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS media_embeddings (
                    media_id TEXT PRIMARY KEY,
                    embedding BLOB NOT NULL,
                    FOREIGN KEY(media_id) REFERENCES media_items(id) ON DELETE CASCADE
                )
            """)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_media_collection ON media_items(collection)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_media_session ON media_items(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_media_created ON media_items(created_at DESC)")

            # Table migrations: inspect columns via PRAGMA table_info(media_items)
            cur.execute("PRAGMA table_info(media_items)")
            existing_cols = {col_info[1] for col_info in cur.fetchall()}
            expected_cols = {
                "id", "session_id", "message_id", "collection", "media_type",
                "file_path", "file_size", "mime_type", "status",
                "probe_metadata_json", "summary_text", "tags_json", "user_notes",
                "created_at", "analyzed_at",
                # Epic 8, E8-4.4: transcript stage columns (added to
                # pre-4.4 databases by this same migration loop).
                "transcript_text", "transcript_status",
            }
            for col in expected_cols - existing_cols:
                conn.execute(f"ALTER TABLE media_items ADD COLUMN {col} TEXT")

            if current_version < 1:
                conn.execute("PRAGMA user_version = 1")
            conn.commit()

        self._initialized = True

    def register_media(
        self,
        file_path: str,
        collection: str = "user",
        media_type: str | None = None,
        session_id: str | None = None,
        message_id: str | None = None,
        mime_type: str | None = None,
        status: str = "pending",
        probe_metadata: dict[str, Any] | None = None,
        summary_text: str | None = None,
        tags: list[str] | None = None,
        user_notes: str = "",
        media_id: str | None = None,
    ) -> dict[str, Any]:
        """Register an in-place media reference in the media store."""
        self.init_db()
        mid = media_id or str(uuid.uuid4())
        now = int(time.time() * 1000)

        path_obj = Path(file_path)
        file_exists = path_obj.exists() and path_obj.is_file()
        file_size = path_obj.stat().st_size if file_exists else 0

        # Infer mime_type and media_type if not provided
        if not mime_type:
            mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            mime_type = "image/png" if path_obj.suffix.lower() in _IMAGE_EXTENSIONS else "application/octet-stream"

        if not media_type:
            ext = path_obj.suffix.lower()
            if ext in _VIDEO_EXTENSIONS or (mime_type and mime_type.startswith("video/")):
                media_type = "video"
            else:
                media_type = "image"

        probe_meta = dict(probe_metadata or {})
        # Ensure origin is tracked in probe_metadata
        if "origin" not in probe_meta:
            if collection == "generated":
                probe_meta["origin"] = "agent_generated"
            elif "snip" in path_obj.name.lower():
                probe_meta["origin"] = "snip"
            else:
                probe_meta["origin"] = "user_upload"

        tags_list = list(tags or [])
        meta_json = json.dumps(probe_meta)
        tags_json = json.dumps(tags_list)

        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO media_items (
                    id, session_id, message_id, collection, media_type,
                    file_path, file_size, mime_type, status,
                    probe_metadata_json, summary_text, tags_json, user_notes,
                    created_at, analyzed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mid, session_id, message_id, collection, media_type,
                    str(file_path), file_size, mime_type, status,
                    meta_json, summary_text, tags_json, user_notes,
                    now, None
                )
            )
            conn.commit()

        return {
            "id": mid,
            "session_id": session_id,
            "message_id": message_id,
            "collection": collection,
            "media_type": media_type,
            "file_path": str(file_path),
            "file_size": file_size,
            "mime_type": mime_type,
            "status": status,
            "probe_metadata": probe_meta,
            "origin": probe_meta.get("origin", collection),
            "summary_text": summary_text,
            "transcript_text": None,
            "transcript_status": None,
            "tags": tags_list,
            "user_notes": user_notes,
            "created_at": now,
            "analyzed_at": None,
            "file_exists": file_exists,
        }

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        file_p = Path(d["file_path"])
        d["file_exists"] = file_p.exists() and file_p.is_file()

        try:
            d["probe_metadata"] = json.loads(d.get("probe_metadata_json") or "{}")
        except Exception:  # noqa: BLE001
            d["probe_metadata"] = {}

        try:
            d["tags"] = json.loads(d.get("tags_json") or "[]")
        except Exception:  # noqa: BLE001
            d["tags"] = []

        d["origin"] = d["probe_metadata"].get("origin")
        if not d["origin"]:
            d["origin"] = "agent_generated" if d["collection"] == "generated" else "user_upload"

        return d

    def get_media(self, media_id: str) -> dict[str, Any] | None:
        """Fetch a single media item by id."""
        self.init_db()
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM media_items WHERE id = ?", (media_id,))
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_dict(row)

    def list_media(
        self,
        collection: str = "all",
        session_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        query: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """List media items matching criteria."""
        self.init_db()
        where_clauses = []
        params: list[Any] = []

        if collection and collection != "all":
            if collection in ("user", "generated"):
                where_clauses.append("collection = ?")
                params.append(collection)
            elif collection in ("snip", "snips"):
                where_clauses.append("probe_metadata_json LIKE ?")
                params.append('%"origin": "snip"%')

        if session_id:
            where_clauses.append("session_id = ?")
            params.append(session_id)

        if query and query.strip():
            q_like = f"%{query.strip()}%"
            where_clauses.append(
                "(file_path LIKE ? OR summary_text LIKE ? OR user_notes LIKE ? "
                "OR tags_json LIKE ? OR transcript_text LIKE ?)"
            )
            params.extend([q_like, q_like, q_like, q_like, q_like])

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM media_items {where_sql}", params)
            total = cur.fetchone()[0]

            sql = f"SELECT * FROM media_items {where_sql} ORDER BY created_at DESC"
            if limit is not None:
                sql += f" LIMIT {int(limit)}"
                if offset is not None:
                    sql += f" OFFSET {int(offset)}"

            cur.execute(sql, params)
            rows = cur.fetchall()
            items = [self._row_to_dict(r) for r in rows]

        return items, total

    def update_metadata(
        self,
        media_id: str,
        tags: list[str] | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Update tags and/or user notes for a media item."""
        self.init_db()
        updates = []
        params: list[Any] = []

        if tags is not None:
            updates.append("tags_json = ?")
            params.append(json.dumps(tags))

        if notes is not None:
            updates.append("user_notes = ?")
            params.append(notes)

        if updates:
            params.append(media_id)
            with self._get_conn() as conn:
                conn.execute(
                    f"UPDATE media_items SET {', '.join(updates)} WHERE id = ?",
                    params,
                )
                conn.commit()

        item = self.get_media(media_id)
        if not item:
            return {"media_id": media_id, "tags": tags or [], "notes": notes or ""}

        return {
            "media_id": media_id,
            "tags": item.get("tags", []),
            "notes": item.get("user_notes", ""),
        }

    # ------------------------------------------------------------------
    # Epic 8, E8-4.3: analysis results, embeddings, and hybrid recall.
    # The media_embeddings table itself was created by E8-4.2 as the
    # seam for this slice.
    # ------------------------------------------------------------------

    def store_embedding(self, media_id: str, embedding: list[float]) -> None:
        """Persist (or replace) the metadata embedding for a media item."""
        self.init_db()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO media_embeddings (media_id, embedding) VALUES (?, ?)",
                (media_id, encode_embedding(embedding)),
            )
            conn.commit()

    def get_embedding(self, media_id: str) -> list[float] | None:
        """Fetch the stored embedding for a media item, or None."""
        self.init_db()
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT embedding FROM media_embeddings WHERE media_id = ?", (media_id,))
            row = cur.fetchone()
            if not row:
                return None
            # S4-M13: only the tagged safe format decodes; a corrupt
            # or legacy pickle blob reads as "no embedding".
            return decode_embedding(row[0])

    def update_analysis(
        self,
        media_id: str,
        status: str | None = None,
        summary_text: str | None = None,
        tags: list[str] | None = None,
        probe_metadata: dict[str, Any] | None = None,
        analyzed_at: int | None = None,
        transcript_text: str | None = None,
        transcript_status: str | None = None,
    ) -> dict[str, Any] | None:
        """Apply analysis outcome fields to a media item.

        Only the fields passed are written; returns the refreshed
        item dict (or None when the id is unknown).
        """
        self.init_db()
        updates: list[str] = []
        params: list[Any] = []
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if summary_text is not None:
            updates.append("summary_text = ?")
            params.append(summary_text)
        if transcript_text is not None:
            updates.append("transcript_text = ?")
            params.append(transcript_text)
        if transcript_status is not None:
            updates.append("transcript_status = ?")
            params.append(transcript_status)
        if tags is not None:
            updates.append("tags_json = ?")
            params.append(json.dumps(list(tags)))
        if probe_metadata is not None:
            updates.append("probe_metadata_json = ?")
            params.append(json.dumps(probe_metadata))
        if analyzed_at is not None:
            updates.append("analyzed_at = ?")
            params.append(analyzed_at)
        if updates:
            params.append(media_id)
            with self._get_conn() as conn:
                conn.execute(
                    f"UPDATE media_items SET {', '.join(updates)} WHERE id = ?",
                    params,
                )
                conn.commit()
        return self.get_media(media_id)

    def list_by_status(
        self,
        statuses: list[str] | tuple[str, ...],
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """List media items whose status is in ``statuses``, oldest first."""
        self.init_db()
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        sql = (
            f"SELECT * FROM media_items WHERE status IN ({placeholders}) "
            "ORDER BY created_at ASC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, list(statuses))
            return [self._row_to_dict(r) for r in cur.fetchall()]

    def list_videos_missing_transcripts(
        self,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Analyzed videos the transcript stage still owes (E8-4.4).

        Eligible: no attempt yet (status NULL — every video analysed
        before E8-4.4), an interrupted attempt (``pending``), or a
        retryable outcome (``failed`` / ``unavailable`` — the deps
        may have been installed since). Terminal states are
        excluded: ``done`` is never re-transcribed (idempotency)
        and ``no_audio`` cannot change for the same file.
        Oldest first, like ``list_by_status``.
        """
        self.init_db()
        sql = (
            "SELECT * FROM media_items WHERE media_type = 'video' "
            "AND status = 'analyzed' AND (transcript_status IS NULL "
            "OR transcript_status IN ('pending', 'failed', 'unavailable')) "
            "ORDER BY created_at ASC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute(sql)
            return [self._row_to_dict(r) for r in cur.fetchall()]

    def _embeddings_for(self, media_ids: list[str]) -> dict[str, list[float]]:
        if not media_ids:
            return {}
        placeholders = ", ".join("?" for _ in media_ids)
        out: dict[str, list[float]] = {}
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT media_id, embedding FROM media_embeddings WHERE media_id IN ({placeholders})",
                media_ids,
            )
            for media_id, blob in cur.fetchall():
                vec = decode_embedding(blob)
                if vec is None:  # corrupt or legacy pickle blob — skip it
                    logger.debug(f"Skipping corrupt embedding blob for {media_id}")
                    continue
                out[media_id] = vec
        return out

    def search_media(
        self,
        query: str = "",
        query_vec: list[float] | None = None,
        limit: int = 10,
        collection: str = "all",
    ) -> list[dict[str, Any]]:
        """Hybrid recall over the media index (E8-4.3).

        A lexical leg (distinct query tokens found in the summary,
        tags, notes, or file name) and — when ``query_vec`` is given —
        a semantic leg (cosine over stored metadata embeddings,
        floored at MEDIA_SIMILARITY_THRESHOLD) are merged with RRF,
        the same ranking shape as episodic recall and workspace
        search. Returned dicts are full media items plus ``score``.
        With no query vector the lexical leg alone answers, which is
        how recall degrades while the embedding endpoint is down.
        """
        self.init_db()
        limit = max(1, int(limit or 10))
        candidates, _total = self.list_media(collection=collection, limit=None)
        if not candidates:
            return []

        query_tokens = set(_tokens(query))

        def _lexical_text(item: dict[str, Any]) -> str:
            return " ".join([
                item.get("summary_text") or "",
                " ".join(item.get("tags") or []),
                item.get("user_notes") or "",
                Path(item.get("file_path") or "").name,
                # Epic 8, E8-4.4: spoken words are searchable text —
                # a phrase only ever said in a video is findable.
                item.get("transcript_text") or "",
            ])

        lexical_ranked = sorted(
            (it for it in candidates
             if query_tokens and len(query_tokens & set(_tokens(_lexical_text(it)))) > 0),
            key=lambda it: (
                -len(query_tokens & set(_tokens(_lexical_text(it)))),
                it.get("created_at") or 0,
            ),
        )

        semantic_ranked: list[dict[str, Any]] = []
        if query_vec is not None:
            embeddings = self._embeddings_for([it["id"] for it in candidates])
            scored = []
            for it in candidates:
                emb = embeddings.get(it["id"])
                if emb is None:
                    continue
                sim = _cosine(query_vec, emb)
                if sim >= MEDIA_SIMILARITY_THRESHOLD:
                    scored.append((sim, it))
            semantic_ranked = [
                it for _sim, it in sorted(
                    scored, key=lambda p: (-p[0], p[1].get("created_at") or 0))
            ]

        merged: dict[str, dict[str, Any]] = {}
        for leg in (lexical_ranked, semantic_ranked):
            for rank, it in enumerate(leg):
                entry = merged.setdefault(it["id"], {"item": it, "score": 0.0})
                entry["score"] += 1.0 / (_RRF_K + rank + 1)
        ordered = sorted(
            merged.values(),
            key=lambda e: (-e["score"], e["item"].get("created_at") or 0),
        )
        results: list[dict[str, Any]] = []
        for entry in ordered[:limit]:
            item = dict(entry["item"])
            item["score"] = entry["score"]
            results.append(item)
        return results

    def delete_media(self, media_id: str, delete_underlying_file: bool = False) -> tuple[bool, bool]:
        """Delete media entry and embedding from database.

        If delete_underlying_file is True, also unlinks the file from disk if it exists.
        Returns:
            (deleted_from_db, file_deleted_from_disk)
        """
        self.init_db()
        file_path: str | None = None
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT file_path FROM media_items WHERE id = ?", (media_id,))
            row = cur.fetchone()
            if not row:
                return False, False
            file_path = row[0]

            # Delete embeddings first (in case SQLite foreign key cascade is disabled)
            conn.execute("DELETE FROM media_embeddings WHERE media_id = ?", (media_id,))
            # Delete media_items entry
            conn.execute("DELETE FROM media_items WHERE id = ?", (media_id,))
            conn.commit()

        file_deleted = False
        if delete_underlying_file and file_path:
            file_deleted = _unlink_media_file(file_path)

        return True, file_deleted

    def delete_by_session(self, session_id: str) -> int:
        """Delete all media entries belonging to a session.

        S4-M3: also unlinks each item's file from disk (best-effort,
        via ``_unlink_media_file``) — previously only the DB rows were
        removed and the session's files were orphaned on disk.
        """
        self.init_db()
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, file_path FROM media_items WHERE session_id = ?", (session_id,))
            pairs = [(r[0], r[1]) for r in cur.fetchall()]
            for mid, _file_path in pairs:
                conn.execute("DELETE FROM media_embeddings WHERE media_id = ?", (mid,))
                conn.execute("DELETE FROM media_items WHERE id = ?", (mid,))
            conn.commit()
        for _mid, file_path in pairs:
            if file_path:
                _unlink_media_file(file_path)
        return len(pairs)


GLOBAL_MEDIA_STORE: MediaStore | None = None


def get_media_store() -> MediaStore:
    """Return the global default MediaStore singleton."""
    global GLOBAL_MEDIA_STORE
    if GLOBAL_MEDIA_STORE is None:
        GLOBAL_MEDIA_STORE = MediaStore()
    return GLOBAL_MEDIA_STORE


def set_global_media_store(store: MediaStore) -> None:
    """Set the global MediaStore singleton (useful in test harnesses)."""
    global GLOBAL_MEDIA_STORE
    GLOBAL_MEDIA_STORE = store
