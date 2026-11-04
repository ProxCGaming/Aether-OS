"""Embedder lifecycle: fail-fast probe + startup gate
(models/catalogue-embedder, Part B).

- Once an attempt has failed connection-class, the endpoint is
  suspect: later attempts probe the TCP connect at a 2s bound and
  fail in ~2s instead of paying the 30s embedding timeout against
  a dead endpoint; a successful probe hands over to the real call.
- The startup gate (app._ensure_embedder_then_backfill) runs the
  backfill only when the embedder is ready/pulled; an unavailable
  embedder is recorded, warned about once, and skips the backfill.
- embedding_stats() carries the recorded embedder_status for the
  Memory settings.
"""
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from aether_engine.memory import episodic


@pytest.fixture(autouse=True)
def _reset_embed_state():
    episodic._embed_unavailable_until = 0.0
    episodic._embed_endpoint_suspect = False
    episodic.set_embedder_status(
        {"model": episodic.DEFAULT_EMBEDDER, "state": "unknown", "detail": ""})
    yield
    episodic._embed_unavailable_until = 0.0
    episodic._embed_endpoint_suspect = False


def _ok_response():
    return type("R", (), {"data": [{"embedding": [0.1, 0.2]}]})()


@pytest.mark.asyncio
async def test_suspect_endpoint_fails_fast_via_probe():
    episodic._embed_endpoint_suspect = True
    embed = AsyncMock(return_value=_ok_response())
    with patch("aether_engine.memory.episodic.litellm.aembedding", new=embed), \
            patch("aether_engine.memory.episodic._probe_embed_endpoint",
                  new=AsyncMock(return_value=False)), \
            patch("aether_engine.memory.episodic._audit_logger.log_event"):
        assert await episodic._embed_text("hello", task_id="t-probe") is None
    # The real call was never attempted, and the breaker re-tripped
    # so subsequent attempts skip outright during the cooldown.
    assert embed.await_count == 0
    assert episodic._embed_breaker_open()


@pytest.mark.asyncio
async def test_suspect_endpoint_probe_success_hands_over():
    episodic._embed_endpoint_suspect = True
    embed = AsyncMock(return_value=_ok_response())
    with patch("aether_engine.memory.episodic.litellm.aembedding", new=embed), \
            patch("aether_engine.memory.episodic._probe_embed_endpoint",
                  new=AsyncMock(return_value=True)):
        assert await episodic._embed_text("hello", task_id="t-probe2") == [0.1, 0.2]
    assert embed.await_count == 1
    # A success clears the suspicion.
    assert episodic._embed_endpoint_suspect is False


@pytest.mark.asyncio
async def test_connection_failure_marks_endpoint_suspect():
    embed = AsyncMock(side_effect=ConnectionError("Ollama down"))
    with patch("aether_engine.memory.episodic.litellm.aembedding", new=embed), \
            patch("aether_engine.memory.episodic._audit_logger.log_event"):
        assert await episodic._embed_text("hello", task_id="t-suspect") is None
    assert episodic._embed_endpoint_suspect is True


def test_note_embedder_unavailable_records_and_arms_breaker():
    episodic.note_embedder_unavailable(
        {"model": "qwen3-embedding:0.6b", "state": "ollama_off",
         "detail": "Ollama is not running"})
    status = episodic.embedder_status()
    assert status["state"] == "ollama_off"
    assert status["model"] == "qwen3-embedding:0.6b"
    assert episodic._embed_breaker_open()
    assert episodic._embed_endpoint_suspect is True


@pytest.fixture
def isolated_stats_dbs(tmp_path):
    """embedding_stats() reads both stores; isolate them on tmp
    files (the test_phase10_1 recipe)."""
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
    stack.enter_context(
        patch("aether_engine.memory.episodic.EMBEDDINGS_DB_PATH", emb_db))
    stack.enter_context(
        patch("aether_engine.memory.episodic._get_conn", side_effect=fake_get_conn))
    yield
    stack.close()


def test_embedding_stats_carry_embedder_status(isolated_stats_dbs):
    episodic.set_embedder_status(
        {"model": "qwen3-embedding:0.6b", "state": "pulled", "detail": ""})
    stats = episodic.embedding_stats()
    assert stats["embedder_status"]["state"] == "pulled"
    assert stats["embedder_status"]["model"] == "qwen3-embedding:0.6b"


# --- startup gate -------------------------------------------------------------

@pytest.mark.asyncio
async def test_startup_gate_skips_backfill_when_ollama_off(monkeypatch):
    import importlib

    app_module = importlib.import_module("aether_engine.app")
    from aether_engine.config import UserConfig

    monkeypatch.setattr(app_module.engine_state, "user_config", UserConfig())
    monkeypatch.setattr(app_module.engine_state, "ollama_manager", object())
    ensure = AsyncMock(return_value={
        "state": "ollama_off", "model": "qwen3-embedding:0.6b",
        "detail": "Ollama is not running"})
    backfill = AsyncMock(return_value={})
    with patch("aether_engine.models.local_models.ensure_embedder", new=ensure), \
            patch("aether_engine.memory.episodic.backfill_episodic_embeddings",
                  new=backfill):
        await app_module._ensure_embedder_then_backfill()
    assert backfill.await_count == 0
    assert episodic.embedder_status()["state"] == "ollama_off"
    assert episodic._embed_breaker_open()


@pytest.mark.asyncio
async def test_startup_gate_runs_backfill_when_ready(monkeypatch):
    import importlib

    app_module = importlib.import_module("aether_engine.app")
    from aether_engine.config import UserConfig

    monkeypatch.setattr(app_module.engine_state, "user_config", UserConfig())
    monkeypatch.setattr(app_module.engine_state, "ollama_manager", object())
    ensure = AsyncMock(return_value={
        "state": "ready", "model": "qwen3-embedding:0.6b", "detail": ""})
    backfill = AsyncMock(return_value={})
    with patch("aether_engine.models.local_models.ensure_embedder", new=ensure), \
            patch("aether_engine.memory.episodic.backfill_episodic_embeddings",
                  new=backfill):
        await app_module._ensure_embedder_then_backfill()
    assert backfill.await_count == 1
    assert episodic.embedder_status()["state"] == "ready"
