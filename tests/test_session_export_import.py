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
    attach_dir = tmp_path / "attachments" / sess_id
    attach_dir.mkdir(parents=True, exist_ok=True)
    sample_file = attach_dir / "diagram.png"
    sample_file.write_bytes(b"\x89PNG\r\n\x1a\nFakePngContent")

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
            (json.dumps([{"name": "diagram.png", "size": sample_file.stat().st_size}]), sess_id),
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
        assert "attachments/diagram.png" in names
        assert zf.read("attachments/diagram.png") == b"\x89PNG\r\n\x1a\nFakePngContent"

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
    restored_file = restore_attach_dir / sess_id / "diagram.png"
    assert restored_file.exists()
    assert restored_file.read_bytes() == b"\x89PNG\r\n\x1a\nFakePngContent"

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
