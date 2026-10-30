"""Phase 10.3 A3: KG facts dedupe + confidence-weighted ranking.

Regression tests for the audit finding in critical/02 #5: KG facts were
never deduped (``extract_and_store_facts`` did an unconditional INSERT)
and the confidence recorded at extraction was ignored in retrieval
ranking (every retrieval ordered by timestamp DESC only). Each test
failed before the fix and passes after.
"""
import json
import sqlite3
import uuid

import pytest
from unittest.mock import patch

import aether_engine.memory.knowledge_graph as kg_mod
from aether_engine.memory.knowledge_graph import (
    dedupe_facts,
    extract_and_store_facts,
    get_all_facts,
    get_entity_facts,
    search_facts,
)


class _Chunk:
    def __init__(self, text):
        self.text = text


class _FakeProvider:
    """Provider whose extraction call returns the given facts as JSON."""

    def __init__(self, facts):
        self._payload = json.dumps(facts)

    async def call_stream(self, messages):
        yield _Chunk(self._payload)


@pytest.fixture
def isolated_kg(tmp_path):
    """Patch the KG DB path + audit logger onto tmp files (never the
    real ~/.aether/memory.db)."""
    test_db = tmp_path / "kg_dedup.db"
    with patch("aether_engine.memory.knowledge_graph.DB_PATH", test_db):
        with patch(
            "aether_engine.memory.knowledge_graph._audit_logger.log_event"
        ) as mock_log:
            yield test_db, mock_log


def _audit_calls(mock_log, event):
    return [c for c in mock_log.call_args_list if c.args and c.args[0] == event]


def _seed_fact(entity_name, fact_text, confidence, timestamp):
    """Insert one fact row directly, bypassing extract_and_store_facts,
    so confidence / timestamp are exactly controlled."""
    with kg_mod._get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM entities WHERE name = ?", (entity_name,)
        ).fetchone()
        if row:
            entity_id = row[0]
        else:
            entity_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO entities (id, name, entity_type, first_seen,"
                " last_updated) VALUES (?, ?, ?, ?, ?)",
                (entity_id, entity_name, "Concept", timestamp, timestamp),
            )
        fact_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO facts (id, entity_id, fact_text, source_task_id,"
            " timestamp, confidence) VALUES (?, ?, ?, ?, ?, ?)",
            (fact_id, entity_id, fact_text, "task-seed", timestamp, confidence),
        )
        conn.commit()
    return fact_id


def _entity_names():
    with kg_mod._get_conn() as conn:
        return {r[0] for r in conn.execute("SELECT name FROM entities")}


# --- insert-time dedupe ------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_dedupes_same_fact_case_insensitively(isolated_kg):
    """Same (entity, content) with different casing / padding stores one
    row; the later copies are skipped and logged, not re-inserted."""
    _, mock_log = isolated_kg

    await extract_and_store_facts(
        provider=_FakeProvider([{
            "entity_name": "Test User", "entity_type": "Person",
            "fact": "Likes Python", "confidence": 0.9,
        }]),
        user_prompt="I really like coding in Python.",
        assistant_response="Python is very versatile.",
        task_id="task-1",
    )
    # Same fact, entity + content re-cased.
    await extract_and_store_facts(
        provider=_FakeProvider([{
            "entity_name": "test user", "entity_type": "Person",
            "fact": "likes python", "confidence": 0.8,
        }]),
        user_prompt="I really like coding in Python.",
        assistant_response="Python is very versatile.",
        task_id="task-2",
    )
    # Same fact again, padded with whitespace.
    await extract_and_store_facts(
        provider=_FakeProvider([{
            "entity_name": "Test User", "entity_type": "Person",
            "fact": "  Likes Python  ", "confidence": 0.95,
        }]),
        user_prompt="I really like coding in Python.",
        assistant_response="Python is very versatile.",
        task_id="task-3",
    )

    facts = get_all_facts(limit=10)
    assert len(facts) == 1
    # The first copy survives, untouched (no confidence bump on dup).
    assert facts[0]["fact_text"] == "Likes Python"
    assert facts[0]["confidence"] == 0.9
    # The skipped duplicate must not have left a fact-less "test user"
    # entity behind (dedupe runs before the entity upsert).
    assert _entity_names() == {"Test User"}

    assert len(_audit_calls(mock_log, "KNOWLEDGE_FACT_STORED")) == 1
    skipped = _audit_calls(mock_log, "KNOWLEDGE_FACT_DUPLICATE_SKIPPED")
    assert len(skipped) == 2


@pytest.mark.asyncio
async def test_extract_keeps_distinct_facts_for_same_entity(isolated_kg):
    """Dedupe must not collapse genuinely different facts."""
    _, mock_log = isolated_kg

    await extract_and_store_facts(
        provider=_FakeProvider([
            {"entity_name": "Test User", "entity_type": "Person",
             "fact": "Likes Python", "confidence": 0.9},
            {"entity_name": "Test User", "entity_type": "Person",
             "fact": "Dislikes Java", "confidence": 0.7},
        ]),
        user_prompt="I like Python but not Java.",
        assistant_response="Noted.",
        task_id="task-1",
    )

    facts = get_all_facts(limit=10)
    assert len(facts) == 2
    assert {f["fact_text"] for f in facts} == {"Likes Python", "Dislikes Java"}
    assert _audit_calls(mock_log, "KNOWLEDGE_FACT_DUPLICATE_SKIPPED") == []


# --- confidence-weighted ranking ---------------------------------------------

def test_higher_confidence_ranks_first(isolated_kg):
    """Higher-confidence facts rank above lower-confidence ones, all
    else equal — even when the high-confidence fact is older, and even
    when the low-confidence fact is newer. NULL confidence ranks last.
    Ties on confidence break by recency (timestamp DESC)."""
    kg_mod.init_kg_db()
    _seed_fact("User", "User prefers tea", 0.2, timestamp=2000)
    _seed_fact("User", "User prefers Python", 0.9, timestamp=1000)
    _seed_fact("User", "User prefers coffee", 0.9, timestamp=1100)
    _seed_fact("User", "User likes hiking", None, timestamp=3000)
    _seed_fact("Other", "Other fact about Python", 0.5, timestamp=1500)

    all_facts = get_all_facts(limit=10)
    assert [f["fact_text"] for f in all_facts] == [
        "User prefers coffee",        # 0.9, newer of the 0.9 pair
        "User prefers Python",        # 0.9, older — still above 0.5/0.2
        "Other fact about Python",    # 0.5
        "User prefers tea",           # 0.2, newer but lowest stated
        "User likes hiking",          # NULL confidence -> 0.0, last
    ]
    assert all("confidence" in f for f in all_facts)

    entity_facts = get_entity_facts("User")
    assert [f["fact_text"] for f in entity_facts] == [
        "User prefers coffee",
        "User prefers Python",
        "User prefers tea",
        "User likes hiking",
    ]

    found = search_facts("Python", limit=10)
    assert [f["fact_text"] for f in found] == [
        "User prefers Python",
        "Other fact about Python",
    ]


# --- one-time migration ------------------------------------------------------

def test_dedupe_facts_migration_collapses_existing_duplicates(isolated_kg):
    """Pre-existing duplicate rows (seeded directly, as the old
    unconditional INSERT left them) collapse to one row per dedupe key.
    The highest-confidence copy survives; the removal is logged with
    what was removed; a second run is a no-op."""
    _, mock_log = isolated_kg
    kg_mod.init_kg_db()
    _seed_fact("User", "Likes Python", 0.3, timestamp=1000)
    _seed_fact("User", "likes python", 0.9, timestamp=2000)
    _seed_fact("User", "  LIKES PYTHON ", 0.5, timestamp=1500)
    # Same fact under a case-variant entity name: same dedupe key.
    _seed_fact("user", "likes python", 0.4, timestamp=1200)
    _seed_fact("User", "User likes hiking", 0.7, timestamp=1000)
    assert len(get_all_facts(limit=10)) == 5

    result = dedupe_facts()

    assert result == {"fact_groups_deduped": 1, "facts_removed": 3}
    facts = get_all_facts(limit=10)
    assert len(facts) == 2
    by_text = {f["fact_text"]: f for f in facts}
    # Highest-confidence copy survives, with its own stored text.
    assert by_text["likes python"]["confidence"] == 0.9
    assert "User likes hiking" in by_text
    # The now-empty case-variant entity is removed with its facts.
    assert _entity_names() == {"User"}

    logged = _audit_calls(mock_log, "KNOWLEDGE_GRAPH_DEDUPED")
    assert len(logged) == 1
    assert logged[0].args[1]["facts_removed"] == 3
    assert len(logged[0].args[1]["removed"]) == 3

    # Idempotent: nothing left to collapse, nothing logged.
    assert dedupe_facts() == {"fact_groups_deduped": 0, "facts_removed": 0}
    assert len(_audit_calls(mock_log, "KNOWLEDGE_GRAPH_DEDUPED")) == 1


def test_dedupe_migration_runs_on_first_init(isolated_kg):
    """Existing deployments heal on first use: duplicates seeded before
    any init_kg_db() call on this DB file are collapsed by the
    migration that init_kg_db() runs once per process per DB file."""
    test_db, mock_log = isolated_kg
    # Seed without going through init_kg_db (which would already run
    # the migration), so the duplicates genuinely pre-date the fix.
    with kg_mod._get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                entity_type TEXT,
                first_seen INTEGER NOT NULL,
                last_updated INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                id TEXT PRIMARY KEY,
                entity_id TEXT NOT NULL,
                fact_text TEXT NOT NULL,
                source_task_id TEXT,
                timestamp INTEGER NOT NULL,
                confidence REAL,
                FOREIGN KEY(entity_id) REFERENCES entities(id) ON DELETE CASCADE
            )
        """)
        conn.commit()
    _seed_fact("User", "Likes Python", 0.3, timestamp=1000)
    _seed_fact("User", "likes python", 0.9, timestamp=2000)

    facts = get_all_facts(limit=10)

    assert len(facts) == 1
    assert facts[0]["confidence"] == 0.9
    assert len(_audit_calls(mock_log, "KNOWLEDGE_GRAPH_DEDUPED")) == 1
