"""S4-M3: session delete must remove the session's files, not just DB rows.

Before the fix:
- ``MediaStore.delete_by_session`` deleted embeddings + item rows only;
  the media items' files on disk survived.
- The SESSION_DELETE cascade in ``routes/websocket.py`` never removed
  ``~/.aether/attachments/<session_id>/`` (materialised uploads/snips).

There is no existing cheap SESSION_DELETE round-trip harness in the
suite (no test exercises SESSION_DELETE_REQUEST), so this is covered
at the store level and at the ``_remove_session_attachments`` helper
level.
"""
from pathlib import Path

from aether_engine.media.store import MediaStore


def test_delete_by_session_unlinks_files_and_tolerates_missing(tmp_path):
    store = MediaStore(db_path=tmp_path / "media.db")

    present = tmp_path / "present.png"
    present.write_bytes(b"present")
    missing = tmp_path / "missing.png"
    missing.write_bytes(b"missing")
    other = tmp_path / "other.png"
    other.write_bytes(b"other")

    store.register_media(
        file_path=str(present), collection="user", media_type="image",
        session_id="sess-s4m3",
    )
    store.register_media(
        file_path=str(missing), collection="user", media_type="image",
        session_id="sess-s4m3",
    )
    store.register_media(
        file_path=str(other), collection="user", media_type="image",
        session_id="sess-other",
    )

    # Second item's file is already gone before the cascade runs.
    missing.unlink()

    deleted = store.delete_by_session("sess-s4m3")

    assert deleted == 2
    _items, total = store.list_media(session_id="sess-s4m3")
    assert total == 0
    # The surviving file belonging to the deleted session is unlinked.
    assert not present.exists()
    # Other session untouched: row and file both remain.
    _items2, total2 = store.list_media(session_id="sess-other")
    assert total2 == 1
    assert other.exists()


def test_remove_session_attachments_removes_directory():
    from aether_engine.routes.websocket import _remove_session_attachments

    session_id = "sess-s4m3-attachments"
    attach_dir = Path.home() / ".aether" / "attachments" / session_id
    attach_dir.mkdir(parents=True, exist_ok=True)
    (attach_dir / "upload.png").write_bytes(b"data")
    nested = attach_dir / "nested"
    nested.mkdir(exist_ok=True)
    (nested / "snip.png").write_bytes(b"snip")

    _remove_session_attachments(session_id)

    assert not attach_dir.exists()


def test_remove_session_attachments_nonexistent_is_noop():
    from aether_engine.routes.websocket import _remove_session_attachments

    # Must not raise for a session that never had attachments.
    _remove_session_attachments("sess-s4m3-does-not-exist")
