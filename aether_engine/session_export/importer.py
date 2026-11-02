"""Session import deserializer and validator for JSON and ZIP archives (Epic 8, E8-A7.1).

Restores living sessions into the database with full round-trip fidelity,
recreating messages, tool records, and attachments while enforcing strict
security validation (including Zip-Slip prevention and fail-closed state safety).
"""

from __future__ import annotations

import base64
import binascii
import copy
import io
import json
import logging
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from aether_engine.memory import session_store

logger = logging.getLogger("aether_engine.session_export.importer")

# Per-workspace sessions (grill Q9, superseded form): there is no
# workspace export and a session's binding does NOT travel as a filing
# instruction. An export may name the session's home workspace as a
# provenance label; an import IGNORES that value for filing and always
# restores the session unbound ("No workspace"). Successful import
# results state the outcome under ``workspace`` (always None) and
# ``workspace_note``.
IMPORT_WORKSPACE_NOTE = (
    "Imported into No workspace (the workspace named in the export is "
    "a provenance label only; the binding does not transfer)"
)

# M15 residual (EGM S4 M12): format gate + size caps.
#
# Every export this app produces carries ``format_version: 1`` (the
# exporter's payload builder stamps it on both the JSON form and the
# ZIP's session.json). Import validates it before anything is
# written: a foreign file that merely has a ``messages`` list is not
# an Aether export and is refused.
SUPPORTED_FORMAT_VERSIONS = (1,)

# Size caps, sized far beyond any real session — a transcript is
# text and attachments are user-shared files — while bounding what
# a hostile or corrupt archive can make the importer expand into
# memory/disk before a refusal can occur (zip-bomb exposure).
MAX_IMPORT_MEMBERS = 10_000  # session.json + transcript + attachments
MAX_IMPORT_MEMBER_BYTES = 512 * 1024 * 1024  # one member, expanded
MAX_IMPORT_TOTAL_BYTES = 2 * 1024 * 1024 * 1024  # all members, expanded
MAX_SESSION_JSON_BYTES = 64 * 1024 * 1024  # session.json is parsed in memory
MAX_IMPORT_JSON_BYTES = 256 * 1024 * 1024  # whole JSON-form payload


def _validate_format_version(session_data: dict[str, Any]) -> None:
    """Refuse anything that is not a supported Aether export."""
    version = session_data.get("format_version")
    if version is None:
        raise ValueError(
            "Not an Aether session export: missing format_version"
        )
    if version not in SUPPORTED_FORMAT_VERSIONS:
        supported = ", ".join(str(v) for v in SUPPORTED_FORMAT_VERSIONS)
        raise ValueError(
            f"Unsupported session export format_version: {version!r} "
            f"(supported: {supported})"
        )


def _read_member_capped(
    zf: zipfile.ZipFile, name: str, limit: int
) -> bytes:
    """Read one archive member, refusing past ``limit`` expanded bytes.

    Streams in chunks so an oversized member is refused after
    ``limit`` bytes instead of being expanded in full first.
    """
    chunks: list[bytes] = []
    total = 0
    with zf.open(name, "r") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError(
                    f"Archive member {name!r} exceeds the import size "
                    f"cap of {limit} bytes"
                )
            chunks.append(chunk)
    return b"".join(chunks)


def _stage_member_capped(
    zf: zipfile.ZipFile,
    name: str,
    dest: Path,
    *,
    total_so_far: int,
) -> int:
    """Stream one member to ``dest``, enforcing the size caps.

    Returns the member's expanded size. Raises ValueError the moment
    the per-member cap or the running total cap is exceeded — the
    caller stages into a throwaway directory, so a refusal here
    leaves nothing behind.
    """
    written = 0
    with zf.open(name, "r") as fh, dest.open("wb") as out:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_IMPORT_MEMBER_BYTES:
                raise ValueError(
                    f"Archive member {name!r} exceeds the per-file "
                    f"import cap of {MAX_IMPORT_MEMBER_BYTES} bytes"
                )
            if total_so_far + written > MAX_IMPORT_TOTAL_BYTES:
                raise ValueError(
                    f"Archive exceeds the total import cap of "
                    f"{MAX_IMPORT_TOTAL_BYTES} bytes (expanded)"
                )
            out.write(chunk)
    return written


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


def _choose_session_id(raw_id: Any) -> str:
    """Pick the id an import will restore under, before anything is written.

    The archived id is reused when it is free; otherwise a fresh UUID is
    minted. ZIP import needs the id up front so message attachment
    references can be rewritten to the restored files' locations (which
    live beneath the new session's attachments directory) before the
    messages are persisted.
    """
    session_store.init_db()
    candidate = str(raw_id) if raw_id else str(uuid.uuid4())
    with session_store._get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM sessions WHERE id = ?", (candidate,))
        if cur.fetchone():
            candidate = str(uuid.uuid4())
    return candidate


def _restore_session_to_db(
    session_data: dict[str, Any], session_id: str | None = None
) -> tuple[str, str, int]:
    """Insert session and messages into SQLite in a single transaction.

    When ``session_id`` is given it is used verbatim (the caller has
    already verified it is free, e.g. via :func:`_choose_session_id`);
    a collision then aborts the transaction rather than silently
    restoring under a different id than the caller prepared for.
    """
    session_store.init_db()
    raw_id = session_data.get("id")
    title = (session_data.get("title") or "Imported Session").strip()
    created_at = int(session_data.get("created_at") or time.time())
    updated_at = int(session_data.get("updated_at") or time.time())
    messages = session_data.get("messages", [])

    with session_store._get_conn() as conn:
        _ensure_attachments_column(conn)
        cur = conn.cursor()

        if session_id is None:
            # If session id is provided and not already in use, reuse it; otherwise create fresh UUID
            session_id = str(raw_id) if raw_id else str(uuid.uuid4())
            cur.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,))
            if cur.fetchone():
                session_id = str(uuid.uuid4())
        else:
            session_id = str(session_id)
            cur.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,))
            if cur.fetchone():
                raise ValueError(
                    f"Session id {session_id} is already in use"
                )

        # The workspace column is deliberately NOT written here: an
        # imported session always lands unbound (workspace NULL),
        # whatever provenance label the export carried (see
        # IMPORT_WORKSPACE_NOTE).
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
            if len(content) > MAX_IMPORT_JSON_BYTES:
                return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": f"Session JSON exceeds the import size cap of {MAX_IMPORT_JSON_BYTES} bytes"}
            raw = content
        elif file_path is not None:
            p = Path(file_path)
            if not p.exists() or not p.is_file():
                return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": f"File not found: {file_path}"}
            if p.stat().st_size > MAX_IMPORT_JSON_BYTES:
                return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": f"Session JSON exceeds the import size cap of {MAX_IMPORT_JSON_BYTES} bytes"}
            raw = p.read_text(encoding="utf-8")
        else:
            return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": "No content or file_path provided"}

        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": "Session JSON must be an object"}

        _validate_format_version(data)

        if "messages" not in data or not isinstance(data["messages"], list):
            return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": "Session JSON missing valid 'messages' list"}

        sess_id, title, count = _restore_session_to_db(data)
        return {
            "ok": True,
            "session_id": sess_id,
            "title": title,
            "message_count": count,
            "error": None,
            "workspace": None,
            "workspace_note": IMPORT_WORKSPACE_NOTE,
        }
    except json.JSONDecodeError as e:
        return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": f"Invalid JSON format: {e}"}
    except ValueError as e:
        return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": str(e)}
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


def _rewrite_attachment_paths(
    session_data: dict[str, Any],
    staged: dict[str, Path],
    final_dir: Path,
) -> dict[str, Any]:
    """Copy of ``session_data`` with attachment references reconnected (M14).

    The exporter stores each attachment in the archive as
    ``attachments/<name>`` — the attachment's ``name`` when it has one,
    else the basename of its recorded path. Any message attachment whose
    file was restored from the archive gets its ``path`` rewritten to
    the file's location under the new session's attachments directory,
    so the reference resolves to the restored bytes. Attachments whose
    files are not in the archive (for example binaries the exporter
    withheld for redaction safety) have nothing to point at and keep
    their recorded reference untouched.
    """
    by_name: dict[str, str] = {}
    for rel_path in staged:
        by_name.setdefault(Path(rel_path).name, rel_path)

    rewritten = copy.deepcopy(session_data)
    messages = rewritten.get("messages")
    if not isinstance(messages, list):
        return rewritten
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        attachments = msg.get("attachments")
        if not isinstance(attachments, list):
            continue
        for att in attachments:
            if not isinstance(att, dict) or not att.get("path"):
                continue
            rel_path: str | None = None
            name = att.get("name")
            if name and str(name) in by_name:
                rel_path = by_name[str(name)]
            else:
                base = Path(str(att["path"])).name
                if base in by_name:
                    rel_path = by_name[base]
            if rel_path is None:
                recorded = str(att["path"]).replace("\\", "/")
                for candidate in staged:
                    if recorded.endswith("/" + candidate):
                        rel_path = candidate
                        break
            if rel_path is not None:
                att["path"] = str(final_dir / rel_path)
    return rewritten


def _remove_empty_dirs(root: Path) -> None:
    """Remove ``root`` and any directories beneath it that are empty.

    Used to unwind a failed import: only directories this import left
    empty disappear; anything holding pre-existing files is kept.
    """
    if not root.exists():
        return
    for dirpath in sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            dirpath.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def import_session_archive(archive_path: str | Path | None = None, content: str | bytes | None = None) -> dict[str, Any]:
    """Validate and restore a session from a ZIP archive containing session.json and attachments.

    The import is transactional from the caller's point of view (M15):
    every archive member is validated and every attachment is staged
    (which also verifies its CRC) before any state changes; the session
    and its messages are then inserted in one database transaction with
    attachment references already rewritten to their restored locations
    (M14); the staged files are placed last, and if placing them fails
    the restored session is deleted again. A refused import therefore
    leaves no session, no messages, and no extracted files behind.

    Validation happens before any of that (M15 residual, EGM S4 M12):
    the payload must carry a supported ``format_version`` and the
    archive must fit the size caps (member count, per-member and
    total expanded bytes), enforced on the declared sizes AND again
    while streaming members into staging.
    """
    staging_dir: Path | None = None
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
                infos = zf.infolist()
                member_names = [info.filename for info in infos]

                # 1. Zip-slip verification for all members
                dummy_target = Path("/tmp/check")
                for member_name in member_names:
                    _validate_zip_member(member_name, dummy_target)

                # 1b. Size caps (M15 residual): refuse on the
                # archive's declared shape before expanding anything,
                # and enforce the same caps again while streaming
                # below, so a lying header cannot sneak past.
                if len(infos) > MAX_IMPORT_MEMBERS:
                    return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": f"Archive has too many members ({len(infos)} > {MAX_IMPORT_MEMBERS})"}
                declared_total = 0
                for info in infos:
                    if info.file_size > MAX_IMPORT_MEMBER_BYTES:
                        return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": f"Archive member {info.filename!r} exceeds the per-file import cap of {MAX_IMPORT_MEMBER_BYTES} bytes"}
                    if info.filename == "session.json" and info.file_size > MAX_SESSION_JSON_BYTES:
                        return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": f"session.json exceeds the import size cap of {MAX_SESSION_JSON_BYTES} bytes"}
                    declared_total += info.file_size
                if declared_total > MAX_IMPORT_TOTAL_BYTES:
                    return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": f"Archive exceeds the total import cap of {MAX_IMPORT_TOTAL_BYTES} bytes (expanded)"}

                # 2. Check for session.json and validate its shape
                if "session.json" not in member_names:
                    return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": "Archive missing session.json"}

                raw_json = _read_member_capped(
                    zf, "session.json", MAX_SESSION_JSON_BYTES
                ).decode("utf-8")
                session_data = json.loads(raw_json)
                if not isinstance(session_data, dict) or not isinstance(session_data.get("messages"), list):
                    return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": "Corrupt or invalid session archive"}

                _validate_format_version(session_data)

                # 3. Stage every attachment member BEFORE any state
                # change. Reading a member verifies its CRC, so a
                # corrupt attachment refuses the import here — while
                # the database and the attachments folder are still
                # untouched — instead of after the session is committed.
                staging_dir = Path(tempfile.mkdtemp(prefix="aether-import-"))
                staged: dict[str, Path] = {}
                staged_total = 0
                for member_name in member_names:
                    if member_name.startswith("attachments/") and not member_name.endswith("/"):
                        rel_path = member_name[len("attachments/"):]
                        staged_path = _validate_zip_member(rel_path, staging_dir)
                        staged_path.parent.mkdir(parents=True, exist_ok=True)
                        staged_total += _stage_member_capped(
                            zf, member_name, staged_path,
                            total_so_far=staged_total,
                        )
                        staged[rel_path] = staged_path

                # 4. Choose the restored session's id and reconnect the
                # messages' attachment references to the restored files.
                sess_id = _choose_session_id(session_data.get("id"))
                final_dir: Path | None = None
                if staged:
                    final_dir = attachments_dir_for_session(sess_id)
                    session_data = _rewrite_attachment_paths(
                        session_data, staged, final_dir
                    )

                # 5. Restore database records in a single transaction.
                try:
                    sess_id, title, count = _restore_session_to_db(
                        session_data, session_id=sess_id
                    )
                except Exception:
                    if final_dir is not None:
                        _remove_empty_dirs(final_dir)
                    raise

                # 6. Place the staged files. If placing fails, compensate
                # for the committed restore (delete the session, remove
                # any files already placed) so the refusal leaves no trace.
                placed: list[Path] = []
                try:
                    for rel_path, staged_path in staged.items():
                        dest_path = final_dir / rel_path
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(staged_path), str(dest_path))
                        placed.append(dest_path)
                except Exception:
                    for placed_path in placed:
                        placed_path.unlink(missing_ok=True)
                    session_store.delete_session(sess_id)
                    if final_dir is not None:
                        _remove_empty_dirs(final_dir)
                    raise

                return {
                    "ok": True,
                    "session_id": sess_id,
                    "title": title,
                    "message_count": count,
                    "error": None,
                    "workspace": None,
                    "workspace_note": IMPORT_WORKSPACE_NOTE,
                }
    except zipfile.BadZipFile as e:
        return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": f"Corrupt ZIP archive: {e}"}
    except ValueError as e:
        return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": str(e)}
    except Exception as e:
        logger.exception("Failed to import session from archive")
        return {"ok": False, "session_id": "", "title": "", "message_count": 0, "error": f"Import failed: {e}"}
    finally:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)
