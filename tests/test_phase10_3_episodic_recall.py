"""Phase 10.3 A2: episodic recall — thresholded semantic search, dedupe,
embedding backfill.

Before the fix, all three sub-findings reproduced in current code:

1. ``query_episodes`` ran a full scan with no similarity threshold: every
   row in ``episodic_embeddings`` got an RRF score, so an irrelevant
   episode (cosine ~0.0 with the query) was returned purely by rank.
2. ``episodic_memory`` (FTS5) had no primary key / unique constraint on
   ``task_id`` and ``store_episode`` used a plain ``INSERT``, so storing
   the same ``task_id`` twice left two FTS rows.
3. Episodes stored unembedded in Phase 10.1 (absence from
   ``episodic_embeddings``, ``embedded=False`` in the audit log) were
   never re-embedded: ``_embed_text`` says so in its own docstring
   ("No retry/backfill here (Phase 10.3)") and no backfill existed.

These tests fail on the pre-fix code (wrong results / extra rows /
ImportError for the new ``dedupe_episodic_rows`` /
``backfill_episodic_embeddings``) and pass after it. All DBs are tmp
files — nothing here touches the real ~/.aether/*.db.
"""
import sqlite3
import time
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from aether_engine.embedding_codec import encode_embedding
from aether_engine.memory import episodic
from aether_engine.memory.episodic import query_episodes, store_episode

# Deterministic unit vectors for the mocked embedder. The query embeds
# along dim 0; the relevant episode is 0.9-similar, the irrelevant one is
# orthogonal (cosine 0.0) — far below any sane threshold.
QUERY_TEXT = "alpha beta"
QUERY_VEC = [1.0, 0.0, 0.0]
RELEVANT_SUMMARY = "machine learning guide"
RELEVANT_VEC = [0.9, 0.4358898943540673, 0.0]  # cosine 0.9 with QUERY_VEC
IRRELEVANT_SUMMARY = "banana bread recipe"
IRRELEVANT_VEC = [0.0, 1.0, 0.0]  # cosine 0.0 with QUERY_VEC

_VECTORS = {
    QUERY_TEXT: QUERY_VEC,
    RELEVANT_SUMMARY: RELEVANT_VEC,
    IRRELEVANT_SUMMARY: IRRELEVANT_VEC,
}


async def _fake_embed(text, task_id=None, context="store"):
    return list(_VECTORS[text])


@pytest.fixture
def isolated_episodic(tmp_path):
    """Point both episodic DBs at tmp files and capture audit log calls.

    ``_get_conn`` binds ``DB_PATH`` as a default arg at import time, so
    patching the module constants alone does not redirect it — redirect
    explicitly, as in test_phase10_1_embedding_failure.py.
    """
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
    mock_log = stack.enter_context(
        patch("aether_engine.memory.episodic._audit_logger.log_event")
    )
    try:
        yield {"mem_db": mem_db, "emb_db": emb_db, "log": mock_log}
    finally:
        stack.close()


def _audit_calls(mock_log, action):
    return [c for c in mock_log.call_args_list if c.args and c.args[0] == action]


def _insert_episode_row(task_id, summary, timestamp=None):
    """Insert an FTS row directly (no network embedding)."""
    episodic.init_episodic_db()
    with episodic._get_conn() as conn:
        conn.execute(
            "INSERT INTO episodic_memory (task_id, timestamp, user_prompt, task_summary, tags, outcome)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, timestamp or int(time.time()), f"prompt {task_id}",
             summary, "test", "SUCCESS"),
        )
        conn.commit()


def _insert_embedding(emb_db, task_id, vector):
    episodic.init_embeddings_db()
    conn = sqlite3.connect(emb_db)
    try:
        # N02: stored rows carry the producing embedder's identity,
        # and recall only scores rows stamped with the active one —
        # stamp the ambient resolved embedder, as the real store path
        # does. (Pre-N02 this helper wrote identity-less rows, which
        # are now correctly treated as unknown-model and ineligible.)
        conn.execute(
            "INSERT OR REPLACE INTO episodic_embeddings (task_id, embedding, model, dimension)"
            " VALUES (?, ?, ?, ?)",
            (task_id, encode_embedding(vector), episodic.resolve_embedder(), len(vector)),
        )
        conn.commit()
    finally:
        conn.close()


def _embedding_row(emb_db, task_id):
    conn = sqlite3.connect(emb_db)
    try:
        return conn.execute(
            "SELECT task_id FROM episodic_embeddings WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def _episode_row_count(task_id):
    with episodic._get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM episodic_memory WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]


def _episode_summary(task_id):
    with episodic._get_conn() as conn:
        row = conn.execute(
            "SELECT task_summary FROM episodic_memory WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return row[0] if row else None


def _insert_relevant_and_irrelevant(isolated_episodic):
    """Two embedded episodes; the query shares no BM25 tokens with either
    summary, so only the semantic leg (and the threshold) decides."""
    _insert_episode_row("task-relevant", RELEVANT_SUMMARY)
    _insert_episode_row("task-irrelevant", IRRELEVANT_SUMMARY)
    _insert_embedding(isolated_episodic["emb_db"], "task-relevant", RELEVANT_VEC)
    _insert_embedding(isolated_episodic["emb_db"], "task-irrelevant", IRRELEVANT_VEC)


# --- (1) similarity threshold -------------------------------------------------

async def test_irrelevant_episode_below_threshold_not_returned(isolated_episodic):
    _insert_relevant_and_irrelevant(isolated_episodic)
    with patch(
        "aether_engine.memory.episodic._embed_text",
        new=AsyncMock(side_effect=_fake_embed),
    ):
        results = await query_episodes(QUERY_TEXT)
    # Pre-fix: both episodes came back — the irrelevant one (cosine 0.0)
    # got an RRF score purely by rank. Post-fix: only the 0.9-similar one.
    assert [r["task_id"] for r in results] == ["task-relevant"]


async def test_explicit_threshold_zero_restores_unthresholded_behaviour(isolated_episodic):
    _insert_relevant_and_irrelevant(isolated_episodic)
    with patch(
        "aether_engine.memory.episodic._embed_text",
        new=AsyncMock(side_effect=_fake_embed),
    ):
        results = await query_episodes(QUERY_TEXT, similarity_threshold=0.0)
    # A threshold of 0.0 opts back into the old behaviour: every embedded
    # episode enters RRF. (Pre-fix this call raised TypeError: no such kwarg.)
    assert {r["task_id"] for r in results} == {"task-relevant", "task-irrelevant"}


async def test_explicit_high_threshold_excludes_even_relevant_episode(isolated_episodic):
    _insert_relevant_and_irrelevant(isolated_episodic)
    with patch(
        "aether_engine.memory.episodic._embed_text",
        new=AsyncMock(side_effect=_fake_embed),
    ):
        results = await query_episodes(QUERY_TEXT, similarity_threshold=0.95)
    # 0.9 < 0.95, and neither summary shares a BM25 token with the query.
    assert results == []


async def test_default_threshold_comes_from_config(isolated_episodic):
    # No explicit threshold: the value is read from UserConfig
    # (episodic_similarity_threshold, default 0.3) — not a magic number in
    # the memory module.
    from aether_engine.config import UserConfig

    assert UserConfig().episodic_similarity_threshold == 0.3
    _insert_relevant_and_irrelevant(isolated_episodic)
    with patch(
        "aether_engine.memory.episodic._embed_text",
        new=AsyncMock(side_effect=_fake_embed),
    ):
        results = await query_episodes(
            QUERY_TEXT, config=UserConfig(episodic_similarity_threshold=0.95)
        )
    assert results == []


# --- (2) dedupe on task_id ----------------------------------------------------

async def test_store_same_task_id_twice_stores_one_row(isolated_episodic):
    with patch(
        "aether_engine.memory.episodic._embed_text",
        new=AsyncMock(side_effect=_fake_embed),
    ):
        await store_episode(
            "task-dup", "first prompt", RELEVANT_SUMMARY, ["test"], "SUCCESS"
        )
        await store_episode(
            "task-dup", "second prompt", IRRELEVANT_SUMMARY, ["test"], "SUCCESS"
        )
    # Pre-fix: two FTS rows for one task_id. Post-fix: one row, newest wins.
    assert _episode_row_count("task-dup") == 1
    assert _episode_summary("task-dup") == IRRELEVANT_SUMMARY
    assert _embedding_row(isolated_episodic["emb_db"], "task-dup") is not None


def test_dedupe_migration_collapses_existing_duplicates_and_logs(isolated_episodic):
    # Pre-existing duplicates (inserted directly, as they would be by the
    # old store_episode): three rows, same task_id, different timestamps.
    from aether_engine.memory.episodic import dedupe_episodic_rows

    now = int(time.time())
    _insert_episode_row("task-old-dup", "oldest summary", timestamp=now - 300)
    _insert_episode_row("task-old-dup", "middle summary", timestamp=now - 200)
    _insert_episode_row("task-old-dup", "newest summary", timestamp=now - 100)
    assert _episode_row_count("task-old-dup") == 3

    result = dedupe_episodic_rows()

    assert result["rows_removed"] == 2
    assert result["task_ids_deduped"] == 1
    assert _episode_row_count("task-old-dup") == 1
    # The newest row survives — no data silently dropped without a record.
    assert _episode_summary("task-old-dup") == "newest summary"
    logged = _audit_calls(isolated_episodic["log"], "EPISODIC_MEMORY_DEDUPED")
    assert len(logged) == 1
    assert logged[0].args[1]["rows_removed"] == 2
    assert logged[0].args[1]["task_ids"] == ["task-old-dup"]


def test_dedupe_migration_is_noop_without_duplicates(isolated_episodic):
    from aether_engine.memory.episodic import dedupe_episodic_rows

    _insert_episode_row("task-single", RELEVANT_SUMMARY)

    result = dedupe_episodic_rows()

    assert result == {"task_ids_deduped": 0, "rows_removed": 0}
    assert _audit_calls(isolated_episodic["log"], "EPISODIC_MEMORY_DEDUPED") == []


# --- (3) backfill --------------------------------------------------------------

async def test_backfill_embeds_unembedded_episode(isolated_episodic):
    # The Phase 10.1 marker for "stored unembedded": the episode is in
    # episodic_memory but absent from episodic_embeddings.
    from aether_engine.memory.episodic import backfill_episodic_embeddings

    _insert_episode_row("task-unembedded", RELEVANT_SUMMARY)
    assert _embedding_row(isolated_episodic["emb_db"], "task-unembedded") is None

    with patch(
        "aether_engine.memory.episodic._embed_text",
        new=AsyncMock(side_effect=_fake_embed),
    ):
        result = await backfill_episodic_embeddings()

    assert result["backfilled"] == 1
    assert result["failed"] == 0
    assert _embedding_row(isolated_episodic["emb_db"], "task-unembedded") is not None
    logged = _audit_calls(isolated_episodic["log"], "EPISODIC_EMBEDDINGS_BACKFILLED")
    assert len(logged) == 1
    assert logged[0].args[1]["backfilled"] == 1


async def test_backfill_failure_leaves_episode_unembedded(isolated_episodic):
    from aether_engine.memory.episodic import backfill_episodic_embeddings

    _insert_episode_row("task-still-down", RELEVANT_SUMMARY)

    with patch(
        "aether_engine.memory.episodic._embed_text",
        new=AsyncMock(return_value=None),
    ):
        result = await backfill_episodic_embeddings()

    # A failed backfill must not raise and must not fake a row: the episode
    # stays unembedded and is retried on the next backfill.
    assert result["backfilled"] == 0
    assert result["failed"] == 1
    assert result["failed_task_ids"] == ["task-still-down"]
    assert _embedding_row(isolated_episodic["emb_db"], "task-still-down") is None


async def test_query_backfills_unembedded_episode(isolated_episodic):
    # Trigger: query_episodes backfills (bounded, oldest-first) before the
    # semantic scan, so an episode stored while Ollama was down becomes
    # retrievable on the next query instead of staying invisible forever.
    _insert_episode_row("task-unembedded", RELEVANT_SUMMARY)
    assert _embedding_row(isolated_episodic["emb_db"], "task-unembedded") is None

    with patch(
        "aether_engine.memory.episodic._embed_text",
        new=AsyncMock(side_effect=_fake_embed),
    ):
        results = await query_episodes(QUERY_TEXT)

    assert [r["task_id"] for r in results] == ["task-unembedded"]
    assert _embedding_row(isolated_episodic["emb_db"], "task-unembedded") is not None
