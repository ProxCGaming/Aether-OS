"""S1-N3: an episode's semantic embedding covers more than the
200-character summary snippet.

store_episode (and the embedding backfill) embed
``_episode_embed_text(summary, outcome)`` — the summary plus the head
of the outcome — because both learn call sites truncate the summary
to 200 chars while the outcome holds the full response. Before the
fix, episodes whose substance sat beyond the snippet were
semantically unfindable (the BM25/FTS leg still found them).
"""
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from aether_engine.memory import episodic
from aether_engine.memory.episodic import _EMBED_OUTCOME_CHARS, _episode_embed_text


def test_embed_text_appends_outcome_head_to_summary():
    assert _episode_embed_text("short summary", "the full outcome") == (
        "short summary\nthe full outcome"
    )


def test_embed_text_truncates_outcome_at_the_cap():
    text = _episode_embed_text("s", "x" * (_EMBED_OUTCOME_CHARS + 500))
    assert text == "s\n" + "x" * _EMBED_OUTCOME_CHARS


def test_embed_text_without_outcome_is_the_summary():
    assert _episode_embed_text("only summary", "") == "only summary"
    assert _episode_embed_text("only summary", None) == "only summary"


def test_embed_text_outcome_equal_to_summary_is_not_duplicated():
    assert _episode_embed_text("same text", "same text") == "same text"


@pytest.fixture
def isolated_episodic(tmp_path):
    """The test_epic4_e4f03_backfill_fences isolation recipe: both
    episodic DBs on tmp files, audit calls captured."""
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
    yield
    stack.close()


async def test_store_episode_embeds_summary_plus_outcome(isolated_episodic):
    seen = {}

    async def capture(text, task_id=None, context="store"):
        seen["text"] = text
        return [0.1, 0.2]

    with patch("aether_engine.memory.episodic._embed_text", capture):
        await episodic.store_episode(
            "task-n3", "prompt", "tiny summary", ["t"],
            "the substantive outcome the summary never mentions",
        )
    assert seen["text"] == (
        "tiny summary\nthe substantive outcome the summary never mentions"
    )
