"""S5: unified memory scoring + single context assembler.

Pins the S5 contract from the master-build plan (§4 S5, R11/R18):

* the KG schema migration is additive and upgrade-safe — a
  pre-S5 memory.db (built here with the OLD v2 schema + rows, and,
  when present, a copy of the real ~/.aether/memory.db taken
  READ-ONLY) keeps every row and gains provenance 'inferred' /
  importance 0.5 defaults;
* extraction records provenance (stated only on an explicit
  extractor signal) and importance (explicit value, else the
  documented coarse heuristic);
* the context assembler scores KG + episodic + curated candidates
  with relevance x recency x confidence x importance under ONE
  token budget, and every item carries its source id;
* decay touches INFERRED facts only, demote-before-delete, in the
  weekly prune_all pass;
* the curated consolidation document is consumed by the assembler.

All stores are tmp files; nothing touches the real ~/.aether DBs
(the one real-file test copies it and migrates only the copy).
"""
import json
import shutil
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from aether_engine.config import UserConfig
from aether_engine.memory import knowledge_graph as kg
from aether_engine.memory.facade import NativeMemoryBackend

NOW = int(time.time())
DAY = 86400


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

@pytest.fixture()
def kg_db(tmp_path):
    db = tmp_path / "kg.db"
    with patch.object(kg, "DB_PATH", db), \
            patch.object(kg, "_audit_logger") as audit:
        audit.log_event = lambda *a, **k: None
        yield db


@pytest.fixture()
def epi_db(tmp_path):
    from aether_engine.memory import episodic

    mem_db = tmp_path / "episodes.db"
    emb_db = tmp_path / "embeddings.db"
    orig_get_conn = episodic._get_conn

    def fake_get_conn(db_path=None):
        if db_path is None or str(db_path) == str(episodic.DB_PATH):
            return orig_get_conn(mem_db)
        if str(db_path) == str(episodic.EMBEDDINGS_DB_PATH):
            return orig_get_conn(emb_db)
        return orig_get_conn(db_path)

    with patch.object(episodic, "_get_conn", fake_get_conn), \
            patch.object(episodic, "DB_PATH", mem_db), \
            patch.object(episodic, "EMBEDDINGS_DB_PATH", emb_db):
        yield mem_db


@pytest.fixture()
def curated_path(tmp_path):
    """The curated document lives at a tmp path (absent unless a
    test writes it) so assembly never reads the real document."""
    path = tmp_path / "curated_memory.md"
    with patch("aether_engine.memory.consolidation.MEMORY_DOCUMENT_PATH", path):
        yield path


def _insert_fact(db_path, fid, entity_id, entity_name, text, *, ts, conf,
                 provenance=None, importance=None):
    """Insert one entity + fact directly. Column list adapts to
    whether the caller wants the S5 fields set explicitly."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO entities (id, name, entity_type, first_seen, last_updated) "
        "VALUES (?, ?, 'Concept', ?, ?)", (entity_id, entity_name, ts, ts))
    if provenance is None:
        conn.execute(
            "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, confidence) "
            "VALUES (?, ?, ?, 'seed', ?, ?)", (fid, entity_id, text, ts, conf))
    else:
        conn.execute(
            "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, confidence, provenance, importance) "
            "VALUES (?, ?, ?, 'seed', ?, ?, ?, ?)",
            (fid, entity_id, text, ts, conf, provenance, importance))
    conn.commit()
    conn.close()


def _fact_row(db_path, fid):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM facts WHERE id = ?", (fid,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ----------------------------------------------------------------------
# Migration / upgrade
# ----------------------------------------------------------------------

_OLD_SCHEMA = """
CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    entity_type TEXT,
    first_seen INTEGER NOT NULL,
    last_updated INTEGER NOT NULL
);
CREATE TABLE facts (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    fact_text TEXT NOT NULL,
    source_task_id TEXT,
    timestamp INTEGER NOT NULL,
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'active',
    superseded_by TEXT,
    FOREIGN KEY(entity_id) REFERENCES entities(id) ON DELETE CASCADE
);
"""


def test_upgrade_migration_on_pre_s5_schema(tmp_path):
    """A v2-shaped memory.db gains the S5 columns with the
    documented defaults and loses nothing."""
    db = tmp_path / "old_memory.db"
    conn = sqlite3.connect(db)
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO entities VALUES ('e1', 'User', 'Person', ?, ?)", (NOW, NOW))
    conn.execute(
        "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, confidence) "
        "VALUES ('f1', 'e1', 'User lives in Kolkata', 't1', ?, 0.9)", (NOW - DAY,))
    conn.execute(
        "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, confidence, status, superseded_by) "
        "VALUES ('f2', 'e1', 'User lived in Delhi', 't0', ?, 0.8, 'superseded', 'f1')", (NOW - 30 * DAY,))
    conn.commit()
    conn.close()

    with patch.object(kg, "DB_PATH", db), \
            patch.object(kg, "_audit_logger") as audit:
        audit.log_event = lambda *a, **k: None
        kg.init_kg_db()

        conn = sqlite3.connect(db)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(facts)")}
        assert {"provenance", "importance", "last_decayed_at"} <= columns
        version = conn.execute(
            "SELECT version FROM aether_schema_meta WHERE store = 'knowledge_graph'"
        ).fetchone()[0]
        conn.close()
        assert version == 3

        facts = {f["fact_id"]: f for f in kg.get_all_facts(limit=10, include_history=True)}
    assert set(facts) == {"f1", "f2"}
    # Documented defaults: provenance is NOT assumed 'stated'
    # retroactively; importance is the neutral 0.5.
    assert facts["f1"]["provenance"] == "inferred"
    assert facts["f1"]["importance"] == 0.5
    assert facts["f2"]["provenance"] == "inferred"
    assert facts["f1"]["fact_text"] == "User lives in Kolkata"
    assert facts["f2"]["status"] == "superseded"


def test_upgrade_migration_on_copy_of_real_db(tmp_path):
    """The same migration run against a copy of the owner's real
    memory.db (copied read-only — the live file is never opened
    for writing): row counts survive and the columns land."""
    real = Path.home() / ".aether" / "memory.db"
    if not real.exists():
        pytest.skip("no real ~/.aether/memory.db on this machine")
    copy = tmp_path / "real_copy.db"
    shutil.copyfile(real, copy)

    def _fact_count(path):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "facts" not in tables:
                return None
            return conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        finally:
            conn.close()

    before = _fact_count(copy)
    if before is None:
        pytest.skip("real memory.db has no facts table")
    # The one-time dedupe migration is not what this test proves —
    # pre-mark this copy as deduped so only the S5 schema migration
    # runs against it and the row count is a pure migration signal.
    kg._DEDUPED_DB_PATHS.add(str(copy))
    with patch.object(kg, "DB_PATH", copy), \
            patch.object(kg, "_audit_logger") as audit:
        audit.log_event = lambda *a, **k: None
        kg.init_kg_db()
    assert _fact_count(copy) == before
    conn = sqlite3.connect(copy)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(facts)")}
    conn.close()
    assert {"provenance", "importance", "last_decayed_at"} <= columns


# ----------------------------------------------------------------------
# Extraction provenance + importance
# ----------------------------------------------------------------------

class _JsonProvider:
    """Extraction test double: streams one fixed JSON payload for
    every call (extraction + the reconcile pass, whose parser
    ignores payloads without old/new pairs)."""

    def __init__(self, payload):
        self._text = json.dumps(payload)

    async def call_stream(self, messages):
        class _Chunk:
            def __init__(self, text):
                self.text = text

        yield _Chunk(self._text)


@pytest.mark.asyncio
async def test_extraction_records_provenance_and_importance(kg_db):
    provider = _JsonProvider([
        {"entity_name": "User", "entity_type": "Person",
         "fact": "User is called Arindam", "confidence": 0.95,
         "provenance": "stated", "importance": 0.9},
        {"entity_name": "Project", "entity_type": "Project",
         "fact": "Project uses SQLite", "confidence": 0.7,
         "provenance": "inferred"},
        {"entity_name": "User", "entity_type": "Person",
         "fact": "User prefers dark themes", "confidence": 0.85,
         "provenance": "stated"},
    ])
    await kg.extract_and_store_facts(
        provider, "I am Arindam and I prefer dark themes",
        "Noted.", "task-s5-prov")

    facts = {f["fact_text"]: f for f in kg.get_all_facts(limit=10)}
    stated = facts["User is called Arindam"]
    assert stated["provenance"] == "stated"
    assert stated["importance"] == 0.9  # the extractor's own signal wins

    inferred = facts["Project uses SQLite"]
    assert inferred["provenance"] == "inferred"
    assert inferred["importance"] == 0.4  # documented heuristic tier

    heuristic = facts["User prefers dark themes"]
    assert heuristic["provenance"] == "stated"
    # Stated + Person/Preference tier of the documented heuristic.
    assert heuristic["importance"] == 0.8


@pytest.mark.asyncio
async def test_extraction_without_signals_defaults_to_inferred(kg_db):
    """A pre-S5-shaped extractor output (no provenance/importance
    fields at all) must not gain the stated privilege by default."""
    provider = _JsonProvider([
        {"entity_name": "Test User", "entity_type": "Person",
         "fact": "Likes Python", "confidence": 0.9},
    ])
    await kg.extract_and_store_facts(
        provider, "I really like coding in Python.", "Great!", "task-s5-old")

    facts = kg.get_all_facts(limit=10)
    assert len(facts) == 1
    assert facts[0]["provenance"] == "inferred"
    assert facts[0]["importance"] == 0.4


# ----------------------------------------------------------------------
# Assembler: scoring order, traceability, budget
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_assembler_scoring_order_budget_and_ids(kg_db, epi_db, curated_path):
    from aether_engine.memory import episodic
    from aether_engine.memory.context_assembler import (
        assemble_memory_context,
        estimate_tokens,
    )

    kg.init_kg_db()
    _insert_fact(kg_db, "f-stated", "e1", "User",
                 "User codes in Python daily", ts=NOW, conf=1.0,
                 provenance="stated", importance=0.9)
    _insert_fact(kg_db, "f-inferred", "e1", "User",
                 "User codes in Python sometimes", ts=NOW - 180 * DAY,
                 conf=0.5, provenance="inferred", importance=0.2)
    await episodic.store_episode(
        "ep-py", "Write Python codes for the report",
        "Python codes written", ["task"], "The Python codes ran fine.")

    backend = NativeMemoryBackend()
    result = await assemble_memory_context(
        backend, "Python codes", config=UserConfig(), now=NOW)

    assert result["items"], "expected scored candidates"
    # The high-importance recent stated fact beats the
    # low-importance stale inferred one for the same query.
    assert result["items"][0]["source"] == "fact"
    assert result["items"][0]["id"] == "f-stated"
    scores = [item["score"] for item in result["items"]]
    assert scores == sorted(scores, reverse=True)
    # Every recall is traceable to a stored record.
    assert all(item["id"] for item in result["items"])
    assert "f-stated" in result["text"]
    assert "ep-py" in result["text"]
    # One total budget, respected by the assembler's own estimate.
    assert result["estimated_tokens"] <= result["budget_tokens"]
    assert estimate_tokens(result["text"]) <= result["budget_tokens"]

    tight = await assemble_memory_context(
        backend, "Python codes", config=UserConfig(),
        budget_tokens=40, now=NOW)
    assert tight["text"]
    assert estimate_tokens(tight["text"]) <= 40
    assert len(tight["items"]) < len(result["items"])


# ----------------------------------------------------------------------
# Decay: inferred only, demote-before-delete, weekly seam
# ----------------------------------------------------------------------

def test_decay_applies_to_inferred_not_stated(kg_db):
    kg.init_kg_db()
    # Stated, 90 days old: exempt however old.
    _insert_fact(kg_db, "f-stated", "e1", "User", "User stated fact",
                 ts=NOW - 90 * DAY, conf=0.8,
                 provenance="stated", importance=0.8)
    # Project's most current fact is recent + stated, so the old
    # inferred one is NOT protected by the most-current guard.
    _insert_fact(kg_db, "f-proj-current", "e2", "Project",
                 "Project current direction", ts=NOW, conf=1.0,
                 provenance="stated", importance=0.8)
    _insert_fact(kg_db, "f-proj-old", "e2", "Project",
                 "Project old inferred detail", ts=NOW - 90 * DAY,
                 conf=0.8, provenance="inferred", importance=0.4)
    # Topic: old inferred at 0.3 decays (90d = two 45d half-lives)
    # to 0.075 — below the 0.1 floor — and is not most current.
    _insert_fact(kg_db, "f-topic-current", "e3", "Topic",
                 "Topic current fact", ts=NOW, conf=1.0,
                 provenance="stated", importance=0.8)
    _insert_fact(kg_db, "f-topic-old", "e3", "Topic",
                 "Topic stale inference", ts=NOW - 90 * DAY, conf=0.3,
                 provenance="inferred", importance=0.4)
    # Solo: an entity's ONLY fact — inferred and below floor after
    # decay, but the most-current guard keeps it (demoted, not
    # deleted).
    _insert_fact(kg_db, "f-solo", "e4", "Solo", "Solo only inference",
                 ts=NOW - 90 * DAY, conf=0.3,
                 provenance="inferred", importance=0.4)

    result = kg.decay_inferred_facts(config=UserConfig(), now=NOW)
    assert result["removed_below_floor"] == 1
    assert result["kept_referenced"] == 1

    assert _fact_row(kg_db, "f-stated")["confidence"] == 0.8
    assert _fact_row(kg_db, "f-proj-current")["confidence"] == 1.0
    decayed = _fact_row(kg_db, "f-proj-old")
    assert decayed["confidence"] == pytest.approx(0.2)  # 0.8 * 0.5 ** 2
    assert _fact_row(kg_db, "f-topic-old") is None
    solo = _fact_row(kg_db, "f-solo")
    assert solo is not None
    assert solo["confidence"] == pytest.approx(0.075)

    # The anchor makes a second pass at the same `now` a no-op —
    # decay is a function of age, not of how often the pass ran.
    again = kg.decay_inferred_facts(config=UserConfig(), now=NOW)
    assert again["decayed"] == 0
    assert _fact_row(kg_db, "f-proj-old")["confidence"] == pytest.approx(0.2)


def test_decay_runs_in_weekly_prune_all(kg_db, epi_db, curated_path, tmp_path):
    from aether_engine.memory import pruning, session_store

    kg.init_kg_db()
    _insert_fact(kg_db, "f-old-inf", "e1", "User", "User old inference",
                 ts=NOW - 90 * DAY, conf=0.8,
                 provenance="inferred", importance=0.4)
    _insert_fact(kg_db, "f-stated", "e1", "User", "User stated fact",
                 ts=NOW - 90 * DAY, conf=0.8,
                 provenance="stated", importance=0.8)

    session_store._initialized = False
    cfg = UserConfig(
        episodic_retention_days=0, episodic_max_episodes=0,
        kg_fact_retention_days=0, kg_max_facts=0,
        session_retention_days=0, session_max_sessions=0)
    with patch.object(session_store, "DB_PATH", kg_db), \
            patch.object(pruning, "_audit_logger") as audit:
        audit.log_event = lambda *a, **k: None
        results = pruning.prune_all(config=cfg)

    assert "decay" in results
    assert results["decay"]["examined_inferred"] >= 1
    # prune_all decays against the real clock, a few seconds past
    # this test's fixed NOW — hence the looser tolerance here (the
    # exact-arithmetic pin lives in the direct decay test above).
    assert _fact_row(kg_db, "f-old-inf")["confidence"] == pytest.approx(0.2, rel=1e-3)
    assert _fact_row(kg_db, "f-stated")["confidence"] == 0.8


# ----------------------------------------------------------------------
# Consolidation consumed by the assembler
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_curated_document_is_consumed_within_budget(kg_db, epi_db, curated_path):
    from aether_engine.memory.context_assembler import (
        assemble_memory_context,
        estimate_tokens,
    )

    kg.init_kg_db()
    _insert_fact(kg_db, "f-telescope", "e1", "User",
                 "User maintains a violet telescope", ts=NOW, conf=0.9,
                 provenance="stated", importance=0.7)
    curated_path.write_text(
        "# Aether memory document\n\n"
        "Curated from the episodic and knowledge-graph stores.\n\n"
        "## What Aether knows (2 facts)\n\n"
        "### User (Person)\n"
        "- User maintains a violet telescope (confidence 0.90)\n"
        "- User collects nebula photographs (confidence 0.80)\n\n"
        "## Recent tasks (0 most recent)\n\n"
        "_No episodes stored yet._\n",
        encoding="utf-8")

    backend = NativeMemoryBackend()
    result = await assemble_memory_context(
        backend, "nebula photographs violet telescope",
        config=UserConfig(), now=NOW)

    sources = {(item["source"], item["id"]) for item in result["items"]}
    # The curated-only line is recalled, traceable to its document line.
    assert any(source == "curated" for source, _ in sources)
    assert "nebula photographs" in result["text"]
    assert "curated_memory.md:" in result["text"]
    # The line that duplicates the live fact is NOT repeated as a
    # curated candidate — the live record is cited instead.
    assert ("fact", "f-telescope") in sources
    assert result["text"].count("violet telescope") == 1
    assert estimate_tokens(result["text"]) <= result["budget_tokens"]
