"""Phase 10.1 B2: embedding failure must be loud and explicit.

Before the fix, ``_embed_text`` swallowed every exception (1.0s timeout,
hardcoded ``ollama/nomic-embed-text``) and returned ``None`` with no log,
and ``store_episode`` left any stale embedding row in place — so a failed
embed was indistinguishable from "never attempted".

These tests fail on the pre-fix code (no ``EPISODIC_EMBEDDING_FAILED``
audit event, 1.0s timeout) and pass after it.
"""
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aether_engine.memory import episodic
from aether_engine.memory.episodic import _embed_text, store_episode


@pytest.fixture
def isolated_dbs(tmp_path):
    """Point both episodic DBs at tmp files and capture audit log calls.

    Note: ``_get_conn`` binds ``DB_PATH`` as a default arg at import time,
    so patching the module constant alone does not redirect ``_get_conn()``
    calls — redirect them explicitly here.
    """
    test_db = tmp_path / "test_episodic.db"
    test_emb_db = tmp_path / "test_embeddings.db"
    real_db = episodic.DB_PATH
    real_emb_db = episodic.EMBEDDINGS_DB_PATH
    orig_get_conn = episodic._get_conn

    def fake_get_conn(db_path=None):
        if db_path is None or db_path in (real_db, test_db):
            return orig_get_conn(test_db)
        if db_path in (real_emb_db, test_emb_db):
            return orig_get_conn(test_emb_db)
        return orig_get_conn(db_path)

    with (
        patch("aether_engine.memory.episodic.DB_PATH", test_db),
        patch("aether_engine.memory.episodic.EMBEDDINGS_DB_PATH", test_emb_db),
        patch("aether_engine.memory.episodic._get_conn", side_effect=fake_get_conn),
        # Epic 9: the embedding circuit breaker is process state; each
        # simulated-failure test starts from a closed breaker.
        patch("aether_engine.memory.episodic._embed_unavailable_until", 0.0),
        patch("aether_engine.memory.episodic._audit_logger.log_event") as mock_log,
    ):
        yield test_db, test_emb_db, mock_log


def _audit_calls(mock_log, action):
    return [c for c in mock_log.call_args_list if c.args and c.args[0] == action]


def _embedding_row(test_emb_db, task_id):
    conn = sqlite3.connect(test_emb_db)
    try:
        row = conn.execute(
            "SELECT task_id FROM episodic_embeddings WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return row
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


async def test_embedding_failure_stores_episode_unembedded_and_logs_failure(
    isolated_dbs, caplog
):
    _test_db, test_emb_db, mock_log = isolated_dbs
    task_id = "task-embed-fail-1"

    with patch(
        "aether_engine.memory.episodic.litellm.aembedding",
        new=AsyncMock(side_effect=ConnectionError("Ollama down")),
    ), caplog.at_level("WARNING", logger="aether_engine.memory.episodic"):
        await store_episode(
            task_id, "Create a python script", "Generated a script",
            ["python"], "SUCCESS",
        )

    # Episode itself is still stored (FTS row present).
    episodes = episodic.get_all_episodes()
    assert [e["task_id"] for e in episodes] == [task_id]

    # ...but it is explicitly NOT embedded: no embedding row.
    assert _embedding_row(test_emb_db, task_id) is None

    # Failure is logged distinctly via the audit logger: model, error, episode id.
    failures = _audit_calls(mock_log, "EPISODIC_EMBEDDING_FAILED")
    assert len(failures) == 1
    details = failures[0].args[1]
    assert details["task_id"] == task_id
    # Epic 8, E8-3.2: the default embedder was unified to
    # qwen3-embedding:0.6b (ONE embedder for episodic memory + the
    # prose index); the failure record names the resolved model.
    assert details["model"] == "ollama/qwen3-embedding:0.6b"
    assert "Ollama down" in details["error"]
    assert "error_type" in details

    # The stored marker says embedded=False (project's existing marker).
    stored = _audit_calls(mock_log, "EPISODIC_MEMORY_STORED")
    assert len(stored) == 1
    assert stored[0].args[1]["embedded"] is False

    # And the module logger carries the same failure (caplog-visible).
    assert "EPISODIC_EMBEDDING_FAILED" in caplog.text or "Ollama down" in caplog.text


async def test_embedding_timeout_stores_episode_unembedded(isolated_dbs):
    _test_db, test_emb_db, mock_log = isolated_dbs
    task_id = "task-embed-timeout-1"

    with patch(
        "aether_engine.memory.episodic.litellm.aembedding",
        new=AsyncMock(side_effect=TimeoutError()),
    ):
        await store_episode(
            task_id, "Do a thing", "Did the thing", ["test"], "SUCCESS",
        )

    assert _embedding_row(test_emb_db, task_id) is None
    failures = _audit_calls(mock_log, "EPISODIC_EMBEDDING_FAILED")
    assert len(failures) == 1
    assert failures[0].args[1]["task_id"] == task_id
    assert failures[0].args[1]["error_type"] == "TimeoutError"
    stored = _audit_calls(mock_log, "EPISODIC_MEMORY_STORED")
    assert stored[0].args[1]["embedded"] is False


async def test_embedding_failure_clears_stale_embedding_row(isolated_dbs):
    """A re-store after a failure must not leave a stale row masquerading
    as a successful embed (the pre-fix code never deleted it)."""
    _test_db, test_emb_db, mock_log = isolated_dbs
    task_id = "task-embed-stale-1"

    fake_response = MagicMock()
    fake_response.data = [{"embedding": [0.1, 0.2, 0.3]}]
    with patch(
        "aether_engine.memory.episodic.litellm.aembedding",
        new=AsyncMock(return_value=fake_response),
    ):
        await store_episode(
            task_id, "First attempt", "First summary", ["test"], "SUCCESS",
        )
    assert _embedding_row(test_emb_db, task_id) is not None

    with patch(
        "aether_engine.memory.episodic.litellm.aembedding",
        new=AsyncMock(side_effect=RuntimeError("Ollama restarted")),
    ):
        await store_episode(
            task_id, "Second attempt", "Second summary", ["test"], "SUCCESS",
        )
    assert _embedding_row(test_emb_db, task_id) is None
    stored = _audit_calls(mock_log, "EPISODIC_MEMORY_STORED")
    assert stored[-1].args[1]["embedded"] is False


async def test_embed_text_timeout_is_30_seconds():
    """Prompt B2 requires 30s (cold Ollama start); the pre-fix value was 1.0s."""
    from aether_engine.memory.episodic import EMBEDDING_TIMEOUT_SECONDS

    assert EMBEDDING_TIMEOUT_SECONDS == 30.0

    fake_response = MagicMock()
    fake_response.data = [{"embedding": [0.5]}]
    captured = {}

    async def fake_wait_for(coro, timeout=None):
        captured["timeout"] = timeout
        coro.close()
        return fake_response

    with (
        # Epic 9: an earlier failure test may have tripped the
        # embedding circuit breaker; this test needs a closed one.
        patch("aether_engine.memory.episodic._embed_unavailable_until", 0.0),
        patch("aether_engine.memory.episodic.asyncio.wait_for", new=fake_wait_for),
    ):
        result = await _embed_text("hello")
    assert result == [0.5]
    assert captured["timeout"] == 30.0
