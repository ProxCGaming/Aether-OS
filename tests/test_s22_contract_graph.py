"""S22 contract — graph neighbourhood read (SPEC-6B C5, per D-4).

Pins the C5 contract from SPEC-6B:

* ``graph_neighbourhood(entity_name, depth=1|2)`` →
  ``{centre: {entity_name, entity_type, fact_count,
  episode_count}, neighbours: [{entity_name, entity_type,
  cooccurrence_count, fact_count}], replaced_chains:
  [{old_entity, new_entity, fact_id}]}``.
* Edges are DERIVED AT READ TIME: co-occurrence = two entities
  appearing in the same fact/episode; replaced chains come from
  the superseded-by links. No schema change, no extracted
  relations.
* Depth 2 = neighbours of neighbours, counts recomputed against
  the centre (each entry's cooccurrence_count is its DIRECT
  count against the centre; entries carry their depth).
* Unknown entity → an honest empty result naming the entity,
  not an error.
"""
import sqlite3
import time
from unittest.mock import patch

import pytest

from aether_engine.memory import knowledge_graph as kg
from aether_engine.memory.facade import MemoryBackend, NativeMemoryBackend

NOW = int(time.time())


def _fact(fid, entity, entity_type, text, *, status="active",
          superseded_by=None):
    return {"fact_id": fid, "entity_name": entity,
            "entity_type": entity_type, "fact_text": text,
            "status": status, "superseded_by": superseded_by,
            "timestamp": NOW, "confidence": 0.9,
            "provenance": "stated", "importance": 0.8}


FACTS = [
    _fact("f-user-name", "User", "Person", "User's name is Arindam Roy"),
    _fact("f-maa", "Maa", "Person", "Maa is the user's mother"),
    _fact("f-drink", "Filter coffee", "Concept",
          "User's favourite morning drink is filter coffee"),
    _fact("f-home-old", "User", "Person",
          "User lives in the old house", status="superseded",
          superseded_by="f-home-new"),
    _fact("f-home-new", "Kolkata", "Place", "User now lives in Kolkata"),
    _fact("f-ride", "Cycling", "Concept",
          "Maa enjoys cycling in the morning"),
]

EPISODES = [
    {"task_id": "ep-1", "user_prompt": "Plan a cycling trip with Maa",
     "task_summary": "Planned a cycling route", "outcome": "Booked bikes",
     "timestamp": NOW},
    {"task_id": "ep-2", "user_prompt": "What does Maa think of Kolkata?",
     "task_summary": "", "outcome": "", "timestamp": NOW},
    {"task_id": "ep-3", "user_prompt": "Remind User about Maa's birthday",
     "task_summary": "", "outcome": "", "timestamp": NOW},
]


class _StubBackend(MemoryBackend):
    name = "native"

    def list_facts(self, limit=100, include_history=False):
        return list(FACTS)

    def list_episodes(self, limit=50):
        return list(EPISODES)


def _neighbours_by_name(result):
    return {n["entity_name"]: n for n in result["neighbours"]}


# ----------------------------------------------------------------------
# Centre + depth 1
# ----------------------------------------------------------------------

def test_centre_counts():
    result = _StubBackend().graph_neighbourhood("User")
    centre = result["centre"]
    assert centre["entity_name"] == "User"
    assert centre["entity_type"] == "Person"
    # Active facts owned by User: f-user-name only (f-home-old is
    # superseded history and does not count as a live fact).
    assert centre["fact_count"] == 1
    # Only ep-3 names the User entity.
    assert centre["episode_count"] == 1


def test_depth1_neighbours_and_counts():
    result = _StubBackend().graph_neighbourhood("User", depth=1)
    neighbours = _neighbours_by_name(result)
    assert set(neighbours) == {"Maa", "Filter coffee", "Kolkata"}
    # Maa: f-maa's text names the user + ep-3 names both.
    assert neighbours["Maa"]["cooccurrence_count"] == 2
    assert neighbours["Maa"]["entity_type"] == "Person"
    assert neighbours["Maa"]["fact_count"] == 1
    # Filter coffee: its fact's text names the user.
    assert neighbours["Filter coffee"]["cooccurrence_count"] == 1
    # Kolkata: the superseding fact's text names the user.
    assert neighbours["Kolkata"]["cooccurrence_count"] == 1
    assert all(n["depth"] == 1 for n in neighbours.values())
    # Cycling reaches Maa, not the User — not a depth-1 neighbour.
    assert "Cycling" not in neighbours


def test_centre_name_matches_case_insensitively():
    result = _StubBackend().graph_neighbourhood("user")
    assert result["centre"]["entity_name"] == "User"


# ----------------------------------------------------------------------
# Depth 2
# ----------------------------------------------------------------------

def test_depth2_includes_neighbours_of_neighbours():
    result = _StubBackend().graph_neighbourhood("User", depth=2)
    neighbours = _neighbours_by_name(result)
    assert "Cycling" in neighbours
    cycling = neighbours["Cycling"]
    assert cycling["depth"] == 2
    # Counts are recomputed against the CENTRE: Cycling never
    # shares a record with the User directly, so its direct
    # count is honestly 0 — its presence is via Maa.
    assert cycling["cooccurrence_count"] == 0
    # Direct neighbours keep depth 1 and their centre counts.
    assert neighbours["Maa"]["depth"] == 1
    assert neighbours["Maa"]["cooccurrence_count"] == 2


# ----------------------------------------------------------------------
# Replaced chains
# ----------------------------------------------------------------------

def test_replaced_chains_from_superseded_by_links():
    result = _StubBackend().graph_neighbourhood("User")
    assert result["replaced_chains"] == [{
        "old_entity": "User",
        "new_entity": "Kolkata",
        "fact_id": "f-home-old",
    }]


def test_replaced_chain_visible_from_the_other_side():
    result = _StubBackend().graph_neighbourhood("Kolkata")
    assert result["replaced_chains"] == [{
        "old_entity": "User",
        "new_entity": "Kolkata",
        "fact_id": "f-home-old",
    }]


# ----------------------------------------------------------------------
# Honest empties + degraded legs
# ----------------------------------------------------------------------

def test_unknown_entity_is_an_honest_empty_result():
    result = _StubBackend().graph_neighbourhood("Nobody")
    assert result["centre"] == {
        "entity_name": "Nobody", "entity_type": None,
        "fact_count": 0, "episode_count": 0}
    assert result["neighbours"] == []
    assert result["replaced_chains"] == []


def test_episode_leg_failure_degrades_alone():
    class NoEpisodes(_StubBackend):
        def list_episodes(self, limit=50):
            raise NotImplementedError("no episode store")

    result = NoEpisodes().graph_neighbourhood("User")
    assert result["centre"]["episode_count"] is None
    # Fact-derived edges survive: f-maa still links Maa (count 1
    # now — the episode contribution is gone, not faked).
    neighbours = _neighbours_by_name(result)
    assert neighbours["Maa"]["cooccurrence_count"] == 1


# ----------------------------------------------------------------------
# Native backend end-to-end
# ----------------------------------------------------------------------

@pytest.fixture()
def kg_db(tmp_path):
    db = tmp_path / "kg.db"
    with patch.object(kg, "DB_PATH", db), \
            patch.object(kg, "_audit_logger") as audit:
        audit.log_event = lambda *a, **k: None
        kg.init_kg_db()
        yield db


def _insert_fact(db_path, fid, entity_id, entity_name, entity_type, text):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO entities (id, name, entity_type, first_seen, last_updated) "
        "VALUES (?, ?, ?, ?, ?)", (entity_id, entity_name, entity_type, NOW, NOW))
    conn.execute(
        "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, "
        "confidence, status, superseded_by, provenance, importance) "
        "VALUES (?, ?, ?, 'seed', ?, 0.9, 'active', NULL, 'stated', 0.8)",
        (fid, entity_id, text, NOW))
    conn.commit()
    conn.close()


def test_native_backend_neighbourhood(kg_db):
    _insert_fact(kg_db, "f-1", "e-user", "User", "Person",
                 "User's name is Arindam Roy")
    _insert_fact(kg_db, "f-2", "e-maa", "Maa", "Person",
                 "Maa is the user's mother")
    result = NativeMemoryBackend().graph_neighbourhood("Maa")
    assert result["centre"]["entity_name"] == "Maa"
    assert result["centre"]["fact_count"] == 1
    neighbours = _neighbours_by_name(result)
    assert neighbours["User"]["cooccurrence_count"] == 1
