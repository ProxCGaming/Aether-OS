"""Session import deserializer and validator for JSON and ZIP archives (Epic 8, E8-A7.1).

Restores living sessions into the database with full round-trip fidelity,
recreating messages, tool records, and attachments while enforcing strict
security validation (including Zip-Slip prevention and fail-closed state safety).
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import sqlite3
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from aether_engine.memory import session_store

logger = logging.getLogger("aether_engine.session_export.importer")


def data_home() -> Path:
    return Path.home() / ".aether"


def attachments_dir_for_session(session_id: str) -> Path:
    d = data_home() / "attachments" / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ensure_attachments_column(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ALTER TABLE session_messages ADD COLUMN attachments TEXT")
    except sqlite3.OperationalError:
        pass


def _restore_session_to_db(session_data: dict[str, Any]) -> tuple[str, str, int]:
    """Insert session and messages into SQLite in a single transaction."""
    session_store.init_db()
    raw_id = session_data.get("id")
    title = (session_data.get("title") or "Imported Session").strip()
    created_at = int(session_data.get("created_at") or time.time())
    updated_at = int(session_data.get("updated_at") or time.time())
    messages = session_data.get("messages", [])

    with session_store._get_conn() as conn:
        _ensure_attachments_column(conn)
        cur = conn.cursor()

        # If session id is provided and not already in use, reuse it; otherwise create fresh UUID
        session_id = str(raw_id) if raw_id else str(uuid.uuid4())
        cur.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,))
        if cur.fetchone():
            session_id = str(uuid.uuid4())

        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (session_id, title, created_at, updated_at),
        )

        for msg in messages:
            msg_id = str(msg.get("id") or uuid.uuid4())
            role = str(msg.get("role") or "user")
            content = str(msg.get("content") or "")
            ts = int(msg.get("timestamp") or time.time())

            thoughts = msg.get("thoughts")
            thoughts_json = json.dumps(thoughts) if thoughts is not None else None

            attachments = msg.get("attachments")
            attachments_json = json.dumps(attachments) if attachments is not None else None

            # Check for message id collision, if duplicate generate fresh UUID
            cur.execute("SELECT 1 FROM session_messages WHERE id = ?", (msg_id,))
            if cur.fetchone():
                msg_id = str(uuid.uuid4())

            conn.execute(
                "INSERT INTO session_messages (id, session_id, role, content, thoughts, attachments, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (msg_id, session_id, role, content, thoughts_json, attachments_json, ts),
            )

        conn.commit()

    return session_id, title, len(messages)


def import_session_json(content: str | None = None, file_path: str | Path | None = None) -> dict[str, Any]:
    """Validate and import a session from JSON string or file."""
    try:
        if content is not None:
            raw = content
        elif file_path is not None:
            p = Path(file_path)
            if not p.exists() or not p.is_file():
                return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": f"File not found: {file_path}"}
            raw = p.read_text(encoding="utf-8")
        else:
            return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": "No content or file_path provided"}

        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": "Session JSON must be an object"}

        if "messages" not in data or not isinstance(data["messages"], list):
            return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": "Session JSON missing valid 'messages' list"}

        sess_id, title, count = _restore_session_to_db(data)
        return {
            "ok": True,
            "session_id": sess_id,
            "title": title,
            "message_count": count,
            "error": None,
        }
    except json.JSONDecodeError as e:
        return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": f"Invalid JSON format: {e}"}
    except Exception as e:
        logger.exception("Failed to import session from JSON")
        return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": f"Import failed: {e}"}


def _validate_zip_member(name: str, target_dir: Path) -> Path:
    """Zip-Slip protection: ensure the member path stays strictly inside target_dir."""
    if name.startswith(("/", "\\")):
        raise ValueError(f"Insecure archive path traversal detected: {name}")
    parts = Path(name).parts
    if ".." in parts:
        raise ValueError(f"Insecure archive path traversal detected: {name}")
    dest = (target_dir / name).resolve()
    target_resolved = target_dir.resolve()
    if not str(dest).startswith(str(target_resolved)):
        raise ValueError(f"Insecure archive path traversal detected: {name}")
    return dest


def import_session_archive(archive_path: str | Path | None = None, content: str | bytes | None = None) -> dict[str, Any]:
    """Validate and restore a session from a ZIP archive containing session.json and attachments."""
    try:
        zip_bytes: bytes | None = None
        if content is not None:
            if isinstance(content, bytes):
                zip_bytes = content
            elif isinstance(content, str):
                try:
                    zip_bytes = base64.b64decode(content)
                except (ValueError, binascii.Error):
                    zip_bytes = content.encode("utf-8")
            file_obj = io.BytesIO(zip_bytes)
        elif archive_path is not None:
            p = Path(archive_path)
            if not p.exists() or not p.is_file():
                return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": f"Archive file not found: {archive_path}"}
            file_obj = p.open("rb")
        else:
            return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": "No archive_path or content provided"}

        with file_obj:
            if not zipfile.is_zipfile(file_obj):
                return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": "Invalid ZIP archive"}

            file_obj.seek(0)
            with zipfile.ZipFile(file_obj, "r") as zf:
                # 1. Zip-slip verification for all members
                dummy_target = Path("/tmp/check")
                for member_name in zf.namelist():
                    _validate_zip_member(member_name, dummy_target)

                # 2. Check for session.json
                if "session.json" not in zf.namelist():
                    return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": "Archive missing session.json"}

                raw_json = zf.read("session.json").decode("utf-8")
                session_data = json.loads(raw_json)
                if not isinstance(session_data, dict) or "messages" not in session_data:
                    return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": "Corrupt or invalid session archive"}

                # 3. Restore database records
                sess_id, title, count = _restore_session_to_db(session_data)

                # 4. Extract attachments to attachments folder
                target_attach_dir = attachments_dir_for_session(sess_id)
                for member_name in zf.namelist():
                    if member_name.startswith("attachments/") and not member_name.endswith("/"):
                        rel_path = member_name[len("attachments/"):]
                        dest_path = _validate_zip_member(rel_path, target_attach_dir)
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        dest_path.write_bytes(zf.read(member_name))

                return {
                    "ok": True,
                    "session_id": sess_id,
                    "title": title,
                    "message_count": count,
                    "error": None,
                }
    except zipfile.BadZipFile as e:
        return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": f"Corrupt ZIP archive: {e}"}
    except ValueError as e:
        return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": str(e)}
    except Exception as e:
        logger.exception("Failed to import session from archive")
        return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": f"Import failed: {e}"}
