"""Startup KG initialisation: the knowledge-graph store is created
and switched to WAL during engine startup, before any extraction
threads exist (belt-and-braces alongside _ensure_wal_mode, which
made a concurrent first open safe on its own).

Pins:
  (a) the startup init helper leaves a ready store behind — file
      created, entities/facts schema present, journal mode WAL;
  (b) a KG init failure is logged and swallowed — startup must
      never depend on it, because lazy init on first use remains
      the fallback;
  (c) the lifespan actually runs it for the native memory backend:
      after app startup, a fresh KG path is already initialised.
"""
import asyncio
import logging
import sqlite3
from unittest.mock import MagicMock, patch

from aether_engine.memory import knowledge_graph as kg


def _fresh_kg(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    monkeypatch.setattr(kg, "DB_PATH", db)
    kg._DEDUPED_DB_PATHS.discard(str(db))
    return db


def test_startup_init_creates_schema_and_lands_wal(tmp_path, monkeypatch):
    from aether_engine.app import _init_knowledge_graph_at_startup

    db = _fresh_kg(tmp_path, monkeypatch)
    asyncio.run(_init_knowledge_graph_at_startup())

    assert db.exists()
    with sqlite3.connect(db) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert str(mode).lower() == "wal"
    assert {"entities", "facts"} <= tables


def test_startup_kg_init_failure_is_logged_and_swallowed(caplog):
    from aether_engine.app import _init_knowledge_graph_at_startup

    def boom():
        raise RuntimeError("kg store unavailable")

    with (
        patch("aether_engine.memory.knowledge_graph.init_kg_db", side_effect=boom),
        caplog.at_level(logging.WARNING, logger="aether_engine.app"),
    ):
        # Must not raise — startup continues and first use retries
        # the init lazily, as before this existed.
        asyncio.run(_init_knowledge_graph_at_startup())
    assert any(
        "knowledge-graph initialisation failed" in r.getMessage()
        for r in caplog.records
    )


def test_lifespan_initialises_kg_for_native_backend(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from aether_engine.app import app, engine_state

    db = _fresh_kg(tmp_path, monkeypatch)
    orig_backend = engine_state.user_config.memory_backend
    engine_state.user_config.memory_backend = "native"
    mock_store = MagicMock()
    mock_store.list_providers.return_value = []

    async def no_fetch(provider, key, base_url=None, provider_flavor=None):
        from aether_engine.providers.discovery import DiscoveryResult

        return DiscoveryResult(models=[], excluded=[])

    try:
        with (
            patch("aether_engine.app.SecretStore", return_value=mock_store),
            patch("aether_engine.app.fetch_available_models", new=no_fetch),
            patch("aether_engine.app.save_config"),
            TestClient(app) as client,
        ):
            assert client.get("/health").status_code == 200
            # The lifespan awaited the startup KG init before yield:
            # the fresh store is already created and in WAL mode.
            assert db.exists()
            with sqlite3.connect(db) as conn:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert str(mode).lower() == "wal"
    finally:
        engine_state.user_config.memory_backend = orig_backend
