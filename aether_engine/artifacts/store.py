"""Artifact store managing ~/.aether/artifacts.db (Epic 8, E8-2.1).

Stores artifact metadata and reference paths for deliverables created or edited
by agents during tasks.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("aether_engine.artifacts.store")

_DEFAULT_DB_PATH = Path.home() / ".aether" / "artifacts.db"


class ArtifactStore:
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

            # Create table if not exists
            conn.execute("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    file_path TEXT,
                    content TEXT,
                    metadata_json TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_created ON artifacts(created_at DESC)")

            # Table migrations: inspect columns via PRAGMA table_info(artifacts)
            cur.execute("PRAGMA table_info(artifacts)")
            existing_cols = {col_info[1] for col_info in cur.fetchall()}
            expected_cols = {
                "id", "session_id", "task_id", "kind", "title",
                "file_path", "content", "metadata_json", "created_at", "updated_at"
            }
            for col in expected_cols - existing_cols:
                conn.execute(f"ALTER TABLE artifacts ADD COLUMN {col} TEXT")

            if current_version < 1:
                conn.execute("PRAGMA user_version = 1")
            conn.commit()

        self._initialized = True

    def record_artifact(
        self,
        session_id: str,
        task_id: str,
        kind: str,
        title: str,
        file_path: str | None = None,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
        artifact_id: str | None = None,
    ) -> dict[str, Any]:
        """Record an artifact in the store. Returns the artifact dict."""
        self.init_db()
        now = int(time.time() * 1000)
        meta_json = json.dumps(metadata or {})

        # S4-M9 residual: when no explicit id is given and this is a
        # file-backed artifact, reuse the earliest existing row for the
        # same (session_id, task_id, file_path) so repeated writes/edits
        # of the same file in one task refresh a single artifact instead
        # of minting a new row per edit. created_at is preserved by the
        # ON CONFLICT(id) update below (it is not in the SET list).
        existing_created_at: int | None = None
        if artifact_id is None and file_path:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT id, created_at FROM artifacts "
                    "WHERE session_id = ? AND task_id = ? AND file_path = ? "
                    "ORDER BY created_at ASC LIMIT 1",
                    (session_id, task_id, file_path),
                )
                row = cur.fetchone()
                if row:
                    artifact_id = row[0]
                    try:
                        existing_created_at = row[1]
                    except Exception:  # noqa: BLE001 - defensive: row shape
                        existing_created_at = None
        elif artifact_id is not None:
            # Preserve created_at in the returned dict for explicit upserts.
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT created_at FROM artifacts WHERE id = ?",
                    (artifact_id,),
                )
                row = cur.fetchone()
                if row:
                    existing_created_at = row[0]

        art_id = artifact_id or str(uuid.uuid4())

        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO artifacts (
                    id, session_id, task_id, kind, title, file_path, content, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind,
                    title=excluded.title,
                    file_path=excluded.file_path,
                    content=excluded.content,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (art_id, session_id, task_id, kind, title, file_path, content, meta_json, now, now),
            )
            conn.commit()

        return {
            "id": art_id,
            "session_id": session_id,
            "task_id": task_id,
            "kind": kind,
            "title": title,
            "file_path": file_path,
            "content": content,
            "metadata": metadata or {},
            "created_at": existing_created_at if existing_created_at is not None else now,
            "updated_at": now,
        }

    def record_summary_artifact(
        self,
        session_id: str,
        task_id: str,
        summary: str,
        title: str = "Task Summary",
        metadata: dict[str, Any] | None = None,
        artifact_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a task's final summary as an artifact document (kind = 'report') with direct text storage (Epic 8, E8-2.4)."""
        self.init_db()
        art_id = artifact_id
        if not art_id:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT id FROM artifacts WHERE session_id = ? AND task_id = ? AND kind = 'report'",
                    (session_id, task_id),
                )
                row = cur.fetchone()
                if row:
                    art_id = row[0]

        meta = {"type": "summary_document"}
        if metadata:
            meta.update(metadata)
        return self.record_artifact(
            session_id=session_id,
            task_id=task_id,
            kind="report",
            title=title,
            file_path=None,
            content=summary,
            metadata=meta,
            artifact_id=art_id,
        )

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        """Fetch an artifact by id."""
        self.init_db()
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_dict(row)

    def list_artifacts(
        self,
        session_id: str | None = None,
        is_global: bool = False,
        kind: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        search: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """List artifacts, optionally filtered by session_id and kind, ordered newest-first."""
        self.init_db()
        with self._get_conn() as conn:
            cur = conn.cursor()
            params: list[Any] = []
            clauses: list[str] = []

            if not is_global and session_id:
                clauses.append("session_id = ?")
                params.append(session_id)

            if kind:
                clauses.append("kind = ?")
                params.append(kind)

            if search:
                clauses.append("(title LIKE ? OR file_path LIKE ? OR session_id LIKE ? OR task_id LIKE ?)")
                term = f"%{search}%"
                params.extend([term, term, term, term])

            where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""

            count_query = f"SELECT COUNT(*) FROM artifacts {where_clause}"
            cur.execute(count_query, params)
            total = cur.fetchone()[0]

            query = f"SELECT * FROM artifacts {where_clause} ORDER BY created_at DESC, rowid DESC"
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)
                if offset is not None:
                    query += " OFFSET ?"
                    params.append(offset)

            cur.execute(query, params)
            rows = cur.fetchall()
            return [self._row_to_dict(r) for r in rows], total

    def delete_by_session(self, session_id: str) -> int:
        """Delete all artifacts belonging to session_id. Cascades on session deletion."""
        self.init_db()
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM artifacts WHERE session_id = ?", (session_id,))
            deleted = cur.rowcount
            conn.commit()
            return deleted

    def delete_artifact(self, artifact_id: str) -> bool:
        """Delete an individual artifact by id."""
        self.init_db()
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        meta_raw = d.pop("metadata_json", None)
        try:
            d["metadata"] = json.loads(meta_raw) if meta_raw else {}
        except (json.JSONDecodeError, TypeError):
            d["metadata"] = {}
        return d


GLOBAL_ARTIFACT_STORE: ArtifactStore | None = None


def get_artifact_store() -> ArtifactStore:
    """Return the global default ArtifactStore singleton."""
    global GLOBAL_ARTIFACT_STORE
    if GLOBAL_ARTIFACT_STORE is None:
        GLOBAL_ARTIFACT_STORE = ArtifactStore()
    return GLOBAL_ARTIFACT_STORE
