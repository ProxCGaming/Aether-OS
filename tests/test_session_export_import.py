import json
import sqlite3
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

import aether_engine.routes.websocket as websocket_mod
from aether_common.contracts import Event, EventType
from aether_engine.app import app
from aether_engine.memory import session_store


@pytest.fixture(autouse=True)
def isolated_session_db(tmp_path, monkeypatch):
    """Isolate session store SQLite database to temporary path for each test."""
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(session_store, "DB_PATH", db_file)
    monkeypatch.setattr(session_store, "_initialized", False)
    session_store.init_db()
    yield db_file


def test_contracts_exist():
    """Verify contracts define the required session export/import EventTypes."""
    assert hasattr(EventType, "SESSION_EXPORT_REQUEST")
    assert hasattr(EventType, "SESSION_EXPORT_RESPONSE")
    assert hasattr(EventType, "SESSION_IMPORT_REQUEST")
    assert hasattr(EventType, "SESSION_IMPORT_RESPONSE")
    assert EventType.SESSION_EXPORT_REQUEST == "SESSION_EXPORT_REQUEST"
    assert EventType.SESSION_EXPORT_RESPONSE == "SESSION_EXPORT_RESPONSE"
    assert EventType.SESSION_IMPORT_REQUEST == "SESSION_IMPORT_REQUEST"
    assert EventType.SESSION_IMPORT_RESPONSE == "SESSION_IMPORT_RESPONSE"


def test_markdown_export_structure_and_redaction():
    from aether_engine.session_export.exporter import export_session_markdown

    sess_id = session_store.create_session("Export Test Session")
    secret_key = "sk-abcdef1234567890abcdef"
    aws_key = "AKIA1234567890123456"

    # Message with tool activity and secret patterns
    session_store.add_message(
        sess_id,
        "user",
        f"Here is my OpenAI key: {secret_key} and AWS key: {aws_key}",
    )
    thoughts = [
        {
            "type": "TOOL_ACTIVITY",
            "payload": {
                "tool_name": "fetch_api",
                "tool_args": {"api_key": secret_key, "target": "https://api.example.com"},
                "status": "success",
                "result": f"Authenticated successfully with {secret_key}",
            },
        },
        {
            "node": "supervisor",
            "text": "Planning next step without exposing keys.",
        },
    ]
    session_store.add_message(
        sess_id,
        "assistant",
        "I have processed your request.",
        thoughts=thoughts,
    )

    md = export_session_markdown(sess_id)
    assert isinstance(md, str)
    assert "Export Test Session" in md
    assert "Here is my OpenAI key" in md
    assert "TOOL_ACTIVITY" in md or "fetch_api" in md
    assert "supervisor" in md or "Planning next step" in md

    # Diagnostics redaction checks: secrets must NOT appear anywhere in the export
    assert secret_key not in md
    assert aws_key not in md
    assert "[REDACTED]" in md


def test_json_export_structure_and_redaction():
    from aether_engine.session_export.exporter import export_session_json

    sess_id = session_store.create_session("JSON Test Session")
    secret_key = "sk-jsonsecret1234567890"

    session_store.add_message(
        sess_id,
        "user",
        f"Config key: {secret_key}",
    )
    session_store.add_message(
        sess_id,
        "assistant",
        "Done",
        thoughts=[{"type": "TOOL_ACTIVITY", "payload": {"secret": secret_key}}],
    )

    json_str = export_session_json(sess_id)
    data = json.loads(json_str)

    assert data["id"] == sess_id
    assert data["title"] == "JSON Test Session"
    assert len(data["messages"]) == 2
    assert secret_key not in json_str
    assert "[REDACTED]" in json_str


def test_zip_export_and_roundtrip_fidelity(tmp_path):
    from aether_engine.session_export.exporter import export_session_zip
    from aether_engine.session_export.importer import import_session_archive

    sess_id = session_store.create_session("Roundtrip ZIP Session")
    secret_key = "sk-roundtrip1234567890"

    # Setup dummy attachment file in ~/.aether/attachments/<session_id>/
    # F-07: this round-trip uses a UTF-8 text attachment — binary
    # attachments are now deliberately omitted from ZIP exports (they
    # cannot be redaction-verified; see the F-07 tests at the end of this
    # file), so a binary fixture can no longer round-trip by design.
    attach_dir = tmp_path / "attachments" / sess_id
    attach_dir.mkdir(parents=True, exist_ok=True)
    sample_file = attach_dir / "diagram.txt"
    sample_file.write_bytes(b"Fake diagram (text) content\n")

    thoughts = [
        {
            "type": "TOOL_ACTIVITY",
            "payload": {
                "tool_name": "bash",
                "tool_args": {"cmd": "ls"},
                "status": "success",
                "result": "file1.txt\nfile2.txt",
            },
        }
    ]
    # Add message with thoughts and attachment reference
    session_store.add_message(
        sess_id,
        "user",
        f"Attached screenshot with key {secret_key}",
        thoughts=None,
    )
    # Store attachment info on message
    with session_store._get_conn() as conn:
        try:
            conn.execute("ALTER TABLE session_messages ADD COLUMN attachments TEXT")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            "UPDATE session_messages SET attachments = ? WHERE session_id = ?",
            (json.dumps([{"name": "diagram.txt", "size": sample_file.stat().st_size}]), sess_id),
        )
        conn.commit()

    session_store.add_message(
        sess_id,
        "assistant",
        "Got it, processed the file.",
        thoughts=thoughts,
    )

    zip_out = tmp_path / "exports" / f"{sess_id}.zip"
    zip_out.parent.mkdir(parents=True, exist_ok=True)

    with patch("aether_engine.session_export.exporter.attachments_dir", return_value=attach_dir):
        export_session_zip(sess_id, zip_out)

    assert zip_out.exists()
    assert secret_key not in zip_out.read_bytes().decode("latin1", errors="ignore")

    with zipfile.ZipFile(zip_out, "r") as zf:
        names = zf.namelist()
        assert "session.json" in names
        assert "transcript.md" in names
        assert "attachments/diagram.txt" in names
        assert zf.read("attachments/diagram.txt") == b"Fake diagram (text) content\n"

    # Delete session from DB
    deleted = session_store.delete_session(sess_id)
    assert deleted is True
    assert session_store.get_session(sess_id) is None

    # Import the ZIP archive back
    restore_attach_dir = tmp_path / "restored_attachments"
    with patch("aether_engine.session_export.importer.attachments_dir_for_session", lambda sid: restore_attach_dir / sid):
        result = import_session_archive(archive_path=zip_out)

    assert result["ok"] is True
    assert result["session_id"] == sess_id
    assert result["title"] == "Roundtrip ZIP Session"
    assert result["message_count"] == 2
    assert result["error"] is None

    # Verify session restored in DB
    restored = session_store.get_session(sess_id)
    assert restored is not None
    assert restored["title"] == "Roundtrip ZIP Session"
    assert len(restored["messages"]) == 2
    assert restored["messages"][1]["thoughts"] == thoughts

    # Verify attachment restored on disk
    restored_file = restore_attach_dir / sess_id / "diagram.txt"
    assert restored_file.exists()
    assert restored_file.read_bytes() == b"Fake diagram (text) content\n"

    # Accepting new messages:
    new_msg_id = session_store.add_message(sess_id, "user", "Continuing the conversation.")
    assert new_msg_id is not None
    updated = session_store.get_session(sess_id)
    assert len(updated["messages"]) == 3


def test_import_json_roundtrip():
    from aether_engine.session_export.exporter import export_session_json
    from aether_engine.session_export.importer import import_session_json

    sess_id = session_store.create_session("JSON Roundtrip Session")
    session_store.add_message(sess_id, "user", "Message 1")
    session_store.add_message(sess_id, "assistant", "Response 1")

    json_str = export_session_json(sess_id)
    session_store.delete_session(sess_id)
    assert session_store.get_session(sess_id) is None

    result = import_session_json(content=json_str)
    assert result["ok"] is True
    assert result["session_id"] == sess_id
    assert result["message_count"] == 2

    restored = session_store.get_session(sess_id)
    assert restored is not None
    assert len(restored["messages"]) == 2


def test_corrupt_zip_refusal_no_state_change(tmp_path):
    from aether_engine.session_export.importer import import_session_archive

    initial_sessions = session_store.list_sessions()

    # Corrupt zip (not a zip archive)
    bad_zip = tmp_path / "corrupt.zip"
    bad_zip.write_bytes(b"This is not a zip file at all!")

    result = import_session_archive(archive_path=bad_zip)
    assert result["ok"] is False
    assert result["error"] is not None
    assert "zip" in result["error"].lower() or "invalid" in result["error"].lower()

    # Database state must be unchanged
    assert session_store.list_sessions() == initial_sessions


def test_zip_slip_refusal_no_state_change(tmp_path):
    from aether_engine.session_export.importer import import_session_archive

    initial_sessions = session_store.list_sessions()

    # Create zip containing path traversal ../evil.txt
    malicious_zip = tmp_path / "slip.zip"
    with zipfile.ZipFile(malicious_zip, "w") as zf:
        zf.writestr("../evil.txt", "evil payload")
        zf.writestr("session.json", json.dumps({"title": "Evil", "messages": []}))

    result = import_session_archive(archive_path=malicious_zip)
    assert result["ok"] is False
    assert result["error"] is not None
    assert "traversal" in result["error"].lower() or "slip" in result["error"].lower() or "insecure" in result["error"].lower()

    # Database state must be unchanged
    assert session_store.list_sessions() == initial_sessions
    assert not (tmp_path / "evil.txt").exists()


def test_corrupt_json_refusal_no_state_change():
    from aether_engine.session_export.importer import import_session_json

    initial_sessions = session_store.list_sessions()

    # Bad JSON syntax
    result = import_session_json(content="{invalid_json: 123")
    assert result["ok"] is False
    assert result["error"] is not None
    assert session_store.list_sessions() == initial_sessions

    # Missing required keys
    result2 = import_session_json(content=json.dumps({"some_key": "some_value"}))
    assert result2["ok"] is False
    assert result2["error"] is not None
    assert session_store.list_sessions() == initial_sessions


def test_websocket_export_and_import_handlers(tmp_path):
    sess_id = session_store.create_session("WS Export Session")
    session_store.add_message(sess_id, "user", "Hello over WS")
    session_store.add_message(sess_id, "assistant", "Reply over WS")

    with (
        patch.object(websocket_mod, "SecretStore") as mock_store,
        patch.object(websocket_mod, "get_pending_approvals", AsyncMock(return_value=[])),
        TestClient(app) as client,
    ):
        mock_store.return_value.list_providers.return_value = []
        token = websocket_mod.engine_state.auth_token
        with client.websocket_connect(f"/ws/tasks?token={token}") as ws:
            ws.receive_text()  # greeting / initial connection event

            # Export markdown
            ws.send_text(Event(
                type=EventType.SESSION_EXPORT_REQUEST,
                payload={"session_id": sess_id, "format": "markdown"},
            ).to_json())
            resp_md = Event.from_json(ws.receive_text())
            assert resp_md.type == EventType.SESSION_EXPORT_RESPONSE
            assert resp_md.payload["format"] == "markdown"
            assert "Hello over WS" in resp_md.payload["content"]
            assert resp_md.payload["error"] is None

            # Export JSON
            ws.send_text(Event(
                type=EventType.SESSION_EXPORT_REQUEST,
                payload={"session_id": sess_id, "format": "json"},
            ).to_json())
            resp_json = Event.from_json(ws.receive_text())
            assert resp_json.type == EventType.SESSION_EXPORT_RESPONSE
            assert resp_json.payload["format"] == "json"
            exported_json = resp_json.payload["content"]
            assert resp_json.payload["error"] is None

            # Export ZIP
            ws.send_text(Event(
                type=EventType.SESSION_EXPORT_REQUEST,
                payload={"session_id": sess_id, "format": "zip"},
            ).to_json())
            resp_zip = Event.from_json(ws.receive_text())
            assert resp_zip.type == EventType.SESSION_EXPORT_RESPONSE
            assert resp_zip.payload["format"] == "zip"
            assert resp_zip.payload["zip_path"] is not None
            assert Path(resp_zip.payload["zip_path"]).exists()

            # Delete session
            session_store.delete_session(sess_id)
            assert session_store.get_session(sess_id) is None

            # Import JSON via WS
            ws.send_text(Event(
                type=EventType.SESSION_IMPORT_REQUEST,
                payload={"format": "json", "content": exported_json},
            ).to_json())
            resp_import = Event.from_json(ws.receive_text())
            assert resp_import.type == EventType.SESSION_IMPORT_RESPONSE
            assert resp_import.payload["ok"] is True
            assert resp_import.payload["session_id"] == sess_id
            assert resp_import.payload["message_count"] == 2


# --- F-07: ZIP export must not bypass redaction for attachments --------------
# Canary pattern used below: ``sk-exportcanary1234567890`` — it matches the
# ``sk-[A-Za-z0-9_\-]{8,}`` secret pattern in
# ``aether_engine/audit.py::_scrub_string`` (the same scrubber applied to
# session.json / transcript.md), so a correctly redacted export must replace
# it with ``[REDACTED]`` and it must never appear in any ZIP entry.


def test_zip_export_scrubs_text_attachment_from_attachments_dir(tmp_path):
    from aether_engine.session_export.exporter import export_session_zip

    canary = "sk-exportcanary1234567890"
    sess_id = session_store.create_session("ZIP Text Attachment Redaction")
    session_store.add_message(sess_id, "user", "See attached notes.")

    attach_dir = tmp_path / "attachments" / sess_id
    attach_dir.mkdir(parents=True, exist_ok=True)
    (attach_dir / "notes.txt").write_text(
        f"key: {canary}\nkeep this line\n", encoding="utf-8"
    )

    zip_out = tmp_path / "exports" / "text-attachment.zip"
    with patch(
        "aether_engine.session_export.exporter.attachments_dir",
        return_value=attach_dir,
    ):
        export_session_zip(sess_id, zip_out)

    assert zip_out.exists()
    with zipfile.ZipFile(zip_out, "r") as zf:
        names = zf.namelist()
        assert "attachments/notes.txt" in names
        for name in names:
            assert canary.encode() not in zf.read(name), (
                f"canary leaked in ZIP entry {name}"
            )
        content = zf.read("attachments/notes.txt")
        assert b"[REDACTED]" in content
        assert b"keep this line" in content


def test_zip_export_omits_binary_attachment_and_lists_it_in_manifest(tmp_path):
    from aether_engine.session_export.exporter import export_session_zip

    canary = "sk-exportcanary1234567890"
    sess_id = session_store.create_session("ZIP Binary Attachment Omission")
    session_store.add_message(sess_id, "user", "See attached binary.")

    attach_dir = tmp_path / "attachments" / sess_id
    attach_dir.mkdir(parents=True, exist_ok=True)
    # Not valid UTF-8 (0xff/0x89 lead bytes), with the canary embedded.
    (attach_dir / "secret.bin").write_bytes(
        b"\xff\xfe\x00" + canary.encode() + b"\x89\xff\x00"
    )

    zip_out = tmp_path / "exports" / "binary-attachment.zip"
    with patch(
        "aether_engine.session_export.exporter.attachments_dir",
        return_value=attach_dir,
    ):
        export_session_zip(sess_id, zip_out)

    assert zip_out.exists()
    with zipfile.ZipFile(zip_out, "r") as zf:
        names = zf.namelist()
        assert "attachments/secret.bin" not in names
        for name in names:
            assert canary.encode() not in zf.read(name), (
                f"canary leaked in ZIP entry {name}"
            )
        assert "attachments/OMITTED.txt" in names
        omitted = zf.read("attachments/OMITTED.txt").decode("utf-8")
        assert "secret.bin" in omitted
        assert "binary" in omitted.lower()


def test_zip_import_reconnects_attachments_under_new_session_id(tmp_path):
    """M14: when import mints a fresh session id (the exported id is still
    taken), restored message attachment paths must point at the files as
    restored under the NEW session's attachments directory — not at the
    original session's (dead) locations."""
    from aether_engine.session_export.exporter import export_session_zip
    from aether_engine.session_export.importer import import_session_archive

    sess_id = session_store.create_session("Import Reconnect Session")

    # Attachment living in the session's own attachments dir, referenced
    # from a message by its original absolute path.
    attach_dir = tmp_path / "attachments" / sess_id
    attach_dir.mkdir(parents=True, exist_ok=True)
    in_dir_file = attach_dir / "diagram.txt"
    in_dir_file.write_bytes(b"Fake diagram (text) content\n")
    session_store.add_message(
        sess_id,
        "user",
        "See the diagram",
        attachments=[{"name": "diagram.txt", "path": str(in_dir_file)}],
    )

    # Attachment living elsewhere, referenced by absolute path only.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(parents=True, exist_ok=True)
    ext_file = elsewhere / "notes.txt"
    ext_file.write_bytes(b"External notes content\n")
    session_store.add_message(
        sess_id,
        "assistant",
        "And the notes",
        attachments=[{"name": "notes.txt", "path": str(ext_file)}],
    )

    zip_out = tmp_path / "exports" / "reconnect.zip"
    with patch(
        "aether_engine.session_export.exporter.attachments_dir",
        return_value=attach_dir,
    ):
        export_session_zip(sess_id, zip_out)

    with zipfile.ZipFile(zip_out, "r") as zf:
        names = zf.namelist()
        assert "attachments/diagram.txt" in names
        assert "attachments/notes.txt" in names

    # The original session still exists, so import must mint a NEW id and
    # land the files beneath the new session's attachments directory.
    restore_root = tmp_path / "restored_attachments"
    with patch(
        "aether_engine.session_export.importer.attachments_dir_for_session",
        lambda sid: restore_root / sid,
    ):
        result = import_session_archive(archive_path=zip_out)

    assert result["ok"] is True
    new_id = result["session_id"]
    assert new_id != sess_id

    restored = session_store.get_session(new_id)
    assert restored is not None
    assert len(restored["messages"]) == 2

    by_name = {}
    for msg in restored["messages"]:
        for att in msg["attachments"]:
            by_name[att["name"]] = att

    expected_dir = restore_root / new_id
    for name, payload in (
        ("diagram.txt", b"Fake diagram (text) content\n"),
        ("notes.txt", b"External notes content\n"),
    ):
        att = by_name[name]
        # The recorded path must have been rewritten to the restored file…
        assert att["path"] == str(expected_dir / name)
        # …and that path must actually resolve to the restored bytes.
        restored_file = Path(att["path"])
        assert restored_file.exists()
        assert restored_file.read_bytes() == payload


def test_zip_import_crc_failure_leaves_no_session_or_files(tmp_path):
    """M15: an attachment whose CRC fails must refuse the whole import —
    no session row, no messages, and no partially extracted files — even
    though the failure is only discovered when the member is read, after
    another attachment has already been read successfully."""
    from aether_engine.session_export.importer import import_session_archive

    session_payload = {
        "id": "crc-victim-session",
        "title": "CRC Victim",
        "created_at": 1700000000,
        "updated_at": 1700000000,
        "messages": [
            {
                "id": "crc-msg-1",
                "role": "user",
                "content": "with attachments",
                "timestamp": 1700000000,
                "attachments": [
                    {"name": "good.txt", "path": "/old/home/good.txt"},
                    {"name": "bad.txt", "path": "/old/home/bad.txt"},
                ],
            }
        ],
    }

    good_payload = b"perfectly fine attachment bytes\n"
    bad_payload = b"UNIQUE-CORRUPTIBLE-PAYLOAD-0123456789"
    raw_zip = tmp_path / "crc.zip"
    with zipfile.ZipFile(raw_zip, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("session.json", json.dumps(session_payload))
        zf.writestr("attachments/good.txt", good_payload)
        zf.writestr("attachments/bad.txt", bad_payload)

    # Flip one byte inside the stored (uncompressed) bad member: reading
    # it back now fails its CRC check.
    blob = bytearray(raw_zip.read_bytes())
    idx = blob.index(bad_payload)
    blob[idx + 5] ^= 0xFF
    raw_zip.write_bytes(bytes(blob))

    initial_sessions = session_store.list_sessions()

    restore_root = tmp_path / "restored_attachments"
    with patch(
        "aether_engine.session_export.importer.attachments_dir_for_session",
        lambda sid: restore_root / sid,
    ):
        result = import_session_archive(archive_path=raw_zip)

    assert result["ok"] is False
    assert result["error"] is not None

    # No state change: no new session (and no orphaned messages under it).
    assert session_store.list_sessions() == initial_sessions
    assert session_store.get_session("crc-victim-session") is None

    # No extracted files left behind anywhere under the restore root.
    if restore_root.exists():
        leftovers = [p for p in restore_root.rglob("*") if p.is_file()]
        assert leftovers == []


def test_zip_export_scrubs_and_omits_message_referenced_attachments(tmp_path):
    """The message-referenced-paths loop gets the same treatment as the
    attachments-dir sweep: text is scrubbed, binary is omitted + listed."""
    from aether_engine.session_export.exporter import export_session_zip

    canary = "sk-exportcanary1234567890"
    sess_id = session_store.create_session("ZIP Referenced Attachment Redaction")

    ref_dir = tmp_path / "referenced"
    ref_dir.mkdir(parents=True, exist_ok=True)
    text_ref = ref_dir / "ref-notes.txt"
    text_ref.write_text(f"token is {canary}\nreferenced keep\n", encoding="utf-8")
    bin_ref = ref_dir / "ref-secret.bin"
    bin_ref.write_bytes(b"\x89\xff" + canary.encode() + b"\xfe\x00")

    session_store.add_message(
        sess_id,
        "user",
        "Attachments referenced by path.",
        attachments=[
            {"name": "ref-notes.txt", "path": str(text_ref)},
            {"name": "ref-secret.bin", "path": str(bin_ref)},
        ],
    )

    empty_attach_dir = tmp_path / "attachments" / sess_id
    empty_attach_dir.mkdir(parents=True, exist_ok=True)

    zip_out = tmp_path / "exports" / "referenced-attachments.zip"
    with patch(
        "aether_engine.session_export.exporter.attachments_dir",
        return_value=empty_attach_dir,
    ):
        export_session_zip(sess_id, zip_out)

    assert zip_out.exists()
    with zipfile.ZipFile(zip_out, "r") as zf:
        names = zf.namelist()
        assert "attachments/ref-notes.txt" in names
        assert "attachments/ref-secret.bin" not in names
        for name in names:
            assert canary.encode() not in zf.read(name), (
                f"canary leaked in ZIP entry {name}"
            )
        content = zf.read("attachments/ref-notes.txt")
        assert b"[REDACTED]" in content
        assert b"referenced keep" in content
        assert "attachments/OMITTED.txt" in names
        omitted = zf.read("attachments/OMITTED.txt").decode("utf-8")
        assert "ref-secret.bin" in omitted
