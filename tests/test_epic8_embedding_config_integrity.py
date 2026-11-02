"""Epic 8 MINORs N02 + N03 — embedding & config integrity.

N03: ``UserConfig.repo_map_token_budget`` existed as a field but was
absent from ``to_dict``/``from_dict``, so a saved budget silently
reset to the 1000 default on reload — and a deliberate 0 (map
disabled) came back as 1000.

N02: episodic embedding rows carried no model or dimension identity.
Switching the shared embedder (Settings/workspace config) reindexed
prose only; old-model episodic vectors stayed fully eligible for
recall against new-model queries, and ``_cosine_similarity`` zip()ed
vectors, silently truncating when dimensions differed.

These tests fail on the pre-fix code and pass after it. All DBs are
tmp files — nothing here touches the real ~/.aether/*.db.
"""
import pickle
import sqlite3
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from aether_engine.config import UserConfig, load_config, save_config
from aether_engine.memory import episodic
from aether_engine.memory.episodic import (
    backfill_episodic_embeddings,
    query_episodes,
    store_episode,
)

# ---------------------------------------------------------------------------
# N03 — repo_map_token_budget serialisation
# ---------------------------------------------------------------------------


def test_repo_map_token_budget_round_trips_custom_value():
    cfg = UserConfig(repo_map_token_budget=2500)
    assert cfg.to_dict()["repo_map_token_budget"] == 2500
    restored = UserConfig.from_dict(cfg.to_dict())
    assert restored.repo_map_token_budget == 2500


def test_repo_map_token_budget_zero_is_a_valid_disabled_value():
    # 0 means "repo map disabled" (config.py field comment); it must
    # survive the round trip as 0, not fall back to the 1000 default.
    cfg = UserConfig(repo_map_token_budget=0)
    restored = UserConfig.from_dict(cfg.to_dict())
    assert restored.repo_map_token_budget == 0
    # And a persisted 0 in a file loads as 0.
    assert UserConfig.from_dict({"repo_map_token_budget": 0}).repo_map_token_budget == 0


def test_repo_map_token_budget_defaults_when_absent():
    assert UserConfig.from_dict({}).repo_map_token_budget == 1000
    assert UserConfig().to_dict()["repo_map_token_budget"] == 1000


def test_repo_map_token_budget_survives_save_and_load(tmp_path):
    path = tmp_path / "config.json"
    save_config(UserConfig(repo_map_token_budget=0), path)
    assert load_config(path).repo_map_token_budget == 0
    save_config(UserConfig(repo_map_token_budget=4321), path)
    assert load_config(path).repo_map_token_budget == 4321


# ---------------------------------------------------------------------------
# N02 — episodic embedding model identity
# ---------------------------------------------------------------------------

# The query shares no FTS tokens with the stored prompt/summary/tags/
# outcome, so only the semantic leg can return the episode.
QUERY_TEXT = "qqq vvv"
SUMMARY = "zebra handling field guide"
PROMPT = "zebra handling field guide"

MODEL_A = "model-a"  # dimension 3
MODEL_B = "model-b"  # dimension 2 — a switch changes dimension too

_VECTORS = {
    (MODEL_A, SUMMARY): [1.0, 0.0, 0.0],
    (MODEL_A, QUERY_TEXT): [1.0, 0.0, 0.0],
    (MODEL_B, SUMMARY): [1.0, 0.0],
    (MODEL_B, QUERY_TEXT): [1.0, 0.0],
}


class _SwitchableEmbedder:
    """Fake for episodic._embed_text + resolve_embedder: the "active
    model" is whatever the test last switched to, and each model maps
    the same texts to its own dimension's vectors."""

    def __init__(self) -> None:
        self.model = MODEL_A

    async def embed(self, text, task_id=None, context="store"):
        vec = _VECTORS.get((self.model, text))
        return list(vec) if vec is not None else None

    def resolve(self, config=None):
        return self.model


@pytest.fixture
def isolated_episodic(tmp_path):
    """Point both episodic DBs at tmp files (the redirect pattern from
    test_phase10_3_episodic_recall.py: ``_get_conn`` binds DB_PATH as a
    default arg, so patch it explicitly), swap in the switchable
    embedder, and capture audit log calls."""
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

    fake = _SwitchableEmbedder()
    stack = ExitStack()
    stack.enter_context(patch("aether_engine.memory.episodic.DB_PATH", mem_db))
    stack.enter_context(patch("aether_engine.memory.episodic.EMBEDDINGS_DB_PATH", emb_db))
    stack.enter_context(patch("aether_engine.memory.episodic._get_conn", side_effect=fake_get_conn))
    stack.enter_context(patch(
        "aether_engine.memory.episodic._embed_text",
        new=AsyncMock(side_effect=fake.embed),
    ))
    stack.enter_context(patch(
        "aether_engine.memory.episodic.resolve_embedder", new=fake.resolve))
    mock_log = stack.enter_context(
        patch("aether_engine.memory.episodic._audit_logger.log_event")
    )
    try:
        yield {"mem_db": mem_db, "emb_db": emb_db, "log": mock_log, "fake": fake}
    finally:
        stack.close()


def _embedding_row(emb_db, task_id):
    conn = sqlite3.connect(emb_db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM episodic_embeddings WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()


def _audit_calls(mock_log, action):
    return [c for c in mock_log.call_args_list if c.args and c.args[0] == action]


async def test_stored_embeddings_carry_model_identity(isolated_episodic):
    await store_episode("t-a", PROMPT, SUMMARY, ["zebra"], "SUCCESS")
    row = _embedding_row(isolated_episodic["emb_db"], "t-a")
    assert row is not None
    assert row["model"] == MODEL_A
    assert row["dimension"] == 3


async def test_switching_embedder_excludes_stale_vectors_until_rebuilt(isolated_episodic):
    fake = isolated_episodic["fake"]
    await store_episode("t-a", PROMPT, SUMMARY, ["zebra"], "SUCCESS")

    # Same-model recall works (guard against over-blocking).
    results = await query_episodes(QUERY_TEXT, backfill_limit=0)
    assert [r["task_id"] for r in results] == ["t-a"]

    # Switch the shared embedder. Pre-fix, the dim-3 model-A vector was
    # scored against the dim-2 model-B query with zip() truncation:
    # cosine 1.0, returned. Post-fix it is not eligible at all.
    fake.model = MODEL_B
    results = await query_episodes(QUERY_TEXT, backfill_limit=0)
    assert results == []

    # The existing backfill machinery rebuilds it under the new model,
    # after which recall works again — now legitimately.
    result = await backfill_episodic_embeddings()
    assert result["backfilled"] == 1
    row = _embedding_row(isolated_episodic["emb_db"], "t-a")
    assert row["model"] == MODEL_B
    assert row["dimension"] == 2
    results = await query_episodes(QUERY_TEXT, backfill_limit=0)
    assert [r["task_id"] for r in results] == ["t-a"]


async def test_reset_for_embedder_switch_invalidates_incompatible_rows(isolated_episodic):
    fake = isolated_episodic["fake"]
    await store_episode("t-1", PROMPT, SUMMARY, ["zebra"], "SUCCESS")
    await store_episode("t-2", PROMPT, SUMMARY, ["zebra"], "SUCCESS")

    # Resetting for the model that produced the rows keeps them.
    result = episodic.reset_episodic_embeddings_for_embedder(MODEL_A)
    assert result == {"embedder": MODEL_A, "removed": 0}
    assert _embedding_row(isolated_episodic["emb_db"], "t-1") is not None

    # A switch invalidates every incompatible row immediately, so no
    # stale vector is recallable in the interim before backfill.
    fake.model = MODEL_B
    result = episodic.reset_episodic_embeddings_for_embedder(MODEL_B)
    assert result == {"embedder": MODEL_B, "removed": 2}
    assert _embedding_row(isolated_episodic["emb_db"], "t-1") is None
    assert _embedding_row(isolated_episodic["emb_db"], "t-2") is None
    logged = _audit_calls(
        isolated_episodic["log"], "EPISODIC_EMBEDDINGS_INVALIDATED")
    assert len(logged) == 1
    assert logged[0].args[1]["removed"] == 2


async def test_same_model_dimension_mismatch_is_excluded_not_truncated(isolated_episodic):
    # A stored row claiming the active model but carrying a vector of
    # the wrong dimension is a data error: it must be excluded, never
    # zip-truncated into a similarity score.
    episodic.init_episodic_db()
    episodic.init_embeddings_db()
    with episodic._get_conn() as conn:
        conn.execute(
            "INSERT INTO episodic_memory (task_id, timestamp, user_prompt, task_summary, tags, outcome)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("t-dim", 1, PROMPT, SUMMARY, "zebra", "SUCCESS"),
        )
        conn.commit()
    conn = sqlite3.connect(isolated_episodic["emb_db"])
    try:
        conn.execute(
            "INSERT INTO episodic_embeddings (task_id, embedding, model, dimension)"
            " VALUES (?, ?, ?, ?)",
            ("t-dim", pickle.dumps([1.0, 0.0]), MODEL_A, 2),
        )
        conn.commit()
    finally:
        conn.close()

    results = await query_episodes(QUERY_TEXT, backfill_limit=0)
    assert results == []


def test_cosine_similarity_never_truncates_mismatched_dimensions():
    # Pre-fix this returned 1.0 (zip stopped at the shorter vector).
    assert episodic._cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0]) == 0.0
    assert episodic._cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0
    # Same-dimension behaviour is unchanged.
    assert episodic._cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0


async def test_legacy_rows_without_identity_are_unknown_model(isolated_episodic):
    # Build the embeddings DB with the PRE-fix schema (no identity
    # columns) and a row in it, as an existing deployment would have.
    emb_db = isolated_episodic["emb_db"]
    conn = sqlite3.connect(emb_db)
    try:
        conn.execute(
            "CREATE TABLE episodic_embeddings ("
            " task_id TEXT PRIMARY KEY, embedding BLOB)"
        )
        conn.execute(
            "INSERT INTO episodic_embeddings (task_id, embedding) VALUES (?, ?)",
            ("t-legacy", pickle.dumps([1.0, 0.0, 0.0])),
        )
        conn.commit()
    finally:
        conn.close()
    episodic.init_episodic_db()
    with episodic._get_conn() as conn:
        conn.execute(
            "INSERT INTO episodic_memory (task_id, timestamp, user_prompt, task_summary, tags, outcome)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("t-legacy", 1, PROMPT, SUMMARY, "zebra", "SUCCESS"),
        )
        conn.commit()

    # The migration adds the identity columns; the legacy row's model
    # stays NULL (unknown) and it must NOT be treated as a model-A
    # vector — pre-fix it scored cosine 1.0 and was returned.
    episodic.init_embeddings_db()
    row = _embedding_row(emb_db, "t-legacy")
    assert row["model"] is None
    results = await query_episodes(QUERY_TEXT, backfill_limit=0)
    assert results == []

    # Backfill rebuilds it under the active model; then it recalls.
    result = await backfill_episodic_embeddings()
    assert result["backfilled"] == 1
    row = _embedding_row(emb_db, "t-legacy")
    assert row["model"] == MODEL_A
    assert row["dimension"] == 3
    results = await query_episodes(QUERY_TEXT, backfill_limit=0)
    assert [r["task_id"] for r in results] == ["t-legacy"]


async def test_embedding_stats_counts_only_current_model_rows(isolated_episodic):
    fake = isolated_episodic["fake"]
    await store_episode("t-a", PROMPT, SUMMARY, ["zebra"], "SUCCESS")
    stats = episodic.embedding_stats()
    assert stats["episodes"] == 1
    assert stats["embedded"] == 1
    assert stats["pending"] == 0

    # After a switch, the stale-model row is pending rebuild, not
    # "embedded" — Diagnostics must not claim recall is healthy.
    fake.model = MODEL_B
    stats = episodic.embedding_stats()
    assert stats["embedded"] == 0
    assert stats["pending"] == 1
