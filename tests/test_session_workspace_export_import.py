"""Slice 6 of per-workspace sessions: export provenance + unbound import.

Owner decision (grill Q9, as superseded by the owner's clarification):
there is NO workspace export. Session export (Epic 8, E8-A7.1) stays
session-only; the session's home workspace may appear in the export as
a PROVENANCE LABEL only, and the binding does NOT travel as a filing
instruction — an imported session always lands unbound ("No
workspace"), whatever the file says, and the import result states so.
"""

import json
import zipfile

import pytest

from aether_engine.memory import session_store

ROOT = "/home/user/projects/studeoo"


@pytest.fixture(autouse=True)
def isolated_session_db(tmp_path, monkeypatch):
    """Isolate session store SQLite database to temporary path for each test."""
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(session_store, "DB_PATH", db_file)
    monkeypatch.setattr(session_store, "_initialized", False)
    session_store.init_db()
    yield db_file


def _bound_session(title="Bound Session"):
    sess_id = session_store.create_session(title, workspace=ROOT)
    session_store.add_message(sess_id, "user", "Hello from a bound session")
    session_store.add_message(sess_id, "assistant", "Hi there")
    return sess_id


# ---------------------------------------------------------------------------
# Export: the provenance label
# ---------------------------------------------------------------------------


def test_json_export_carries_workspace_as_data_for_bound_session():
    from aether_engine.session_export.exporter import export_session_json

    sess_id = _bound_session()
    payload = json.loads(export_session_json(sess_id))

    assert payload["workspace"] == ROOT
    # Provenance only: the format does not change.
    assert payload["format_version"] == 1


def test_json_export_workspace_is_null_for_unbound_session():
    from aether_engine.session_export.exporter import export_session_json

    sess_id = session_store.create_session("Unbound Session")
    session_store.add_message(sess_id, "user", "No home workspace")

    payload = json.loads(export_session_json(sess_id))
    assert payload["workspace"] is None


def test_markdown_export_has_one_workspace_line_for_bound_session():
    from aether_engine.session_export.exporter import export_session_markdown

    md = export_session_markdown(_bound_session())
    assert md.count("**Workspace:**") == 1
    assert f"- **Workspace:** `{ROOT}`" in md


def test_markdown_export_workspace_line_for_unbound_session():
    from aether_engine.session_export.exporter import export_session_markdown

    sess_id = session_store.create_session("Unbound Session")
    session_store.add_message(sess_id, "user", "No home workspace")

    md = export_session_markdown(sess_id)
    assert md.count("**Workspace:**") == 1
    assert "- **Workspace:** No workspace" in md


def test_zip_export_carries_the_label_in_both_forms(tmp_path):
    from aether_engine.session_export.exporter import export_session_zip

    sess_id = _bound_session("ZIP Label Session")
    zip_out = tmp_path / "label.zip"
    export_session_zip(sess_id, zip_out)

    with zipfile.ZipFile(zip_out, "r") as zf:
        payload = json.loads(zf.read("session.json").decode("utf-8"))
        transcript = zf.read("transcript.md").decode("utf-8")

    assert payload["workspace"] == ROOT
    assert f"- **Workspace:** `{ROOT}`" in transcript


# ---------------------------------------------------------------------------
# Import: always lands unbound, and says so
# ---------------------------------------------------------------------------


def test_import_json_with_workspace_lands_unbound_and_says_so():
    from aether_engine.session_export.exporter import export_session_json
    from aether_engine.session_export.importer import import_session_json

    sess_id = _bound_session("Exported Bound Session")
    json_str = export_session_json(sess_id)
    session_store.delete_session(sess_id)
    assert session_store.get_session(sess_id) is None

    result = import_session_json(content=json_str)

    assert result["ok"] is True
    assert result["workspace"] is None
    assert "No workspace" in result["workspace_note"]

    restored = session_store.get_session(result["session_id"])
    assert restored is not None
    assert restored["workspace"] is None


def test_import_zip_with_workspace_lands_unbound_and_says_so(tmp_path):
    from aether_engine.session_export.exporter import export_session_zip
    from aether_engine.session_export.importer import import_session_archive

    sess_id = _bound_session("Exported Bound ZIP Session")
    zip_out = tmp_path / "bound.zip"
    export_session_zip(sess_id, zip_out)
    session_store.delete_session(sess_id)
    assert session_store.get_session(sess_id) is None

    result = import_session_archive(archive_path=zip_out)

    assert result["ok"] is True
    assert result["workspace"] is None
    assert "No workspace" in result["workspace_note"]

    restored = session_store.get_session(result["session_id"])
    assert restored is not None
    assert restored["workspace"] is None


def test_import_legacy_json_without_workspace_key_is_unchanged():
    """Files exported before bindings existed import exactly as today."""
    from aether_engine.session_export.importer import import_session_json

    legacy = {
        "format_version": 1,
        "exported_at": 1700000000,
        "id": "legacy-session-id",
        "title": "Legacy Session",
        "created_at": 1700000000,
        "updated_at": 1700000000,
        "messages": [
            {"id": "m1", "role": "user", "content": "Old export", "timestamp": 1700000000},
            {"id": "m2", "role": "assistant", "content": "Indeed", "timestamp": 1700000001},
        ],
    }

    result = import_session_json(content=json.dumps(legacy))

    assert result["ok"] is True
    assert result["session_id"] == "legacy-session-id"
    assert result["message_count"] == 2
    assert result["workspace"] is None
    assert "No workspace" in result["workspace_note"]

    restored = session_store.get_session("legacy-session-id")
    assert restored is not None
    assert restored["workspace"] is None
