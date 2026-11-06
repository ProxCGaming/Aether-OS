"""Wave 2 remainder — memory: E4-01, N-07, N-12 (episodic/KG), N-20, N-36.

Each test pins one audited defect:

- E4-01: ESCALATED (see the section below) — the card and the
  E4-F03 pinning test assert opposite contracts; only the agreed
  half (re-store WITH a thinking log replaces it) is pinned here.
- N-07: concurrent KG extraction naming the same new entity must not
  lose a whole fact batch to the check-then-insert race.
- N-12: the synchronous SQLite work inside ``store_episode`` /
  ``query_episodes`` / KG extraction must not run on the event loop.
- N-20: episodic backfill must not load every episode's outcome text
  before applying its limit, and the recall semantic leg must not
  ``fetchall`` the whole embeddings table.
- N-36: session search must escape LIKE wildcards (the KG twin does).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

from aether_engine.memory import episodic, session_store
from aether_engine.memory import knowledge_graph as kg
from aether_engine.providers.base import StreamChunk

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_episodic(tmp_path):
    """Point both episodic DBs at tmp files (the established redirect:
    ``_get_conn`` binds DB_PATH as a default arg, so patch it too)."""
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

    episodic._embed_endpoint_suspect = False
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
        episodic._embed_endpoint_suspect = False


@pytest.fixture
def kg_db(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    monkeypatch.setattr(kg, "DB_PATH", db)
    return db


async def _no_embed(text, task_id=None, context=None):
    return None


async def _probe_delay(coro, probe_s=0.05):
    """Run *coro* as a task and measure how long a short sleep is
    delayed by it: ~probe_s when the loop stays free, ~the blocking
    duration when the op runs synchronous work on the loop thread."""
    task = asyncio.create_task(coro)
    start = time.monotonic()
    await asyncio.sleep(probe_s)
    delay = time.monotonic() - start
    result = await task
    return delay, result


# ---------------------------------------------------------------------------
# E4-01 — ESCALATED, not fixed: the card (Pass A, Pass B CONFIRMED)
# says a re-store without a thinking log must not leave the previous
# revision's thinking attached, but the E4-F03 wave deliberately
# pinned the opposite contract as facade semantics
# (test_epic4_e4f03_backfill_fences.py::
# test_completed_re_store_without_thinking_keeps_prior_thinking).
# Both artefacts name the same mechanism and the same resume case.
# Pending an owner ruling, the retention behaviour is untouched;
# the guard below pins only the half both contracts agree on.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restore_with_thinking_log_replaces_it(
        isolated_episodic, monkeypatch):
    monkeypatch.setattr(episodic, "_embed_text", _no_embed)
    await episodic.store_episode(
        "task-1", "prompt", "summary v1", [], "outcome v1",
        thinking_log="FIRST")
    await episodic.store_episode(
        "task-1", "prompt", "summary v2", [], "outcome v2",
        thinking_log="SECOND")
    rows = episodic.get_all_episodes()
    assert rows[0]["thinking_log"] == "SECOND"


# ---------------------------------------------------------------------------
# N-07 — concurrent KG extraction loses the whole batch
# ---------------------------------------------------------------------------

class _OneFactProvider:
    def __init__(self, fact_text: str):
        self._fact_text = fact_text

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        blob = json.dumps(messages)
        if "OLD facts:" in blob:  # reconciliation prompt
            yield StreamChunk(text="[]", finish_reason="stop")
            return
        yield StreamChunk(
            text=json.dumps([{
                "entity_name": "User", "entity_type": "Person",
                "fact": self._fact_text, "confidence": 0.9,
            }]),
            finish_reason="stop")


def test_concurrent_extraction_same_new_entity_keeps_all_batches(
        kg_db, monkeypatch):
    # Widen the check-then-insert window so the race is deterministic:
    # every thread pauses inside the dedupe scan, then all proceed to
    # the entity SELECT/INSERT together.
    original_find = kg._find_duplicate_fact_id

    def slowed_find(conn, entity_name, fact_text):
        time.sleep(0.15)
        return original_find(conn, entity_name, fact_text)

    monkeypatch.setattr(kg, "_find_duplicate_fact_id", slowed_find)

    facts = [f"User fact number {i}" for i in range(4)]
    barrier = threading.Barrier(len(facts))

    def run_one(fact_text: str):
        barrier.wait(timeout=10)
        asyncio.run(kg.extract_and_store_facts(
            _OneFactProvider(fact_text),
            "Tell me about the user please",
            "Here is a substantive answer about the user.",
            f"task-{fact_text}",
        ))

    threads = [threading.Thread(target=run_one, args=(f,)) for f in facts]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    stored = {f["fact_text"] for f in kg.get_all_facts()}
    assert stored == set(facts), stored


# ---------------------------------------------------------------------------
# N-12 — synchronous SQLite inside async memory functions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_episode_db_write_does_not_block_loop(
        isolated_episodic, monkeypatch):
    monkeypatch.setattr(episodic, "_embed_text", _no_embed)
    current = episodic._get_conn
    calls = {"n": 0}

    def slow_first_get_conn(db_path=None):
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(0.4)  # the write block's first connection
        return current(db_path)

    monkeypatch.setattr(episodic, "_get_conn", slow_first_get_conn)
    delay, _ = await _probe_delay(episodic.store_episode(
        "task-block", "prompt", "summary", [], "outcome",
        thinking_log="thinking"))
    assert delay < 0.25, f"event loop stalled {delay:.2f}s by store_episode"


@pytest.mark.asyncio
async def test_query_episodes_bm25_leg_does_not_block_loop(
        isolated_episodic, monkeypatch):
    monkeypatch.setattr(episodic, "_embed_text", _no_embed)
    current = episodic._get_conn
    calls = {"n": 0}

    def slow_first_get_conn(db_path=None):
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(0.4)
        return current(db_path)

    monkeypatch.setattr(episodic, "_get_conn", slow_first_get_conn)
    delay, results = await _probe_delay(episodic.query_episodes(
        "anything", backfill_limit=0))
    assert results == []
    assert delay < 0.25, f"event loop stalled {delay:.2f}s by query_episodes"


@pytest.mark.asyncio
async def test_kg_insert_loop_does_not_block_loop(kg_db, monkeypatch):
    original_find = kg._find_duplicate_fact_id

    def slowed_find(conn, entity_name, fact_text):
        time.sleep(0.4)
        return original_find(conn, entity_name, fact_text)

    monkeypatch.setattr(kg, "_find_duplicate_fact_id", slowed_find)
    delay, _ = await _probe_delay(kg.extract_and_store_facts(
        _OneFactProvider("User drinks tea"),
        "Tell me about the user please",
        "Here is a substantive answer about the user.",
        "task-probe"))
    assert delay < 0.25, f"event loop stalled {delay:.2f}s by KG extraction"
    assert [f["fact_text"] for f in kg.get_all_facts()] == ["User drinks tea"]


# ---------------------------------------------------------------------------
# N-20 — backfill / recall load whole tables before applying limits
# ---------------------------------------------------------------------------

class _CountingCursor(sqlite3.Cursor):
    _sql = ""

    def execute(self, sql, parameters=()):
        self._sql = sql
        return super().execute(sql, parameters)

    def fetchall(self):
        rows = super().fetchall()
        self.connection.record("fetchall", self._sql, len(rows))
        return rows

    def __next__(self):
        row = super().__next__()
        self.connection.record("iter", self._sql, 1)
        return row


class _CountingConn(sqlite3.Connection):
    registry: ClassVar[list] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events: list[tuple[str, str, int]] = []
        _CountingConn.registry.append(self)

    def cursor(self, factory=None):
        return super().cursor(_CountingCursor)

    def execute(self, sql, parameters=()):
        # The C-level shortcut does not dispatch to the overridden
        # cursor() — route it through the counting cursor explicitly.
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        return self.cursor().executemany(sql, seq_of_parameters)

    def record(self, kind: str, sql: str, n: int):
        self.events.append((kind, sql, n))


def _rows_materialised(sql_needle: str, kinds=("fetchall", "iter")) -> int:
    total = 0
    for conn in _CountingConn.registry:
        for kind, sql, n in conn.events:
            if kind in kinds and sql_needle in sql and "SELECT" in sql.upper():
                total += n
    return total


@pytest.fixture
def counting_episodic(tmp_path):
    """Episodic DBs behind row-counting connections."""
    mem_db = tmp_path / "memory.db"
    emb_db = tmp_path / "embeddings.db"
    real_db = episodic.DB_PATH
    real_emb_db = episodic.EMBEDDINGS_DB_PATH
    _CountingConn.registry = []

    def fake_get_conn(db_path=None):
        if db_path is None or db_path in (real_db, mem_db):
            target = mem_db
        elif db_path in (real_emb_db, emb_db):
            target = emb_db
        else:
            target = Path(db_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target, factory=_CountingConn)
        conn.row_factory = sqlite3.Row
        return conn

    episodic._embed_endpoint_suspect = False
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
        episodic._embed_endpoint_suspect = False


def _insert_episode_row(task_id: str, summary: str, outcome: str):
    episodic.init_episodic_db()
    with episodic._get_conn() as conn:
        conn.execute(
            "INSERT INTO episodic_memory"
            " (task_id, timestamp, user_prompt, task_summary, tags, outcome)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, 1_700_000_000, "prompt", summary, "", outcome))
        conn.commit()


@pytest.mark.asyncio
async def test_backfill_reads_outcomes_only_for_attempted(
        counting_episodic, monkeypatch):
    for i in range(30):
        _insert_episode_row(f"t-{i}", f"summary {i}", f"OUTCOME-{i} " + "x" * 200)
    _CountingConn.registry = []
    for conn in list(_CountingConn.registry):
        conn.events = []

    embed_calls = {"n": 0}

    async def fake_embed(text, task_id=None, context=None):
        embed_calls["n"] += 1
        return [0.5, 0.5]

    monkeypatch.setattr(episodic, "_embed_text", fake_embed)
    result = await episodic.backfill_episodic_embeddings(limit=3)

    # Semantics preserved: the whole store was checked, exactly the
    # limited number attempted, empty-summary counting intact.
    assert result["checked"] == 30
    assert result["backfilled"] == 3
    assert embed_calls["n"] == 3
    # The defect: outcome text (the heavy column) was loaded for all
    # 30 episodes even though only 3 could ever be attempted.
    outcome_rows = _rows_materialised("outcome")
    assert outcome_rows <= 3, f"outcome text loaded for {outcome_rows} rows"


@pytest.mark.asyncio
async def test_semantic_leg_does_not_fetchall_embeddings_table(
        counting_episodic, monkeypatch):
    from aether_engine.embedding_codec import encode_embedding

    model = episodic.resolve_embedder()
    episodic.init_episodic_db()
    episodic.init_embeddings_db()
    with episodic._get_conn(episodic.EMBEDDINGS_DB_PATH) as conn:
        for i in range(25):
            conn.execute(
                "INSERT INTO episodic_embeddings"
                " (task_id, embedding, model, dimension) VALUES (?, ?, ?, ?)",
                (f"t-{i}", encode_embedding([0.5, 0.5]), model, 2))
        conn.commit()
    for i in range(25):
        _insert_episode_row(f"t-{i}", f"alpha summary {i}", "outcome")
    _CountingConn.registry = []
    for conn in list(_CountingConn.registry):
        conn.events = []

    async def fake_embed(text, task_id=None, context=None):
        return [0.5, 0.5]

    monkeypatch.setattr(episodic, "_embed_text", fake_embed)
    results = await episodic.query_episodes("alpha", backfill_limit=0)
    assert results, "semantic/BM25 recall should still find the episodes"
    fetched = _rows_materialised("episodic_embeddings", kinds=("fetchall",))
    assert fetched == 0, f"semantic leg fetchall()ed {fetched} embedding rows"


# ---------------------------------------------------------------------------
# N-36 — session search LIKE wildcards
# ---------------------------------------------------------------------------

@pytest.fixture
def sessions_db(tmp_path, monkeypatch):
    db = tmp_path / "sessions.db"
    monkeypatch.setattr(session_store, "DB_PATH", db)
    session_store.init_db()
    return db


def test_session_search_escapes_like_wildcards(sessions_db):
    pct = session_store.create_session(title="100% coverage plan")
    many = session_store.create_session(title="1000 items list")
    session_store.create_session(title="unrelated notes")

    hits = session_store.search_sessions("%")
    assert [h["id"] for h in hits] == [pct], hits

    hits = session_store.search_sessions("100%")
    assert [h["id"] for h in hits] == [pct], hits

    hits = session_store.search_sessions("100_")
    assert hits == [], hits

    # Ordinary substring search still works.
    hits = session_store.search_sessions("1000")
    assert [h["id"] for h in hits] == [many], hits
