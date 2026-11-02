"""E4-F03: episode revisions and backfill have no revision/deletion
fence for dependent data (full-product review, section F).

Three ordered races, all driven at the real episodic seam with one
fixed two-dimensional synthetic vector space (the audit's recipe):

1. Cancelled replacement — ``store_episode`` commits the new FTS row,
   then awaits the embed. A cancellation during that await skipped
   BOTH the new-vector store and the stale-vector drop the normal
   failure branch performs, leaving the new text wearing the OLD
   vector: semantically recallable for the old concept, and counted
   as embedded by ``embedding_stats`` so backfill never rebuilt it.
2. Older backfill overwrites newer — ``backfill_episodic_embeddings``
   snapshots (task_id, summary), awaits the embed, then wrote the
   vector unconditionally; a revision stored in between (with its own
   newer vector) was overwritten by the stale backfill vector.
3. Delete during backfill — the same unconditional write recreated
   an embedding row for an episode that no longer exists (orphan
   vector; stats went to 0 episodes / 1 embedded / pending = -1).

The store path carries the same two write races as the backfill path
(delete-during-store-embed resurrects; an older in-flight store
overwrites a newer completed store), so both writers are fenced by
the same rule: a vector is written only if the episode still exists
with exactly the summary that was embedded, checked in the same SQL
statement as the write.

Controls (pass before and after): uninterrupted backfill embeds and
writes; an update that lands BEFORE the backfill read is embedded
under its new summary; delete with nothing in flight; a cancelled
backfill writes nothing; a completed re-store without a thinking log
keeps the prior thinking (the facade's resume path relies on it —
thinking retention is API semantics, not part of this fence).

All DBs are tmp files — nothing here touches the real ~/.aether/*.db.
"""
import asyncio
import sqlite3
import time
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from aether_engine.embedding_codec import decode_embedding
from aether_engine.memory import episodic
from aether_engine.memory.episodic import (
    backfill_episodic_embeddings,
    delete_episode,
    embedding_stats,
    store_episode,
)

OLD_SUMMARY = "old concept summary"
NEW_SUMMARY = "new concept summary"
OLD_VEC = [1.0, 0.0]
NEW_VEC = [0.0, 1.0]
TASK = "task-e4f03"


class GatedEmbedder:
    """Fake for ``episodic._embed_text`` with per-text gates.

    ``vectors`` maps text -> vector (or None for an embed failure).
    A text in ``gated`` blocks inside the call until the test sets
    the matching release event; ``entered[text]`` fires on entry so
    the test can interleave a store/delete/cancel at exactly the
    await boundary the defect lives on.
    """

    def __init__(self, vectors, gated=()):
        self.vectors = dict(vectors)
        self.entered = {t: asyncio.Event() for t in self.vectors}
        self.release = {t: asyncio.Event() for t in gated}

    async def __call__(self, text, task_id=None, context="store"):
        # S1-N3: an episode's embedded text is "summary\noutcome
        # head"; this fake's vectors/gates are keyed by the summary
        # line that heads it, so resolve the key before lookup.
        if text not in self.vectors and "\n" in text:
            text = text.split("\n", 1)[0]
        self.entered.setdefault(text, asyncio.Event()).set()
        gate = self.release.get(text)
        if gate is not None:
            await gate.wait()
        vec = self.vectors.get(text)
        return list(vec) if vec is not None else None


@pytest.fixture
def isolated_episodic(tmp_path):
    """Point both episodic DBs at tmp files (the redirect pattern
    from test_phase10_3_episodic_recall.py: ``_get_conn`` binds
    DB_PATH as a default arg at import time, so patch it explicitly)
    and capture audit log calls."""
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


def _insert_episode_row(task_id, summary, timestamp=None):
    """Insert an FTS row directly (unembedded, no embedder call)."""
    episodic.init_episodic_db()
    with episodic._get_conn() as conn:
        conn.execute(
            "INSERT INTO episodic_memory (task_id, timestamp, user_prompt, task_summary, tags, outcome)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, timestamp or int(time.time()), f"prompt {task_id}",
             summary, "test", "SUCCESS"),
        )
        conn.commit()


def _stored_vector(emb_db, task_id):
    conn = sqlite3.connect(emb_db)
    try:
        row = conn.execute(
            "SELECT embedding FROM episodic_embeddings WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    return decode_embedding(row[0]) if row else None


def _episode_summary(mem_db, task_id):
    conn = sqlite3.connect(mem_db)
    try:
        row = conn.execute(
            "SELECT task_summary FROM episodic_memory WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


async def _wait_entered(embedder, text):
    await asyncio.wait_for(embedder.entered[text].wait(), timeout=5)


# ---------------------------------------------------------------------------
# 1. Cancellation during store_episode's embed await
# ---------------------------------------------------------------------------

async def test_cancelled_replacement_drops_stale_vector(isolated_episodic):
    emb_db = isolated_episodic["emb_db"]
    # Seed: a completed store — old text, old thinking, old vector.
    seed = GatedEmbedder({OLD_SUMMARY: OLD_VEC})
    with patch("aether_engine.memory.episodic._embed_text", seed):
        await store_episode(TASK, "prompt", OLD_SUMMARY, ["t"], "SUCCESS",
                            thinking_log="OLD_THINKING")
    assert _stored_vector(emb_db, TASK) == OLD_VEC
    assert embedding_stats()["pending"] == 0

    # Re-store with new text (no thinking log — the resume shape),
    # then cancel while the embed is in flight.
    gated = GatedEmbedder({NEW_SUMMARY: NEW_VEC}, gated={NEW_SUMMARY})
    with patch("aether_engine.memory.episodic._embed_text", gated):
        task = asyncio.ensure_future(
            store_episode(TASK, "prompt 2", NEW_SUMMARY, ["t"], "SUCCESS"))
        await _wait_entered(gated, NEW_SUMMARY)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # The replacement text persisted (committed before the await —
    # pre-existing behaviour), but the OLD vector must not survive
    # attached to it: the episode is truthfully unembedded, so
    # backfill can rebuild the vector from the NEW summary.
    assert _episode_summary(isolated_episodic["mem_db"], TASK) == NEW_SUMMARY
    assert _stored_vector(emb_db, TASK) is None
    stats = embedding_stats()
    assert stats["episodes"] == 1
    assert stats["embedded"] == 0
    assert stats["pending"] == 1

    # And the rebuild actually happens under the new summary.
    rebuild = GatedEmbedder({NEW_SUMMARY: NEW_VEC})
    with patch("aether_engine.memory.episodic._embed_text", rebuild):
        result = await backfill_episodic_embeddings()
    assert result["backfilled"] == 1
    assert _stored_vector(emb_db, TASK) == NEW_VEC
    assert embedding_stats()["pending"] == 0


# ---------------------------------------------------------------------------
# 2. Backfill races: overwrite-newer and resurrect-after-delete
# ---------------------------------------------------------------------------

async def test_backfill_does_not_overwrite_newer_vector(isolated_episodic):
    emb_db = isolated_episodic["emb_db"]
    _insert_episode_row(TASK, OLD_SUMMARY)

    gated = GatedEmbedder(
        {OLD_SUMMARY: OLD_VEC, NEW_SUMMARY: NEW_VEC}, gated={OLD_SUMMARY})
    with patch("aether_engine.memory.episodic._embed_text", gated):
        backfill = asyncio.ensure_future(backfill_episodic_embeddings())
        await _wait_entered(gated, OLD_SUMMARY)
        # A newer revision lands while the backfill embed is in
        # flight, complete with its own vector.
        await store_episode(TASK, "prompt 2", NEW_SUMMARY, ["t"], "SUCCESS")
        assert _stored_vector(emb_db, TASK) == NEW_VEC
        gated.release[OLD_SUMMARY].set()
        result = await backfill

    # The stale backfill vector (a function of the OLD summary) must
    # not replace the newer revision's vector.
    assert _stored_vector(emb_db, TASK) == NEW_VEC
    assert _episode_summary(isolated_episodic["mem_db"], TASK) == NEW_SUMMARY
    assert result["backfilled"] == 0
    assert result.get("fenced") == 1
    stats = embedding_stats()
    assert stats["embedded"] == 1
    assert stats["pending"] == 0


async def test_backfill_does_not_resurrect_deleted_episode(isolated_episodic):
    emb_db = isolated_episodic["emb_db"]
    _insert_episode_row(TASK, OLD_SUMMARY)

    gated = GatedEmbedder({OLD_SUMMARY: OLD_VEC}, gated={OLD_SUMMARY})
    with patch("aether_engine.memory.episodic._embed_text", gated):
        backfill = asyncio.ensure_future(backfill_episodic_embeddings())
        await _wait_entered(gated, OLD_SUMMARY)
        assert delete_episode(TASK) is True
        assert embedding_stats()["episodes"] == 0
        gated.release[OLD_SUMMARY].set()
        result = await backfill

    # No orphan vector row, and stats stay internally consistent
    # (pre-fix: 0 episodes / 1 embedded / pending = -1).
    assert _stored_vector(emb_db, TASK) is None
    stats = embedding_stats()
    assert stats["episodes"] == 0
    assert stats["embedded"] == 0
    assert stats["pending"] == 0
    assert result["backfilled"] == 0
    assert result.get("fenced") == 1


# ---------------------------------------------------------------------------
# The store path's own write races (same fence)
# ---------------------------------------------------------------------------

async def test_store_does_not_resurrect_vector_after_delete_during_embed(
        isolated_episodic):
    emb_db = isolated_episodic["emb_db"]
    gated = GatedEmbedder({NEW_SUMMARY: NEW_VEC}, gated={NEW_SUMMARY})
    with patch("aether_engine.memory.episodic._embed_text", gated):
        task = asyncio.ensure_future(
            store_episode(TASK, "prompt", NEW_SUMMARY, ["t"], "SUCCESS"))
        await _wait_entered(gated, NEW_SUMMARY)
        # The FTS row is already committed at this point; delete the
        # episode while its embed is still in flight.
        assert delete_episode(TASK) is True
        gated.release[NEW_SUMMARY].set()
        await task

    assert _stored_vector(emb_db, TASK) is None
    stats = embedding_stats()
    assert stats["episodes"] == 0
    assert stats["embedded"] == 0
    assert stats["pending"] == 0


async def test_older_store_does_not_overwrite_newer_store(isolated_episodic):
    emb_db = isolated_episodic["emb_db"]
    gated = GatedEmbedder(
        {OLD_SUMMARY: OLD_VEC, NEW_SUMMARY: NEW_VEC}, gated={OLD_SUMMARY})
    with patch("aether_engine.memory.episodic._embed_text", gated):
        older = asyncio.ensure_future(
            store_episode(TASK, "prompt", OLD_SUMMARY, ["t"], "SUCCESS"))
        await _wait_entered(gated, OLD_SUMMARY)
        await store_episode(TASK, "prompt 2", NEW_SUMMARY, ["t"], "SUCCESS")
        assert _stored_vector(emb_db, TASK) == NEW_VEC
        gated.release[OLD_SUMMARY].set()
        await older

    assert _stored_vector(emb_db, TASK) == NEW_VEC
    assert _episode_summary(isolated_episodic["mem_db"], TASK) == NEW_SUMMARY


# ---------------------------------------------------------------------------
# Controls — pass before and after the fix
# ---------------------------------------------------------------------------

async def test_uninterrupted_backfill_embeds_and_writes(isolated_episodic):
    emb_db = isolated_episodic["emb_db"]
    _insert_episode_row(TASK, OLD_SUMMARY)
    embedder = GatedEmbedder({OLD_SUMMARY: OLD_VEC})
    with patch("aether_engine.memory.episodic._embed_text", embedder):
        result = await backfill_episodic_embeddings()
    assert result["backfilled"] == 1
    assert result.get("fenced", 0) == 0
    assert _stored_vector(emb_db, TASK) == OLD_VEC
    assert embedding_stats()["pending"] == 0


async def test_update_before_backfill_read_is_embedded(isolated_episodic):
    emb_db = isolated_episodic["emb_db"]
    _insert_episode_row(TASK, OLD_SUMMARY)
    # The revision lands BEFORE any backfill read, with its embed
    # failing — the episode stays unembedded under the NEW summary.
    failing = GatedEmbedder({NEW_SUMMARY: None})
    with patch("aether_engine.memory.episodic._embed_text", failing):
        await store_episode(TASK, "prompt 2", NEW_SUMMARY, ["t"], "SUCCESS")
    assert _episode_summary(isolated_episodic["mem_db"], TASK) == NEW_SUMMARY
    assert _stored_vector(emb_db, TASK) is None

    embedder = GatedEmbedder({OLD_SUMMARY: OLD_VEC, NEW_SUMMARY: NEW_VEC})
    with patch("aether_engine.memory.episodic._embed_text", embedder):
        result = await backfill_episodic_embeddings()
    assert result["backfilled"] == 1
    assert _stored_vector(emb_db, TASK) == NEW_VEC


async def test_delete_with_nothing_in_flight(isolated_episodic):
    emb_db = isolated_episodic["emb_db"]
    embedder = GatedEmbedder({OLD_SUMMARY: OLD_VEC})
    with patch("aether_engine.memory.episodic._embed_text", embedder):
        await store_episode(TASK, "prompt", OLD_SUMMARY, ["t"], "SUCCESS",
                            thinking_log="OLD_THINKING")
    assert _stored_vector(emb_db, TASK) == OLD_VEC
    assert delete_episode(TASK) is True
    assert _stored_vector(emb_db, TASK) is None
    stats = embedding_stats()
    assert stats["episodes"] == 0
    assert stats["embedded"] == 0
    assert stats["pending"] == 0


async def test_cancelled_backfill_writes_nothing(isolated_episodic):
    emb_db = isolated_episodic["emb_db"]
    _insert_episode_row(TASK, OLD_SUMMARY)
    gated = GatedEmbedder({OLD_SUMMARY: OLD_VEC}, gated={OLD_SUMMARY})
    with patch("aether_engine.memory.episodic._embed_text", gated):
        backfill = asyncio.ensure_future(backfill_episodic_embeddings())
        await _wait_entered(gated, OLD_SUMMARY)
        backfill.cancel()
        with pytest.raises(asyncio.CancelledError):
            await backfill
    assert _stored_vector(emb_db, TASK) is None
    assert embedding_stats()["pending"] == 1


async def test_fence_resolves_the_served_db_file(tmp_path):
    # Some suites (the prose-indexer suite's pattern) redirect only
    # ``_get_conn`` and leave the module constants pointing at the
    # real ~/.aether paths. The fence must guard the store those
    # connections actually serve — resolving the file off the
    # constant instead makes the guarded write fail with "no such
    # table" (the regression this pins).
    mem_db = tmp_path / "served-memory.db"
    emb_db = tmp_path / "served-embeddings.db"
    orig_get_conn = episodic._get_conn

    def fake_get_conn(db_path=None):
        if db_path is None or db_path == episodic.DB_PATH:
            return orig_get_conn(mem_db)
        if db_path == episodic.EMBEDDINGS_DB_PATH:
            return orig_get_conn(emb_db)
        return orig_get_conn(db_path)

    embedder = GatedEmbedder({OLD_SUMMARY: OLD_VEC})
    with patch("aether_engine.memory.episodic._get_conn",
               side_effect=fake_get_conn), \
            patch("aether_engine.memory.episodic._audit_logger.log_event"), \
            patch("aether_engine.memory.episodic._embed_text", embedder):
        await store_episode(TASK, "prompt", OLD_SUMMARY, ["t"], "SUCCESS")
    assert _stored_vector(emb_db, TASK) == OLD_VEC


async def test_completed_re_store_without_thinking_keeps_prior_thinking(
        isolated_episodic):
    # Thinking retention on a re-store that supplies no new log is
    # facade semantics (the resume path re-stores without one) — the
    # E4-F03 fence guards the vector, not this.
    embedder = GatedEmbedder({OLD_SUMMARY: OLD_VEC, NEW_SUMMARY: NEW_VEC})
    with patch("aether_engine.memory.episodic._embed_text", embedder):
        await store_episode(TASK, "prompt", OLD_SUMMARY, ["t"], "SUCCESS",
                            thinking_log="OLD_THINKING")
        await store_episode(TASK, "prompt 2", NEW_SUMMARY, ["t"], "SUCCESS")
    rows = episodic.get_all_episodes()
    assert len(rows) == 1
    assert rows[0]["task_summary"] == NEW_SUMMARY
    assert rows[0]["thinking_log"] == "OLD_THINKING"
    assert _stored_vector(isolated_episodic["emb_db"], TASK) == NEW_VEC
