"""Wave 2, N-18 [S2]: ``delete_by_session`` must not destroy originals.

The media store's doctrine (module docstring) is an in-place
reference index: "Media files are never moved; the index points at
their in-place filesystem paths." The sibling delete makes file
deletion explicit — ``delete_media(..., delete_underlying_file=
False)`` — yet ``delete_by_session`` unlinked EVERY item's file
unconditionally, so deleting/cleaning a chat session destroyed the
user's original files at their real locations.

The corrected contract mirrors ``delete_media``: rows are removed;
files are unlinked only when the caller opts in explicitly.
"""
from __future__ import annotations

from pathlib import Path

from aether_engine.media.store import MediaStore


def _store_with_user_originals(tmp_path: Path) -> tuple[MediaStore, Path, Path]:
    store = MediaStore(db_path=tmp_path / "media.db")
    # A user's original photo, living at its own real location —
    # the index merely points at it.
    original = tmp_path / "camera" / "IMG_0001.jpg"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"user-original-bytes")
    other_session = tmp_path / "camera" / "IMG_0002.jpg"
    other_session.write_bytes(b"other-session-bytes")
    store.register_media(
        file_path=str(original), collection="user", media_type="image",
        session_id="sess-doomed")
    store.register_media(
        file_path=str(other_session), collection="user", media_type="image",
        session_id="sess-kept")
    return store, original, other_session


def test_delete_by_session_default_keeps_user_files(tmp_path):
    store, original, other_session = _store_with_user_originals(tmp_path)
    deleted = store.delete_by_session("sess-doomed")
    assert deleted == 1
    _items, total = store.list_media(session_id="sess-doomed")
    assert total == 0
    # The row is gone; the user's original file is NOT.
    assert original.exists()
    assert original.read_bytes() == b"user-original-bytes"
    assert other_session.exists()


def test_delete_by_session_explicit_opt_in_unlinks(tmp_path):
    store, original, other_session = _store_with_user_originals(tmp_path)
    deleted = store.delete_by_session(
        "sess-doomed", delete_underlying_files=True)
    assert deleted == 1
    assert not original.exists()
    # The opt-in is still scoped to the named session.
    assert other_session.exists()
    _items, total = store.list_media(session_id="sess-kept")
    assert total == 1
