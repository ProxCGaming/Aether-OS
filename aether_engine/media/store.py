"""MediaStore managing ~/.aether/media.db (Epic 8, E8-4.2).

In-place reference index for media items (snips, user-uploaded media, and agent-generated media).
Media files are never moved; the index points at their in-place filesystem paths.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("aether_engine.media.store")

_DEFAULT_DB_PATH = Path.home() / ".aether" / "media.db"

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tiff", ".ico"}
_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".m4v"}


class MediaStore:
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
                "created_at", "analyzed_at"
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
                "(file_path LIKE ? OR summary_text LIKE ? OR user_notes LIKE ? OR tags_json LIKE ?)"
            )
            params.extend([q_like, q_like, q_like, q_like])

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
            p = Path(file_path)
            try:
                if p.exists() and p.is_file():
                    os.unlink(p)
                    file_deleted = True
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Failed to delete underlying file {file_path}: {e}")

        return True, file_deleted

    def delete_by_session(self, session_id: str) -> int:
        """Delete all media entries belonging to a session."""
        self.init_db()
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM media_items WHERE session_id = ?", (session_id,))
            ids = [r[0] for r in cur.fetchall()]
            for mid in ids:
                conn.execute("DELETE FROM media_embeddings WHERE media_id = ?", (mid,))
                conn.execute("DELETE FROM media_items WHERE id = ?", (mid,))
            conn.commit()
            return len(ids)


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
