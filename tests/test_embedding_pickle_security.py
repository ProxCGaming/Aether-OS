"""S1-M7 + S4-M13 — embeddings must never be pickle-deserialised.

Episodic memory, the workspace prose (RAG) store, and the media store
all persisted float-vector embeddings into SQLite with ``pickle.dumps``
and read them back with ``pickle.loads``. Anyone who can supply a DB
file (import / backup / restore surfaces) could therefore get arbitrary
code execution the next time a semantic query ran.

Post-fix format: a version-tagged JSON encoding produced/consumed by
``aether_engine.embedding_codec``. Rows that lack the tag — every legacy
pickle row included — are treated as "no embedding" (embeddings are
recomputable) and are NEVER unpickled.

These tests plant a pickle whose ``__reduce__`` writes a sentinel file
if it is ever executed, per store, and assert the sentinel never
appears and the row reads as missing/invalid. Round-trip tests assert
embeddings stored through each store's public write path load back
exactly (float64 fidelity) in the new format.
"""
import pickle
import sqlite3
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aether_engine.media.store import MediaStore
from aether_engine.memory import episodic
from aether_engine.rag.store import WorkspaceRagStore

# ---------------------------------------------------------------------------
# Malicious pickle payload: executing it writes the sentinel file.
# ---------------------------------------------------------------------------

def _write_sentinel(path: str) -> int:
    Path(path).write_text("pwned", encoding="utf-8")
    return 1


class _Evil:
    def __init__(self, sentinel: Path):
        self.sentinel = str(sentinel)

    def __reduce__(self):
        return (_write_sentinel, (self.sentinel,))


def _evil_pickle(sentinel: Path) -> bytes:
    return pickle.dumps(_Evil(sentinel))


# ---------------------------------------------------------------------------
# Codec-level guards
# ---------------------------------------------------------------------------

def test_stores_no_longer_reference_pickle():
    import aether_engine.media.store as media_store
    import aether_engine.rag.store as rag_store

    for module in (episodic, rag_store, media_store):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("import pickle", "pickle.loads", "pickle.dumps",
                          "pickle.load(", "pickle.Unpickler"):
            assert forbidden not in source, (module.__name__, forbidden)


# ---------------------------------------------------------------------------
# Episodic store
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_episodic(tmp_path):
    """Redirect both episodic DBs at tmp files (pattern copied from
    test_epic8_embedding_config_integrity.py)."""
    mem_db = tmp_path / "memory.db"
    emb_db = tmp_path / "embeddings.db"
    real_db = episodic.DB_PATH
    real_emb_db = episodic.EMBEDDINGS_DB_PATH
    orig_get_conn = episodic._get_conn

    def fake_get_conn(db_path=None):
        if db_path is None or db_path in (real_db, mem_db):
            return orig_get_conn(mem_db)
        if db_path in (real_emb_db, emb_db):
            return orig_get_conn(emb_db)
        return orig_get_conn(db_path)

    stack = ExitStack()
    stack.enter_context(patch("aether_engine.memory.episodic.DB_PATH", mem_db))
    stack.enter_context(patch("aether_engine.memory.episodic.EMBEDDINGS_DB_PATH", emb_db))
    stack.enter_context(patch("aether_engine.memory.episodic._get_conn", side_effect=fake_get_conn))
    stack.enter_context(patch("aether_engine.memory.episodic._audit_logger.log_event"))
    try:
        yield emb_db
    finally:
        stack.close()


def _insert_episode_row(task_id: str, summary: str) -> None:
    episodic.init_episodic_db()
    with episodic._get_conn() as conn:
        conn.execute(
            "INSERT INTO episodic_memory (task_id, timestamp, user_prompt, task_summary, tags, outcome)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, 1, "unrelated prompt", summary, "test", "SUCCESS"),
        )
        conn.commit()


def _raw_embedding_blob(emb_db: Path, task_id: str) -> bytes:
    conn = sqlite3.connect(emb_db)
    try:
        return conn.execute(
            "SELECT embedding FROM episodic_embeddings WHERE task_id = ?",
            (task_id,)).fetchone()[0]
    finally:
        conn.close()


async def _query_with_vec(isolated, vec):
    with patch(
        "aether_engine.memory.episodic._embed_text",
        new=AsyncMock(return_value=list(vec)),
    ):
        return await episodic.query_episodes(
            "qqq vvv", backfill_limit=0)


async def test_episodic_embedding_round_trips_in_new_format(isolated_episodic):
    vec = [0.1, 0.2, 0.3]
    episodic.init_embeddings_db()
    episodic._store_embedding_row("t-rt", vec)

    blob = _raw_embedding_blob(isolated_episodic, "t-rt")
    assert not blob.startswith(b"\x80")  # not a pickle stream

    # Behavioural round-trip: the stored vector is recallable.
    _insert_episode_row("t-rt", "zebra handling field guide")
    results = await _query_with_vec(isolated_episodic, vec)
    assert [r["task_id"] for r in results] == ["t-rt"]


async def test_episodic_legacy_pickle_row_is_never_unpickled(
        isolated_episodic, tmp_path):
    sentinel = tmp_path / "episodic-pwned.txt"
    episodic.init_embeddings_db()
    _insert_episode_row("t-evil", "zebra handling field guide")
    conn = sqlite3.connect(isolated_episodic)
    try:
        conn.execute(
            "INSERT INTO episodic_embeddings (task_id, embedding, model, dimension)"
            " VALUES (?, ?, ?, ?)",
            ("t-evil", _evil_pickle(sentinel),
             episodic.resolve_embedder(), 3),
        )
        conn.commit()
    finally:
        conn.close()

    results = await _query_with_vec(isolated_episodic, [1.0, 0.0, 0.0])
    assert results == []  # treated as missing, not scored
    assert not sentinel.exists()


# ---------------------------------------------------------------------------
# RAG / prose store
# ---------------------------------------------------------------------------

def _rag_store_with_chunk(tmp_path) -> tuple[WorkspaceRagStore, str]:
    store = WorkspaceRagStore(db_path=tmp_path / "rag.db")
    doc_id = store.upsert_document(
        "/ws", "notes.md", mtime=1, file_size=10, sha256="x" * 64,
        status="indexed")
    return store, doc_id


def test_rag_embedding_round_trips_in_new_format(tmp_path):
    store, doc_id = _rag_store_with_chunk(tmp_path)
    vec = [0.1, 0.2, 0.3]
    store.replace_chunks(doc_id, [{
        "chunk_index": 0, "line_start": 1, "line_end": 2,
        "content": "hello world", "embedding": vec,
    }])
    chunks = store.chunks_for_document(doc_id)
    assert chunks[0]["embedding"] == vec

    # The set_chunk_embedding write path round-trips too.
    vec2 = [1.0, 0.0, -0.5]
    store.set_chunk_embedding(chunks[0]["id"], vec2)
    assert store.chunks_for_document(doc_id)[0]["embedding"] == vec2
    assert store.search_candidates(["/ws"])[0]["embedding"] == vec2

    conn = sqlite3.connect(store.db_path)
    try:
        blob = conn.execute("SELECT embedding FROM prose_chunks").fetchone()[0]
    finally:
        conn.close()
    assert not blob.startswith(b"\x80")


def test_rag_legacy_pickle_row_is_never_unpickled(tmp_path):
    sentinel = tmp_path / "rag-pwned.txt"
    store, doc_id = _rag_store_with_chunk(tmp_path)
    store.replace_chunks(doc_id, [{
        "chunk_index": 0, "line_start": 1, "line_end": 2,
        "content": "hello world", "embedding": [1.0, 0.0],
    }])
    chunk_id = store.chunks_for_document(doc_id)[0]["id"]
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute(
            "UPDATE prose_chunks SET embedding = ? WHERE id = ?",
            (_evil_pickle(sentinel), chunk_id))
        conn.commit()
    finally:
        conn.close()

    # Reads as "no embedding" (the backfill queue's None marker).
    assert store.chunks_for_document(doc_id)[0]["embedding"] is None
    assert store.search_candidates(["/ws"])[0]["embedding"] is None
    assert not sentinel.exists()


# ---------------------------------------------------------------------------
# Media store
# ---------------------------------------------------------------------------

def _media_store_with_item(tmp_path) -> tuple[MediaStore, str]:
    store = MediaStore(db_path=tmp_path / "media.db")
    f = tmp_path / "photo.png"
    f.write_bytes(b"imagedata")
    item = store.register_media(
        file_path=str(f), collection="user", media_type="image")
    return store, item["id"]


def test_media_embedding_round_trips_in_new_format(tmp_path):
    store, media_id = _media_store_with_item(tmp_path)
    vec = [0.1, 0.2, 0.3]
    store.store_embedding(media_id, vec)
    assert store.get_embedding(media_id) == vec
    assert store._embeddings_for([media_id]) == {media_id: vec}

    conn = sqlite3.connect(store.db_path)
    try:
        blob = conn.execute(
            "SELECT embedding FROM media_embeddings WHERE media_id = ?",
            (media_id,)).fetchone()[0]
    finally:
        conn.close()
    assert not blob.startswith(b"\x80")


def test_media_legacy_pickle_row_is_never_unpickled(tmp_path):
    sentinel = tmp_path / "media-pwned.txt"
    store, media_id = _media_store_with_item(tmp_path)
    store.init_db()
    with store._get_conn() as conn:
        conn.execute(
            "INSERT INTO media_embeddings (media_id, embedding) VALUES (?, ?)",
            (media_id, _evil_pickle(sentinel)))
        conn.commit()

    assert store.get_embedding(media_id) is None
    assert store._embeddings_for([media_id]) == {}
    assert not sentinel.exists()
