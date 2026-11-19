"""S22 / SPEC-6B C2 (FF5-05): the Forget centre's four calls.

Pins what the spec note promises for Batch 5 board 3, at the
facade level and over the wire:

* ``forget_candidates()`` — topic discovery: distinct topics
  ranked by item count, ``[{topic, fact_count, episode_count,
  last_touched}]``; topics are what the forget finder itself can
  match again (entity names, episode tags) and the counts come
  from running that finder, so discovery can never promise a
  different set than execute would remove;
* ``forget_execute(topic)`` — the EXACT removed list
  ``[{fact_id, summary}…, {episode_id, summary}…]`` +
  ``tombstone_id``; the records are really gone afterwards;
* ``forget_undo()`` — restores the latest tombstone and names
  what came back (counts + topic); nothing to undo is an honest
  empty result, not an error;
* ``forget_history(limit, offset)`` — tombstone entries newest
  first with a total; ``undoable`` is true only for the latest
  unconsumed tombstone.

All stores are tmp files (the S8 fixture patterns); the embedder
is dead — no live provider calls.
"""
import json
import sqlite3
import time
from unittest.mock import AsyncMock, patch

import pytest

from aether_engine.config import UserConfig
from aether_engine.memory import episodic, forget, learned
from aether_engine.memory import knowledge_graph as kg
from aether_engine.memory.facade import NativeMemoryBackend
from aether_engine.routes import ws_connection
from aether_engine.routes.ws_memory_personal import (
    send_memory_forget_candidates_result,
    send_memory_forget_execute_result,
    send_memory_forget_history_result,
    send_memory_forget_undo_result,
)

NOW = int(time.time())


@pytest.fixture()
def kg_db(tmp_path):
    db = tmp_path / "kg.db"
    with patch.object(kg, "DB_PATH", db), \
            patch.object(kg, "_audit_logger") as audit:
        audit.log_event = lambda *a, **k: None
        kg.init_kg_db()
        yield db


@pytest.fixture()
def epi_db(tmp_path):
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
def memory_home(tmp_path):
    with patch.object(learned, "LEARNED_MD_PATH", tmp_path / "learned.md"), \
            patch("aether_engine.memory.consolidation.MEMORY_DOCUMENT_PATH",
                  tmp_path / "curated_memory.md"), \
            patch.object(forget, "TOMBSTONES_PATH",
                         tmp_path / "forget_tombstones.json"):
        yield tmp_path


@pytest.fixture()
def dead_embedder():
    with patch.object(episodic, "embed_text",
                      new=AsyncMock(return_value=None)), \
            patch.object(episodic, "_embed_text",
                         new=AsyncMock(return_value=None)):
        yield


def _insert_fact(db_path, fid, entity_id, entity_name, entity_type, text, *,
                 ts=NOW, conf=0.9, provenance="stated", importance=0.8):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO entities (id, name, entity_type, first_seen, last_updated) "
        "VALUES (?, ?, ?, ?, ?)", (entity_id, entity_name, entity_type, ts, ts))
    conn.execute(
        "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, "
        "confidence, status, superseded_by, provenance, importance) "
        "VALUES (?, ?, ?, 'seed', ?, ?, 'active', NULL, ?, ?)",
        (fid, entity_id, text, ts, conf, provenance, importance))
    conn.commit()
    conn.close()


async def _seed_berlin(kg_db):
    """Two Berlin facts + one Berlin episode, one unrelated fact
    — the discovery/execute playground."""
    _insert_fact(kg_db, "f-berlin-1", "e-berlin", "Berlin trip", "Concept",
                 "User is planning a trip to Berlin in June", ts=NOW - 100)
    _insert_fact(kg_db, "f-berlin-2", "e-berlin", "Berlin trip", "Concept",
                 "User booked a Berlin hostel near Mitte", ts=NOW - 50)
    _insert_fact(kg_db, "f-name", "e-arindam", "Arindam", "Person",
                 "Arindam Roy lives in Kolkata")
    await episodic.store_episode(
        "task-berlin", "Plan my Berlin trip",
        "Planned the Berlin trip itinerary", ["travel"], "success")


# ----------------------------------------------------------------------
# Topic discovery
# ----------------------------------------------------------------------

async def test_discovery_ranks_topics_by_item_count(
        kg_db, epi_db, memory_home, dead_embedder):
    await _seed_berlin(kg_db)
    # An episode no fact mentions, findable again only through
    # its tag word, which its own text carries.
    await episodic.store_episode(
        "task-dentist", "Book a dentist appointment",
        "Booked the dentist for Friday", ["dentist"], "success")
    backend = NativeMemoryBackend()

    candidates = backend.forget_candidates()
    by_topic = {c["topic"]: c for c in candidates}

    berlin = by_topic["Berlin trip"]
    assert berlin["fact_count"] == 2
    assert berlin["episode_count"] == 1
    assert berlin["last_touched"] is not None
    # Ranked by item count: Berlin (3 items) leads.
    assert candidates[0]["topic"] == "Berlin trip"
    assert by_topic["Arindam"]["fact_count"] == 1
    assert by_topic["Arindam"]["episode_count"] == 0
    # The episode tag is a discoverable topic, counted by the
    # same finder: it matches the dentist episode and no facts.
    assert by_topic["dentist"]["fact_count"] == 0
    assert by_topic["dentist"]["episode_count"] == 1


async def test_discovery_on_empty_memory_is_an_empty_list(
        kg_db, epi_db, memory_home, dead_embedder):
    assert NativeMemoryBackend().forget_candidates() == []


# ----------------------------------------------------------------------
# Execute: the exact removed list
# ----------------------------------------------------------------------

async def test_execute_reports_exactly_what_was_removed(
        kg_db, epi_db, memory_home, dead_embedder):
    await _seed_berlin(kg_db)
    backend = NativeMemoryBackend()

    result = backend.forget_execute("Berlin trip", config=UserConfig())

    removed_facts = [r["fact_id"] for r in result["removed"] if "fact_id" in r]
    removed_episodes = [
        r["episode_id"] for r in result["removed"] if "episode_id" in r]
    assert sorted(removed_facts) == ["f-berlin-1", "f-berlin-2"]
    assert removed_episodes == ["task-berlin"]
    # Summaries are one line of the record's own text (Rule 3).
    berlin_summary = next(
        r["summary"] for r in result["removed"]
        if r.get("fact_id") == "f-berlin-1")
    assert berlin_summary == "User is planning a trip to Berlin in June"
    assert result["counts"] == {"facts": 2, "episodes": 1, "lessons": 0}
    assert result["tombstone_id"]
    assert result["undo_supported"] is True
    # And the records really are gone; the unrelated fact survives.
    assert kg.search_facts("Berlin") == []
    assert episodic.get_episode_snapshot("task-berlin") is None
    assert kg.search_facts("Arindam") != []


async def test_execute_with_no_match_removes_nothing(
        kg_db, epi_db, memory_home, dead_embedder):
    await _seed_berlin(kg_db)
    backend = NativeMemoryBackend()
    result = backend.forget_execute("Atlantis", config=UserConfig())
    assert result["removed"] == []
    assert result["counts"] == {"facts": 0, "episodes": 0, "lessons": 0}
    assert result["tombstone_id"] is None
    assert kg.search_facts("Berlin") != []


# ----------------------------------------------------------------------
# Undo: names what came back; nothing-to-undo is honest
# ----------------------------------------------------------------------

async def test_undo_restores_and_names_what_came_back(
        kg_db, epi_db, memory_home, dead_embedder):
    await _seed_berlin(kg_db)
    backend = NativeMemoryBackend()
    backend.forget_execute("Berlin trip", config=UserConfig())

    undo = backend.forget_undo(config=UserConfig())
    assert undo["undone"] is True
    assert undo["topic"] == "Berlin trip"
    assert undo["counts"] == {"facts": 2, "episodes": 1, "lessons": 0}
    assert kg.search_facts("Berlin") != []
    assert episodic.get_episode_snapshot("task-berlin") is not None

    # A second undo has nothing left: honest empty, not an error.
    again = backend.forget_undo(config=UserConfig())
    assert again["undone"] is False
    assert "error" not in again
    assert again["counts"] == {"facts": 0, "episodes": 0, "lessons": 0}


def test_undo_with_nothing_to_undo(kg_db, epi_db, memory_home, dead_embedder):
    undo = NativeMemoryBackend().forget_undo(config=UserConfig())
    assert undo["undone"] is False
    assert "error" not in undo


# ----------------------------------------------------------------------
# History: newest first, undoable only for the latest
# ----------------------------------------------------------------------

async def test_history_entries_and_the_undoable_flag(
        kg_db, epi_db, memory_home, dead_embedder):
    await _seed_berlin(kg_db)
    _insert_fact(kg_db, "f-priya", "e-priya", "Priya", "Person",
                 "Priya is the user's sister")
    backend = NativeMemoryBackend()
    backend.forget_execute("Priya", config=UserConfig())
    backend.forget_execute("Berlin trip", config=UserConfig())

    history = backend.forget_history()
    assert history["total"] == 2
    entries = history["entries"]
    assert [e["topic"] for e in entries] == ["Berlin trip", "Priya"]
    berlin, priya = entries
    assert berlin["fact_count"] == 2 and berlin["episode_count"] == 1
    assert berlin["forgotten_at"] is not None
    assert berlin["tombstone_id"]
    # Only the latest unconsumed tombstone is undoable.
    assert berlin["undoable"] is True
    assert priya["undoable"] is False

    # Pagination: limit + offset slice, total unchanged.
    page = backend.forget_history(limit=1, offset=1)
    assert page["total"] == 2
    assert [e["topic"] for e in page["entries"]] == ["Priya"]

    # After the undo consumes Berlin's tombstone, Priya's becomes
    # the latest unconsumed one — and Berlin stays in the history.
    backend.forget_undo(config=UserConfig())
    entries = backend.forget_history()["entries"]
    by_topic = {e["topic"]: e for e in entries}
    assert by_topic["Berlin trip"]["undoable"] is False
    assert by_topic["Priya"]["undoable"] is True


# ----------------------------------------------------------------------
# The wire senders
# ----------------------------------------------------------------------

class _FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


def _conn():
    return ws_connection.WsConnection(_FakeSocket(), object(), object())


def _patch_backend(monkeypatch, backend):
    monkeypatch.setattr(
        "aether_engine.routes.websocket.backend_or_none",
        lambda config, context="": backend)


async def test_forget_wire_round_trip(
        kg_db, epi_db, memory_home, dead_embedder, monkeypatch):
    await _seed_berlin(kg_db)
    _patch_backend(monkeypatch, NativeMemoryBackend())
    conn = _conn()

    await send_memory_forget_candidates_result(conn, "req-1")
    reply = conn.ws.sent[-1]
    assert reply["type"] == "MEMORY_FORGET_CANDIDATES_RESPONSE"
    topics = [c["topic"] for c in reply["payload"]["candidates"]]
    assert topics[0] == "Berlin trip"

    await send_memory_forget_execute_result(conn, "req-2", "Berlin trip")
    reply = conn.ws.sent[-1]
    assert reply["type"] == "MEMORY_FORGET_EXECUTE_RESPONSE"
    payload = reply["payload"]
    assert payload["success"] is True
    assert sorted(
        r["fact_id"] for r in payload["removed"] if "fact_id" in r
    ) == ["f-berlin-1", "f-berlin-2"]
    assert payload["tombstone_id"]

    await send_memory_forget_history_result(conn, "req-3", 20, 0)
    reply = conn.ws.sent[-1]
    assert reply["type"] == "MEMORY_FORGET_HISTORY_RESPONSE"
    assert reply["payload"]["total"] == 1
    assert reply["payload"]["history"][0]["undoable"] is True

    await send_memory_forget_undo_result(conn, "req-4")
    reply = conn.ws.sent[-1]
    assert reply["type"] == "MEMORY_FORGET_UNDO_RESPONSE"
    assert reply["payload"]["success"] is True
    assert reply["payload"]["undone"] is True
    assert reply["payload"]["topic"] == "Berlin trip"


async def test_forget_wire_reports_an_unavailable_backend(monkeypatch):
    _patch_backend(monkeypatch, None)
    conn = _conn()
    await send_memory_forget_candidates_result(conn, "req-1")
    payload = conn.ws.sent[-1]["payload"]
    assert payload["candidates"] == []
    assert "unavailable" in payload["error"]

    await send_memory_forget_execute_result(conn, "req-2", "Berlin trip")
    payload = conn.ws.sent[-1]["payload"]
    assert payload["success"] is False
    assert "unavailable" in payload["error"]
