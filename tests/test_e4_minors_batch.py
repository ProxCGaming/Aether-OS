"""Epic 4 minors batch — E4-F10, E4-F11, E4-F12, E9-F01.

Four findings from the Astra full-product review, each re-verified
against current code before fixing:

* **E4-F10** — the episodic FTS sanitizer strips only ``"``, ``'``
  and ``:``; any other FTS5 syntax in an ordinary query (``Python?``)
  makes ``MATCH`` raise ``OperationalError`` and the whole lexical
  leg is discarded, so degraded (embeddings-down) recall returns
  nothing for punctuated queries.
* **E9-F01** — the embedding circuit breaker's classifier accepts
  only builtin ``ConnectionError``/``TimeoutError`` or the exact
  class names ``APIConnectionError``/``ConnectError``. The real
  timeout classes (``litellm.Timeout``, ``openai.APITimeoutError``,
  ``httpx.ReadTimeout``) match none of those, so a timing-out
  endpoint is retried on every call instead of pausing for the 60 s
  cooldown.
* **E4-F11** — the native stores (knowledge graph, sessions,
  episodic + embeddings) evolve their schemas through inspection /
  try-except migrations but record no application schema version
  anywhere: ``PRAGMA user_version`` stays 0 and there is no version
  marker a future migration could gate on.
* **E4-F12** — ``CONFIG_UPDATE`` applies a fixed field set that
  omits ``memory_backend``: an update naming it is acknowledged
  ``success: True`` while the live config, the saved file and the
  facade's backend all stay on the old value.

TDD: every test here fails on the pre-fix code for the stated reason
and passes after the fix. All DBs are tmp files; the socket tests
follow the E6-F05 isolated-settings fixture pattern (tmp
``CONFIG_PATH``, audit log path set before ``initialize_auth()``).
"""
import asyncio
import sqlite3
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

import aether_engine.config as config_module
import aether_engine.state as state_module
from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig, load_config, save_config
from aether_engine.memory import episodic, facade, knowledge_graph, session_store
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.registry import ModelRegistry
from aether_engine.secrets.dpapi import BaseProtector, SecretDecryptionError
from aether_engine.secrets.storage import SecretStore

# ---------------------------------------------------------------------------
# Shared episodic isolation (the test_phase10_3_episodic_recall pattern:
# ``_get_conn`` binds DB_PATH as a default arg, so redirect it wholesale).
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_episodic(tmp_path):
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
    stack.enter_context(
        patch("aether_engine.memory.episodic._audit_logger.log_event"))
    try:
        yield {"mem_db": mem_db, "emb_db": emb_db}
    finally:
        stack.close()


# ---------------------------------------------------------------------------
# E4-F10 — punctuation must not kill the lexical leg.
# ---------------------------------------------------------------------------

async def _store_python_episode():
    await episodic.store_episode(
        "task-python", "How do I sort a list in Python?",
        "Python sorting guide with sorted() examples",
        ["python"], "answered",
    )


@pytest.mark.asyncio
async def test_lexical_leg_survives_punctuation(isolated_episodic):
    # Embeddings are down: _embed_text returns None for both the
    # store and the query, so recall is lexical-only.
    with patch("aether_engine.memory.episodic._embed_text",
               new=AsyncMock(return_value=None)):
        await _store_python_episode()
        plain = await episodic.query_episodes("Python", backfill_limit=0)
        punctuated = await episodic.query_episodes("Python?", backfill_limit=0)
    assert [r["task_id"] for r in plain] == ["task-python"]
    # Pre-fix: the MATCH parser error emptied the whole lexical leg.
    assert [r["task_id"] for r in punctuated] == ["task-python"]


@pytest.mark.asyncio
async def test_lexical_leg_survives_operator_shaped_queries(isolated_episodic):
    with patch("aether_engine.memory.episodic._embed_text",
               new=AsyncMock(return_value=None)):
        await _store_python_episode()
        for query in (
            "Python (guide): sorted?",
            "Python AND sorting",
            "sorting * guide",
            '"Python" guide',
        ):
            rows = await episodic.query_episodes(query, backfill_limit=0)
            assert [r["task_id"] for r in rows] == ["task-python"], query


@pytest.mark.asyncio
async def test_tokenless_query_degrades_to_no_match(isolated_episodic):
    with patch("aether_engine.memory.episodic._embed_text",
               new=AsyncMock(return_value=None)):
        await _store_python_episode()
        # Pure punctuation has no searchable tokens: no-match, and
        # crucially no raised error / no dead search.
        assert await episodic.query_episodes("?!*()", backfill_limit=0) == []


# ---------------------------------------------------------------------------
# E9-F01 — real timeout exception families trip the embedding breaker.
# ---------------------------------------------------------------------------

@pytest.fixture
def closed_breaker():
    previous = episodic._embed_unavailable_until
    episodic._embed_unavailable_until = 0.0
    try:
        yield
    finally:
        episodic._embed_unavailable_until = previous


def _real_timeout_exceptions():
    import httpx
    import litellm
    import openai

    return [
        litellm.Timeout(message="slow", model="m", llm_provider="ollama"),
        openai.APITimeoutError(httpx.Request("POST", "http://127.0.0.1:11434")),
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.ConnectError("connection refused"),
        openai.APIConnectionError(
            request=httpx.Request("POST", "http://127.0.0.1:11434")),
    ]


@pytest.mark.parametrize("exc_factory", [
    pytest.param(i, id=name) for i, name in enumerate([
        "litellm.Timeout", "openai.APITimeoutError", "httpx.ReadTimeout",
        "httpx.ConnectTimeout", "httpx.ConnectError",
        "openai.APIConnectionError",
    ])
])
def test_breaker_trips_on_real_timeout_families(closed_breaker, exc_factory):
    exc = _real_timeout_exceptions()[exc_factory]
    episodic._trip_embed_breaker(exc)
    assert episodic._embed_breaker_open(), type(exc).__name__


def test_breaker_still_ignores_arbitrary_errors(closed_breaker):
    for exc in (RuntimeError("bad request"), ValueError("nope"),
                KeyError("missing")):
        episodic._trip_embed_breaker(exc)
        assert not episodic._embed_breaker_open(), type(exc).__name__


def test_real_timeout_pauses_endpoint_attempts(closed_breaker):
    import litellm

    failing = AsyncMock(side_effect=litellm.Timeout(
        message="slow", model="m", llm_provider="ollama"))
    with patch("aether_engine.memory.episodic.litellm.aembedding", new=failing):
        assert asyncio.run(episodic._embed_text("one", task_id="t1")) is None
        assert episodic._embed_breaker_open()
        # Second call must be skipped by the open breaker — pre-fix it
        # reached the endpoint again (the finding's 6-vs-1 shape).
        assert asyncio.run(episodic._embed_text("two", task_id="t2")) is None
        assert failing.await_count == 1


# ---------------------------------------------------------------------------
# E4-F11 — every native store records its application schema version.
# ---------------------------------------------------------------------------

def _meta_version(db_file, store):
    with sqlite3.connect(db_file) as conn:
        row = conn.execute(
            "SELECT version FROM aether_schema_meta WHERE store = ?",
            (store,),
        ).fetchone()
    return row[0] if row else None


def _user_version(db_file):
    with sqlite3.connect(db_file) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def test_kg_records_schema_version_on_fresh_and_legacy_dbs(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    # Legacy schema: facts without the E4-02 supersede columns, one row.
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TABLE entities (
                id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                entity_type TEXT, first_seen INTEGER NOT NULL,
                last_updated INTEGER NOT NULL)
        """)
        conn.execute("""
            CREATE TABLE facts (
                id TEXT PRIMARY KEY, entity_id TEXT NOT NULL,
                fact_text TEXT NOT NULL, source_task_id TEXT,
                timestamp INTEGER NOT NULL, confidence REAL,
                FOREIGN KEY(entity_id) REFERENCES entities(id)
                    ON DELETE CASCADE)
        """)
        conn.execute(
            "INSERT INTO entities VALUES ('e1', 'User', 'Person', 1, 1)")
        conn.execute(
            "INSERT INTO facts VALUES ('f1', 'e1', 'User lives in Kolkata',"
            " 't1', 1, 0.9)")
        conn.commit()
    assert _user_version(db) == 0  # the audited before-state

    monkeypatch.setattr(knowledge_graph, "DB_PATH", db)
    knowledge_graph.init_kg_db()

    with sqlite3.connect(db) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(facts)")}
        row = conn.execute(
            "SELECT fact_text, status FROM facts WHERE id = 'f1'").fetchone()
    assert {"status", "superseded_by"} <= columns  # migration still ran
    assert row == ("User lives in Kolkata", "active")  # data preserved
    assert _meta_version(db, "knowledge_graph") == knowledge_graph.SCHEMA_VERSION
    assert _user_version(db) >= knowledge_graph.SCHEMA_VERSION

    # Re-init is idempotent: the marker stays in step, nothing moves.
    knowledge_graph.init_kg_db()
    assert _meta_version(db, "knowledge_graph") == knowledge_graph.SCHEMA_VERSION


def test_sessions_record_schema_version_on_fresh_and_legacy_dbs(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)
        """)
        conn.execute("""
            CREATE TABLE session_messages (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
                    ON DELETE CASCADE)
        """)
        conn.execute("INSERT INTO sessions VALUES ('s1', 'Chat', 1, 1)")
        conn.execute(
            "INSERT INTO session_messages VALUES ('m1', 's1', 'user',"
            " 'hello', 1)")
        conn.commit()
    assert _user_version(db) == 0

    monkeypatch.setattr(session_store, "DB_PATH", db)
    monkeypatch.setattr(session_store, "_initialized", False)
    session_store.init_db()

    with sqlite3.connect(db) as conn:
        columns = {r[1] for r in conn.execute(
            "PRAGMA table_info(session_messages)")}
        row = conn.execute(
            "SELECT content FROM session_messages WHERE id = 'm1'").fetchone()
    assert {"thoughts", "attachments"} <= columns
    assert row == ("hello",)
    assert _meta_version(db, "sessions") == session_store.SCHEMA_VERSION
    assert _user_version(db) >= session_store.SCHEMA_VERSION


def test_episodic_stores_record_schema_versions(isolated_episodic):
    episodic.init_episodic_db()
    episodic.init_embeddings_db()
    assert _meta_version(
        isolated_episodic["mem_db"], "episodic") == episodic.EPISODIC_SCHEMA_VERSION
    assert _meta_version(
        isolated_episodic["emb_db"],
        "episodic_embeddings") == episodic.EMBEDDINGS_SCHEMA_VERSION
    assert _user_version(isolated_episodic["emb_db"]) >= (
        episodic.EMBEDDINGS_SCHEMA_VERSION)
    assert _user_version(isolated_episodic["mem_db"]) >= (
        episodic.EPISODIC_SCHEMA_VERSION)


def test_embeddings_legacy_db_migrates_and_records_version(isolated_episodic):
    emb_db = isolated_episodic["emb_db"]
    # Legacy embeddings table: no N02 model/dimension identity columns.
    with sqlite3.connect(emb_db) as conn:
        conn.execute("""
            CREATE TABLE episodic_embeddings (
                task_id TEXT PRIMARY KEY, embedding BLOB)
        """)
        conn.execute(
            "INSERT INTO episodic_embeddings VALUES ('t1', X'00')")
        conn.commit()
    assert _user_version(emb_db) == 0

    episodic.init_embeddings_db()

    with sqlite3.connect(emb_db) as conn:
        columns = {r[1] for r in conn.execute(
            "PRAGMA table_info(episodic_embeddings)")}
        count = conn.execute(
            "SELECT COUNT(*) FROM episodic_embeddings").fetchone()[0]
    assert {"model", "dimension"} <= columns
    assert count == 1
    assert _meta_version(
        emb_db, "episodic_embeddings") == episodic.EMBEDDINGS_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# E4-F12 — CONFIG_UPDATE honours memory_backend, strictly.
# ---------------------------------------------------------------------------

class _DummyProtector(BaseProtector):
    """The E6-F05 seam: reversible, no OS keyring."""

    def protect(self, data: bytes) -> bytes:
        return b"ENC:" + data

    def unprotect(self, data: bytes) -> bytes:
        if not data.startswith(b"ENC:"):
            raise SecretDecryptionError("Invalid header")
        return data[4:]


@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    """Isolated engine state (the E6-F05 fixture pattern): tmp config
    file + tmp secret store. Audit log path is set BEFORE
    initialize_auth() per the fixture law."""
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"

    store = SecretStore(db_path=tmp_path / "secrets.db",
                        protector=_DummyProtector())
    monkeypatch.setattr(state_module, "SecretStore", lambda *a, **k: store)
    monkeypatch.setattr(ws_module, "SecretStore", lambda *a, **k: store)

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    save_config(UserConfig(), config_path)

    monkeypatch.setattr(engine_state, "model_registry", ModelRegistry([]))
    monkeypatch.setattr(engine_state, "health_manager", ProviderHealthManager())
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))
    facade._BACKEND_CACHE.clear()

    engine_state.initialize_auth()
    yield {"tmp_path": tmp_path, "config_path": config_path}
    facade._BACKEND_CACHE.clear()


class _Socket:
    def __enter__(self):
        self._client = TestClient(app)
        self._client.__enter__()
        self._cm = self._client.websocket_connect(
            "/ws/tasks",
            headers={"Authorization": f"Bearer {engine_state.auth_token}"})
        self.ws = self._cm.__enter__()
        hello = Event.from_json(self.ws.receive_text())
        assert hello.type == EventType.HELLO
        return self.ws

    def __exit__(self, *exc):
        self._cm.__exit__(*exc)
        self._client.__exit__(*exc)
        return False


def _update(ws, config):
    ws.send_text(Event(
        type=EventType.CONFIG_UPDATE_REQUEST,
        payload={"config": config}).to_json())
    for _ in range(12):
        ev = Event.from_json(ws.receive_text())
        if ev.type == EventType.CONFIG_UPDATE_RESPONSE:
            return ev
    raise AssertionError("no CONFIG_UPDATE_RESPONSE received")


def test_config_update_applies_memory_backend(settings_env):
    with _Socket() as ws:
        ev = _update(ws, {"memory_backend": "langmem"})
    # Pre-fix: success=True but the reply (and everything else) still
    # said "native" — the write was silently dropped.
    assert ev.payload["success"] is True
    assert ev.payload["config"]["memory_backend"] == "langmem"
    assert engine_state.user_config.memory_backend == "langmem"
    assert load_config().memory_backend == "langmem"  # persisted


def test_config_update_memory_backend_takes_effect_on_the_facade(settings_env):
    # Start from a persisted langmem selection (unavailable in this
    # install, so the facade degrades to None), then switch live.
    save_config(UserConfig(memory_backend="langmem"),
                settings_env["config_path"])
    with _Socket() as ws:
        # The lifespan reloaded user_config from the saved file.
        assert engine_state.user_config.memory_backend == "langmem"
        assert facade.backend_or_none(engine_state.user_config) is None
        ev = _update(ws, {"memory_backend": "native"})
    assert ev.payload["success"] is True
    backend = facade.backend_or_none(engine_state.user_config)
    assert backend is not None and backend.name == "native"


def test_config_update_normalises_memory_backend(settings_env):
    with _Socket() as ws:
        ev = _update(ws, {"memory_backend": " LangMem "})
    assert ev.payload["success"] is True
    assert engine_state.user_config.memory_backend == "langmem"
    assert load_config().memory_backend == "langmem"


def test_config_update_refuses_unknown_memory_backend(settings_env):
    with _Socket() as ws:
        ev = _update(ws, {"memory_backend": "bogus",
                          "supervisor_delegation_cap": 7})
    assert ev.payload["success"] is False
    assert "memory_backend" in ev.payload["error"]
    # The refusal is whole-update: the valid sibling field in the
    # same request was NOT applied either, and nothing was saved.
    assert engine_state.user_config.memory_backend == "native"
    assert engine_state.user_config.supervisor_delegation_cap != 7
    assert load_config().memory_backend == "native"


def test_config_update_refuses_non_string_memory_backend(settings_env):
    with _Socket() as ws:
        ev = _update(ws, {"memory_backend": 42})
    assert ev.payload["success"] is False
    assert "memory_backend" in ev.payload["error"]
    assert engine_state.user_config.memory_backend == "native"
