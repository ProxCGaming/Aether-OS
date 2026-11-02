"""E4-F01: process-reopen dedupe must not delete a renewed current fact.

The reopen migration (``dedupe_facts``, run once per process per DB
from ``init_kg_db``) grouped ALL fact rows by (entity, text) — no
status filter — and kept the highest-confidence copy, ties broken by
EARLIEST timestamp. Re-asserting a fact whose earlier copy was
superseded (Kolkata -> Mumbai -> Kolkata) therefore deleted the
renewed ACTIVE row on the next process start, kept the superseded
original, and left the intervening fact's ``superseded_by`` pointing
at the deleted ID: 1 active / 3 total became 0 active / 2 total.

These tests drive the real native seam (extract + reconcile through
``extract_and_store_facts``, then a simulated process reopen: the
process-local dedupe guard is cleared and ``init_kg_db`` runs again,
which is exactly what a fresh process does on first KG use).
"""
import json
from unittest.mock import patch

import pytest

import aether_engine.memory.knowledge_graph as kg_mod
from aether_engine.memory.knowledge_graph import (
    dedupe_facts,
    extract_and_store_facts,
    get_all_facts,
)


class _Chunk:
    def __init__(self, text):
        self.text = text


class _CityProvider:
    """Extraction + reconciliation stub with EQUAL confidence for both
    cities, so the dedupe survivor is decided by status / timestamp,
    never by a confidence gap. Reconciliation calls (their prompt
    lists OLD facts) get the single (old 0, new 0) pair; each learning
    step below has exactly one old active fact and one new fact."""

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        blob = json.dumps(messages)
        if "OLD facts:" in blob:
            yield _Chunk('[{"old": 0, "new": 0}]')
        elif "Mumbai" in blob:
            yield _Chunk(json.dumps([{
                "entity_name": "User", "entity_type": "Person",
                "fact": "User lives in Mumbai", "confidence": 0.9,
            }]))
        else:
            yield _Chunk(json.dumps([{
                "entity_name": "User", "entity_type": "Person",
                "fact": "User lives in Kolkata", "confidence": 0.9,
            }]))


@pytest.fixture
def isolated_kg(tmp_path):
    """Patch the KG DB path + audit logger onto tmp files (never the
    real ~/.aether/memory.db) — the test_phase10_3_kg_dedup recipe."""
    test_db = tmp_path / "kg_e4f01.db"
    with (
        patch("aether_engine.memory.knowledge_graph.DB_PATH", test_db),
        patch("aether_engine.memory.knowledge_graph._audit_logger.log_event") as mock_log,
    ):
        yield test_db, mock_log


def _pin_timestamp(fact_id, timestamp):
    """Give one fact an exact timestamp. Extraction stamps facts with
    the wall clock, so steps run inside one second would tie; the
    audit scenario is defined by ordered timestamps, pinned here the
    same way the audit's checks pinned theirs."""
    with kg_mod._get_conn() as conn:
        conn.execute(
            "UPDATE facts SET timestamp = ? WHERE id = ?", (timestamp, fact_id)
        )
        conn.commit()


def _reopen():
    """Simulate a fresh process opening the same DB file: the dedupe
    guard is process-local, so a new process starts with it empty and
    init_kg_db() runs the migration again."""
    kg_mod._DEDUPED_DB_PATHS.discard(str(kg_mod.DB_PATH))
    kg_mod.init_kg_db()


def _by_text(rows):
    return {r["fact_text"]: r for r in rows}


async def _learn_kolkata_mumbai_kolkata():
    """Kolkata -> Mumbai -> Kolkata through the store's own path:
    re-asserting Kolkata inserts a NEW active row (insert-time dedupe
    only matches active rows) and reconciliation supersedes Mumbai
    with it. Returns (k1_id, m1_id, k2_id)."""
    provider = _CityProvider()
    await extract_and_store_facts(
        provider, "I live in Kolkata", "Noted!", "t1")
    k1 = get_all_facts(include_history=True)[0]
    _pin_timestamp(k1["fact_id"], 1000)

    await extract_and_store_facts(
        provider, "I moved to Mumbai last week", "Congrats!", "t2")
    m1 = next(r for r in get_all_facts(include_history=True)
              if r["fact_text"] == "User lives in Mumbai")
    _pin_timestamp(m1["fact_id"], 2000)

    await extract_and_store_facts(
        provider, "I moved back to Kolkata", "Welcome back!", "t3")
    active = get_all_facts()
    assert [r["fact_text"] for r in active] == ["User lives in Kolkata"]
    k2 = active[0]
    _pin_timestamp(k2["fact_id"], 3000)
    return k1["fact_id"], m1["fact_id"], k2["fact_id"]


@pytest.mark.asyncio
async def test_reopen_keeps_renewed_current_fact(isolated_kg):
    """The audit repro: before reopen 1 active / 3 total; after
    reopen the renewed Kolkata row must still be the active fact,
    the superseded original is the duplicate that gets collapsed,
    and Mumbai's superseded_by still points at a live row."""
    _ = isolated_kg
    k1_id, _, k2_id = await _learn_kolkata_mumbai_kolkata()

    history = get_all_facts(include_history=True)
    assert len(history) == 3
    assert len(get_all_facts()) == 1

    _reopen()

    active = get_all_facts()
    assert [r["fact_text"] for r in active] == ["User lives in Kolkata"]
    assert active[0]["fact_id"] == k2_id

    history = get_all_facts(include_history=True)
    assert len(history) == 2
    rows = _by_text(history)
    # The renewed row survived; the superseded original was collapsed.
    assert k1_id not in {r["fact_id"] for r in history}
    mumbai = rows["User lives in Mumbai"]
    assert mumbai["status"] == "superseded"
    assert mumbai["superseded_by"] == k2_id

    # Retrieval presents the renewed fact as current.
    assert [r["fact_text"] for r in kg_mod.search_facts("Kolkata")] == [
        "User lives in Kolkata"
    ]

    # Idempotent: a second reopen changes nothing further.
    _reopen()
    assert get_all_facts(include_history=True) == history
    assert dedupe_facts() == {"fact_groups_deduped": 0, "facts_removed": 0}


@pytest.mark.asyncio
async def test_reopen_repairs_superseded_by_chain(isolated_kg):
    """Flip-flop Kolkata -> Mumbai -> Kolkata -> Mumbai leaves two
    same-text superseded pairs. Collapsing them must re-point every
    superseded_by that referenced a deleted copy at the surviving
    copy of that same fact — no pointer may dangle at a deleted ID."""
    _ = isolated_kg
    provider = _CityProvider()
    steps = [
        ("I live in Kolkata", 1000),
        ("I moved to Mumbai last week", 2000),
        ("I moved back to Kolkata", 3000),
        ("I moved to Mumbai again", 4000),
    ]
    for i, (prompt, ts) in enumerate(steps):
        await extract_and_store_facts(provider, prompt, "Noted!", f"t{i + 1}")
        newest = max(get_all_facts(include_history=True),
                     key=lambda r: r["timestamp"])
        _pin_timestamp(newest["fact_id"], ts)

    history = get_all_facts(include_history=True)
    assert len(history) == 4
    assert len(get_all_facts()) == 1

    _reopen()

    history = get_all_facts(include_history=True)
    live_ids = {r["fact_id"] for r in history}
    assert len(get_all_facts()) == 1
    assert get_all_facts()[0]["fact_text"] == "User lives in Mumbai"
    for row in history:
        if row["superseded_by"] is not None:
            assert row["superseded_by"] in live_ids, row
            assert row["superseded_by"] != row["fact_id"], row


@pytest.mark.asyncio
async def test_reopen_without_duplicates_changes_nothing(isolated_kg):
    """Control: a graph with no duplicate (entity, text) groups is
    byte-identical after a reopen — statuses, links and rows all
    untouched."""
    _ = isolated_kg
    provider = _CityProvider()
    await extract_and_store_facts(
        provider, "I live in Kolkata", "Noted!", "t1")
    await extract_and_store_facts(
        provider, "I moved to Mumbai last week", "Congrats!", "t2")

    before = get_all_facts(include_history=True)
    assert len(before) == 2

    _reopen()

    assert get_all_facts(include_history=True) == before
    assert dedupe_facts() == {"fact_groups_deduped": 0, "facts_removed": 0}


def test_active_copy_survives_higher_confidence_superseded_copy(isolated_kg):
    """Status decides before confidence: a renewed ACTIVE copy wins
    over a superseded copy of the same text even when the old copy
    carries the higher confidence — current truth is never deleted
    for an outdated belief's score."""
    import uuid

    kg_mod.init_kg_db()
    with kg_mod._get_conn() as conn:
        entity_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO entities (id, name, entity_type, first_seen,"
            " last_updated) VALUES (?, ?, ?, ?, ?)",
            (entity_id, "User", "Person", 1000, 1000),
        )
        conn.execute(
            "INSERT INTO facts (id, entity_id, fact_text, source_task_id,"
            " timestamp, confidence, status, superseded_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("old-superseded", entity_id, "User lives in Kolkata", "t1",
             1000, 0.99, "superseded", "some-mumbai-id"),
        )
        conn.execute(
            "INSERT INTO facts (id, entity_id, fact_text, source_task_id,"
            " timestamp, confidence, status, superseded_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("renewed-active", entity_id, "User lives in Kolkata", "t3",
             3000, 0.5, "active", None),
        )
        conn.commit()

    result = dedupe_facts()

    assert result == {"fact_groups_deduped": 1, "facts_removed": 1}
    active = get_all_facts()
    assert [r["fact_id"] for r in active] == ["renewed-active"]
