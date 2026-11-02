"""E4-F02: pruning must never erase an entity's last record.

``prune_facts`` protected only entities that had exactly one fact
BEFORE any deletion: it snapshotted those entity ids once, and its
delete helper consulted only that snapshot. An entity that started
with two or more facts could therefore lose every one of them to the
age loop or the count loop — after which the entity cleanup removed
the entity itself, and the graph forgot the entity ever existed.

The guard the store promises is about the entity's LAST record: an
entity's most current fact (status first — an active row beats a
superseded one, mirroring dedupe's survivor rule — then newest
timestamp, then highest confidence, then lowest id) survives every
prune, so the graph always retains the most recent thing it knew
about everything it has ever learned.

These tests seed rows directly (the test_phase10_3 recipe — pruning
is a store-level pass, and direct SQL gives deterministic timestamps
with no LLM in the loop) and drive the real ``prune_facts``.
"""
import time
import uuid
from unittest.mock import patch

import pytest

import aether_engine.memory.knowledge_graph as kg_mod
from aether_engine.memory.knowledge_graph import get_all_facts, prune_facts

DAY = 86400
NOW = int(time.time())

RESULT_KEYS = {
    "pruned_facts", "kept_referenced", "pruned_entities",
    "retention_days", "max_facts",
}


@pytest.fixture
def isolated_kg(tmp_path):
    """Patch the KG DB path + audit logger onto tmp files (never the
    real ~/.aether/memory.db) — the E4-F01 suite's recipe."""
    test_db = tmp_path / "kg_e4f02.db"
    with (
        patch("aether_engine.memory.knowledge_graph.DB_PATH", test_db),
        patch("aether_engine.memory.knowledge_graph._audit_logger.log_event"),
    ):
        yield test_db


def _insert_fact(entity_name, fact_text, age_days, status="active",
                 confidence=0.9, superseded_by=None):
    kg_mod.init_kg_db()
    ts = NOW - int(age_days * DAY)
    with kg_mod._get_conn() as conn:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT id FROM entities WHERE name = ?", (entity_name,)
        ).fetchone()
        if row:
            entity_id = row[0]
        else:
            entity_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO entities (id, name, entity_type, first_seen,"
                " last_updated) VALUES (?, ?, ?, ?, ?)",
                (entity_id, entity_name, "Concept", ts, ts),
            )
        fact_id = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO facts (id, entity_id, fact_text, source_task_id,"
            " timestamp, confidence, status, superseded_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (fact_id, entity_id, fact_text, "task-x", ts, confidence,
             status, superseded_by),
        )
        conn.commit()
    return fact_id


def _texts_by_entity():
    by_entity = {}
    for f in get_all_facts(limit=1000, include_history=True):
        by_entity.setdefault(f["entity_name"], set()).add(f["fact_text"])
    return by_entity


def _entities():
    with kg_mod._get_conn() as conn:
        return {r[0] for r in conn.execute("SELECT name FROM entities")}


def test_age_prune_spares_multi_fact_entitys_last_record(isolated_kg):
    """The audit repro: Multi starts with TWO facts, both older than
    the retention bound. Pre-fix both were deleted and the entity
    removed; the newest of them is Multi's last record and must
    survive, exactly like Solo's only fact does."""
    _insert_fact("Multi Entity", "older stale fact", age_days=400)
    _insert_fact("Multi Entity", "newer stale fact", age_days=390)
    _insert_fact("Solo Entity", "only fact, very old", age_days=400)
    _insert_fact("Fresh Entity", "recent fact", age_days=10)

    result = prune_facts(retention_days=365, max_facts=0)

    assert set(result) == RESULT_KEYS  # return shape unchanged
    by_entity = _texts_by_entity()
    assert by_entity["Multi Entity"] == {"newer stale fact"}
    assert by_entity["Solo Entity"] == {"only fact, very old"}
    assert by_entity["Fresh Entity"] == {"recent fact"}
    assert _entities() == {"Multi Entity", "Solo Entity", "Fresh Entity"}
    assert result["pruned_entities"] == 0
    # Only Multi's older fact was prunable-and-unprotected.
    assert result["pruned_facts"] == 1
    # Both survivors were prune candidates the guard actively kept.
    assert result["kept_referenced"] == 2


def test_count_prune_spares_every_entitys_last_record(isolated_kg):
    """A cap of one fact total cannot be met without erasing whole
    entities; the last-record guard wins over the cap, so each
    entity keeps its most current fact and only unprotected facts
    are pruned."""
    _insert_fact("Multi Entity", "oldest fact", age_days=30)
    _insert_fact("Multi Entity", "newest fact", age_days=20)
    _insert_fact("Other Entity", "other older fact", age_days=10)
    _insert_fact("Other Entity", "other newest fact", age_days=5)

    result = prune_facts(retention_days=0, max_facts=1)

    assert set(result) == RESULT_KEYS
    by_entity = _texts_by_entity()
    assert by_entity["Multi Entity"] == {"newest fact"}
    assert by_entity["Other Entity"] == {"other newest fact"}
    assert _entities() == {"Multi Entity", "Other Entity"}
    assert result["pruned_entities"] == 0
    assert result["pruned_facts"] == 2


def test_last_record_prefers_active_over_newer_superseded(isolated_kg):
    """Currency is status first: an ACTIVE fact is the entity's
    current record even when a superseded row carries a newer
    timestamp — the graph's latest belief is the live one, not the
    most recently discarded one."""
    _insert_fact("Chain Entity", "current belief", age_days=400,
                 status="active")
    _insert_fact("Chain Entity", "discarded belief", age_days=380,
                 status="superseded")

    result = prune_facts(retention_days=365, max_facts=0)

    by_entity = _texts_by_entity()
    assert by_entity["Chain Entity"] == {"current belief"}
    assert _entities() == {"Chain Entity"}
    assert result["pruned_facts"] == 1


def test_prune_still_removes_prunable_facts_with_newer_survivors(isolated_kg):
    """Control (passes before and after): when an entity's current
    record is fresh, its older facts prune exactly as before — the
    guard protects one record per entity, not the whole history."""
    _insert_fact("Rich Entity", "old fact one", age_days=400)
    _insert_fact("Rich Entity", "old fact two", age_days=390)
    _insert_fact("Rich Entity", "fresh current fact", age_days=10)

    result = prune_facts(retention_days=365, max_facts=0)

    assert set(result) == RESULT_KEYS
    assert _texts_by_entity()["Rich Entity"] == {"fresh current fact"}
    assert _entities() == {"Rich Entity"}
    assert result["pruned_facts"] == 2
    assert result["kept_referenced"] == 0
