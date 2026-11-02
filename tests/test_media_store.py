"""Tests for E8-4.2: Media index + Library section (MediaStore and WebSocket contracts).

Tests cover:
1. MediaStore initialization, schema migration (user_version = 1), tables and indexes.
2. In-place media registration (user attachments, snips, agent generated media) with origin paths preserved.
3. Collection separation ('user' vs 'generated', plus origin filtering like 'snip').
4. Missing file graceful degradation (file_exists flag / status).
5. Metadata updates (tags and notes persistence).
6. Media deletion (removing entry and embedding, optional file deletion).
7. WebSocket endpoints: MEDIA_LIST_REQUEST, MEDIA_DELETE_REQUEST, MEDIA_METADATA_UPDATE_REQUEST.
8. Registration integration: chat attachments and agent tool file writes.
"""
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from aether_common.contracts import EventType


@pytest.fixture
def temp_media_dir(monkeypatch):
    """Temporary directory for media files and media.db."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        db_path = tmppath / "media.db"
        yield tmppath, db_path


class TestMediaStore:
    """Unit tests for MediaStore database operations."""

    def test_schema_init_and_version(self, temp_media_dir):
        _, db_path = temp_media_dir
        from aether_engine.media.store import MediaStore

        store = MediaStore(db_path=db_path)
        store.init_db()

        assert db_path.exists()
        with store._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA user_version")
            version = cur.fetchone()[0]
            assert version == 1

            # Check tables exist
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('media_items', 'media_embeddings')"
            )
            tables = {r[0] for r in cur.fetchall()}
            assert "media_items" in tables
            assert "media_embeddings" in tables

    def test_register_in_place_media_preserves_path(self, temp_media_dir):
        tmppath, db_path = temp_media_dir
        from aether_engine.media.store import MediaStore

        store = MediaStore(db_path=db_path)

        # Create a sample image file on disk
        img_path = tmppath / "photo.png"
        img_path.write_bytes(b"\x89PNG\r\n\x1a\nfakeimagebytes")

        item = store.register_media(
            file_path=str(img_path),
            collection="user",
            media_type="image",
            session_id="sess-test-1",
            message_id="msg-test-1",
            mime_type="image/png",
            probe_metadata={"origin": "user_upload", "camera": "Canon"},
            tags=["sample", "test"],
            user_notes="My test photo",
        )

        assert item["id"] is not None
        assert item["file_path"] == str(img_path)
        assert item["file_size"] == len(b"\x89PNG\r\n\x1a\nfakeimagebytes")
        assert item["collection"] == "user"
        assert item["media_type"] == "image"
        assert item["session_id"] == "sess-test-1"
        assert item["message_id"] == "msg-test-1"
        assert item["tags"] == ["sample", "test"]
        assert item["user_notes"] == "My test photo"
        assert item["probe_metadata"]["origin"] == "user_upload"
        assert item["file_exists"] is True

        # In-place reference verification: file was never moved or copied
        assert img_path.exists()

    def test_collection_separation_and_filtering(self, temp_media_dir):
        tmppath, db_path = temp_media_dir
        from aether_engine.media.store import MediaStore

        store = MediaStore(db_path=db_path)

        snip_path = tmppath / "snip_01.png"
        snip_path.write_bytes(b"snipbytes")
        store.register_media(
            file_path=str(snip_path),
            collection="user",
            media_type="image",
            session_id="sess-1",
            message_id="msg-1",
            probe_metadata={"origin": "snip"},
        )

        user_video_path = tmppath / "clip.mp4"
        user_video_path.write_bytes(b"videobytes")
        store.register_media(
            file_path=str(user_video_path),
            collection="user",
            media_type="video",
            session_id="sess-1",
            message_id="msg-2",
            probe_metadata={"origin": "user_upload"},
        )

        agent_img_path = tmppath / "diagram.png"
        agent_img_path.write_bytes(b"chartbytes")
        store.register_media(
            file_path=str(agent_img_path),
            collection="generated",
            media_type="image",
            session_id="sess-2",
            message_id="msg-3",
            probe_metadata={"origin": "agent_generated", "tool": "write_file"},
        )

        # Query all
        all_items, total = store.list_media(collection="all")
        assert total == 3
        assert len(all_items) == 3

        # Query user collection
        user_items, total_user = store.list_media(collection="user")
        assert total_user == 2
        assert all(it["collection"] == "user" for it in user_items)

        # Query generated collection
        gen_items, total_gen = store.list_media(collection="generated")
        assert total_gen == 1
        assert gen_items[0]["collection"] == "generated"
        assert gen_items[0]["file_path"] == str(agent_img_path)

    def test_missing_file_graceful_degradation(self, temp_media_dir):
        tmppath, db_path = temp_media_dir
        from aether_engine.media.store import MediaStore

        store = MediaStore(db_path=db_path)

        # Register file that exists
        moving_file = tmppath / "temp_snapshot.png"
        moving_file.write_bytes(b"snapshot")
        item = store.register_media(
            file_path=str(moving_file),
            collection="user",
            media_type="image",
        )
        assert item["file_exists"] is True

        # Now remove or move the file externally
        moving_file.unlink()
        assert not moving_file.exists()

        # Re-fetch from store
        fetched = store.get_media(item["id"])
        assert fetched is not None
        assert fetched["file_exists"] is False

        # List should also indicate missing file
        items, _ = store.list_media()
        assert items[0]["id"] == item["id"]
        assert items[0]["file_exists"] is False

    def test_update_metadata_tags_and_notes(self, temp_media_dir):
        tmppath, db_path = temp_media_dir
        from aether_engine.media.store import MediaStore

        store = MediaStore(db_path=db_path)

        f = tmppath / "photo.jpg"
        f.write_bytes(b"jpgbytes")
        item = store.register_media(
            file_path=str(f),
            collection="user",
            media_type="image",
        )

        updated = store.update_metadata(
            media_id=item["id"],
            tags=["nature", "landscape", "sunset"],
            notes="Taken during evening walk",
        )
        assert updated["tags"] == ["nature", "landscape", "sunset"]
        assert updated["notes"] == "Taken during evening walk"

        # Verify persistence on re-query
        reloaded = store.get_media(item["id"])
        assert reloaded["tags"] == ["nature", "landscape", "sunset"]
        assert reloaded["user_notes"] == "Taken during evening walk"

    def test_delete_media_entry_and_embedding_cleanup(self, temp_media_dir):
        tmppath, db_path = temp_media_dir
        from aether_engine.media.store import MediaStore

        store = MediaStore(db_path=db_path)

        f = tmppath / "chart.png"
        f.write_bytes(b"chartdata")
        item = store.register_media(
            file_path=str(f),
            collection="generated",
            media_type="image",
        )

        # Insert a dummy embedding for this item
        with store._get_conn() as conn:
            conn.execute(
                "INSERT INTO media_embeddings (media_id, embedding) VALUES (?, ?)",
                (item["id"], b"mock_vector_1024"),
            )
            conn.commit()

        # Verify embedding exists
        with store._get_conn() as conn:
            row = conn.execute("SELECT media_id FROM media_embeddings WHERE media_id = ?", (item["id"],)).fetchone()
            assert row is not None

        # Delete entry without deleting underlying file (default)
        deleted, file_deleted = store.delete_media(item["id"], delete_underlying_file=False)
        assert deleted is True
        assert file_deleted is False

        # Verify item and embedding removed from DB
        assert store.get_media(item["id"]) is None
        with store._get_conn() as conn:
            row = conn.execute("SELECT media_id FROM media_embeddings WHERE media_id = ?", (item["id"],)).fetchone()
            assert row is None

        # Verify file is still present on disk
        assert f.exists()

        # Test deleting with delete_underlying_file=True
        f2 = tmppath / "chart2.png"
        f2.write_bytes(b"chart2data")
        item2 = store.register_media(file_path=str(f2), collection="generated", media_type="image")
        deleted2, file_deleted2 = store.delete_media(item2["id"], delete_underlying_file=True)
        assert deleted2 is True
        assert file_deleted2 is True
        assert not f2.exists()

    def test_search_media_by_query(self, temp_media_dir):
        tmppath, db_path = temp_media_dir
        from aether_engine.media.store import MediaStore

        store = MediaStore(db_path=db_path)

        f1 = tmppath / "dashboard_wireframe.png"
        f1.write_bytes(b"f1")
        store.register_media(
            file_path=str(f1),
            collection="user",
            media_type="image",
            tags=["wireframe", "v1"],
            user_notes="initial design idea",
        )

        f2 = tmppath / "architecture_flow.mp4"
        f2.write_bytes(b"f2")
        store.register_media(
            file_path=str(f2),
            collection="generated",
            media_type="video",
            tags=["architecture", "system"],
            user_notes="generated pipeline video",
        )

        # Search by tag
        results, count = store.list_media(query="wireframe")
        assert count == 1
        assert "dashboard_wireframe" in results[0]["file_path"]

        # Search by note
        results, count = store.list_media(query="pipeline")
        assert count == 1
        assert "architecture_flow" in results[0]["file_path"]


@pytest.mark.asyncio
class TestMediaWebSocketHandlers:
    """Verifies WebSocket request/response contracts for Media Library."""

    async def test_media_contracts_in_websocket(self, temp_media_dir, monkeypatch):
        tmppath, db_path = temp_media_dir
        from aether_engine.media.store import MediaStore, set_global_media_store

        store = MediaStore(db_path=db_path)
        set_global_media_store(store)

        f = tmppath / "test_snip.png"
        f.write_bytes(b"snip123")
        store.register_media(
            file_path=str(f),
            collection="user",
            media_type="image",
            session_id="sess-ws-1",
            message_id="msg-ws-1",
            probe_metadata={"origin": "snip"},
        )

        # Verify EventType values in contracts
        assert EventType.MEDIA_LIST_REQUEST == "MEDIA_LIST_REQUEST"
        assert EventType.MEDIA_LIST_RESPONSE == "MEDIA_LIST_RESPONSE"
        assert EventType.MEDIA_DELETE_REQUEST == "MEDIA_DELETE_REQUEST"
        assert EventType.MEDIA_DELETE_RESPONSE == "MEDIA_DELETE_RESPONSE"
        assert EventType.MEDIA_METADATA_UPDATE_REQUEST == "MEDIA_METADATA_UPDATE_REQUEST"
        assert EventType.MEDIA_METADATA_UPDATE_RESPONSE == "MEDIA_METADATA_UPDATE_RESPONSE"
        assert EventType.MEDIA_RECORDED == "MEDIA_RECORDED"

    async def test_auto_register_message_attachments(self, temp_media_dir, monkeypatch):
        _tmppath, db_path = temp_media_dir
        from aether_engine.media.store import MediaStore, set_global_media_store
        from aether_engine.routes.websocket import _register_message_attachments

        store = MediaStore(db_path=db_path)
        set_global_media_store(store)

        mock_ws = AsyncMock()

        # Test with base64 snip attachment
        b64_png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        attachments = [
            {
                "name": "snip_test.png",
                "mime_type": "image/png",
                "data": b64_png,
                "bounds": {"x": 10, "y": 10, "width": 100, "height": 100},
            }
        ]

        await _register_message_attachments(
            session_id="sess-snip-1",
            message_id="msg-snip-1",
            attachments=attachments,
            ws=mock_ws,
        )

        items, total = store.list_media(session_id="sess-snip-1")
        assert total == 1
        assert items[0]["collection"] == "user"
        assert items[0]["origin"] == "snip"
        assert items[0]["message_id"] == "msg-snip-1"
        assert Path(items[0]["file_path"]).exists()

        # Check MEDIA_RECORDED event was emitted
        assert mock_ws.send_text.called

    async def test_delete_by_session(self, temp_media_dir):
        tmppath, db_path = temp_media_dir
        from aether_engine.media.store import MediaStore

        store = MediaStore(db_path=db_path)
        f1 = tmppath / "s1.png"
        f1.write_bytes(b"s1")
        f2 = tmppath / "s2.png"
        f2.write_bytes(b"s2")

        store.register_media(file_path=str(f1), collection="user", media_type="image", session_id="sess-del-1")
        store.register_media(file_path=str(f2), collection="user", media_type="image", session_id="sess-del-2")

        _items, total = store.list_media(session_id="sess-del-1")
        assert total == 1

        deleted_count = store.delete_by_session("sess-del-1")
        assert deleted_count == 1

        _items1, total1 = store.list_media(session_id="sess-del-1")
        assert total1 == 0

        # Other session's media untouched
        _items2, total2 = store.list_media(session_id="sess-del-2")
        assert total2 == 1

