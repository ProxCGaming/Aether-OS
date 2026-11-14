"""S8: conversational forget — intent path, cascade, tombstone undo.

Pins the S8 contract from the master-build plan (§4 S8, R12):

* intent detection is deterministic and conservative — forget
  commands lead the message, questions never trigger, and
  "don't forget ..." (a REMEMBER request) never triggers;
* the ENGINE resolves the topic literally (entity/text matching,
  never embeddings) and deletes itself, cascading across KG facts
  (+ their fact_embeddings rows), episodes (FTS + thinking +
  embedding rows), learned.md lines and the curated document,
  which is regenerated so a forgotten fact cannot resurface
  through the S5 assembler's curated leg;
* everything deleted is tombstoned first (full records), and
  "undo forget" restores the most recent tombstone with original
  ids / provenance / confidence, then consumes it;
* honesty rails: no match = nothing deleted and the note says so;
  bare "forget everything" executes nothing and points at
  Settings → Memory Management; a backend without snapshot /
  restore reports undo as unsupported instead of faking it;
* the supervisor's forget_memory tool runs the same facade
  service, is classified DESTRUCTIVE at the name level (default
  policy "ask", never auto-allowed), and honours the memory-tool
  gate's Deny.

All stores are tmp files; the embedder is dead (substring/BM25
legs only) — no live provider calls.
"""
import sqlite3
import time
from unittest.mock import AsyncMock, patch

import pytest

from aether_common.contracts import EventType
from aether_engine.config import UserConfig
from aether_engine.langgraph.fast_chat import run_fast_chat
from aether_engine.langgraph.nodes.supervisor import (
    SUPERVISOR_MEMORY_TOOLS,
    _execute_supervisor_memory_tool,
    _memory_tool_gate,
)
from aether_engine.memory import episodic, forget, learned
from aether_engine.memory import knowledge_graph as kg
from aether_engine.memory.context_assembler import assemble_memory_context
from aether_engine.memory.facade import MemoryBackend, NativeMemoryBackend
from aether_engine.providers.base import StreamChunk
from aether_engine.tools.policy import resolve_policy_by_name, tool_risk_by_name
from aether_engine.tools.registry import ToolRisk

NOW = int(time.time())


# ----------------------------------------------------------------------
# Fixtures + helpers (the S6/S7 patterns)
# ----------------------------------------------------------------------

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
    """learned.md + curated doc + tombstones all in the tmp home."""
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


def _seed_berlin_fact(db_path, **kw):
    _insert_fact(db_path, "f-berlin", "e-berlin", "Berlin trip", "Concept",
                 "User is planning a trip to Berlin in June", **kw)


def _fact_embedding_count(db_path, fid):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM fact_embeddings WHERE fact_id = ?",
            (fid,)).fetchone()[0]
    finally:
        conn.close()


def _episode_embedding_count(task_id):
    with episodic._get_conn(episodic.EMBEDDINGS_DB_PATH) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM episodic_embeddings WHERE task_id = ?",
            (task_id,)).fetchone()[0]


class ChunkProvider:
    """Records the messages it was shown; yields fixed chunks
    (the test_epic1_fast_chat pattern)."""

    def __init__(self, chunks=("answer",)):
        self.chunks = list(chunks)
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append([dict(m) for m in messages])
        for text in self.chunks:
            yield StreamChunk(text=text)
        yield StreamChunk(finish_reason="stop")


async def _collect(agen):
    return [event async for event in agen]


def _notes(provider):
    return [
        m["content"] for m in provider.calls[0]
        if m["role"] == "system" and m["content"].startswith("[Memory]")
    ]


# ----------------------------------------------------------------------
# Intent detection — deterministic and conservative
# ----------------------------------------------------------------------

@pytest.mark.parametrize("text,kind,topic", [
    ("forget that", "referent", ""),
    ("forget it", "referent", ""),
    ("forget about my Berlin trip", "topic", "berlin trip"),
    ("please forget about Maa's birthday plans", "topic", "maa's birthday plans"),
    ("forget my password", "topic", "password"),
    ("forget everything about my project", "topic", "project"),
    ("forget all about the Berlin trip", "topic", "berlin trip"),
    ("don't remember my old address", "topic", "old address"),
    ("do not remember that", "referent", ""),
    ("remove the Berlin trip from your memory", "topic", "berlin trip"),
    ("delete that from your memory", "referent", ""),
    ("forget everything", "global", ""),
    ("forget it all", "global", ""),
    ("undo forget", "undo", ""),
    ("undo that forget", "undo", ""),
    ("undo the last forget", "undo", ""),
])
def test_intent_detection_commands(text, kind, topic):
    intent = forget.detect_forget_intent(text)
    assert intent is not None
    assert intent.kind == kind
    assert intent.topic == topic


@pytest.mark.parametrize("text", [
    "don't forget to remind me about the PTM tomorrow",
    "do not forget my birthday",
    "never forget the Alamo",
    "what did you forget?",
    "did you forget my name?",
    "can you forget about the Berlin trip?",
    "I don't remember his name",
    "remember the Berlin trip for next time",
    "hello, how are you today",
    "tell me about the Berlin trip",
])
def test_intent_detection_non_commands(text):
    assert forget.detect_forget_intent(text) is None


# ----------------------------------------------------------------------
# The service: cascade across every surface
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_service_deletes_fact_across_surfaces(
        kg_db, epi_db, memory_home, dead_embedder):
    from aether_engine.memory import consolidation

    _seed_berlin_fact(kg_db)
    _insert_fact(kg_db, "f-name", "e-user", "User", "Person",
                 "User's name is Arindam Roy")
    # A stored vector for the Berlin fact (S6 table, same file).
    conn = sqlite3.connect(kg_db)
    conn.execute(
        "INSERT INTO fact_embeddings (fact_id, embedding, model, dimension) "
        "VALUES (?, ?, ?, ?)", ("f-berlin", b"\x00\x01\x02", "fake", 3))
    conn.commit()
    conn.close()
    assert _fact_embedding_count(kg_db, "f-berlin") == 1

    # The curated document renders the fact before the forget.
    result = consolidation.consolidate_memory_document(UserConfig())
    assert result["written"] is True
    doc = (memory_home / "curated_memory.md").read_text(encoding="utf-8")
    assert "Berlin" in doc

    backend = NativeMemoryBackend()
    outcome = backend.forget_memory(
        "Berlin trip", phrase="forget about my Berlin trip",
        config=UserConfig())

    assert outcome["counts"] == {"facts": 1, "episodes": 0, "lessons": 0}
    assert outcome["undo_supported"] is True
    assert outcome["tombstone_id"]
    # The fact and its vector are gone; the unrelated fact survives.
    assert kg.search_facts("Berlin") == []
    assert kg.get_fact_snapshot("f-berlin") is None
    assert _fact_embedding_count(kg_db, "f-berlin") == 0
    assert kg.search_facts("Arindam") != []
    # The curated document was regenerated without the fact...
    doc = (memory_home / "curated_memory.md").read_text(encoding="utf-8")
    assert "Berlin" not in doc
    # ...so the S5 assembler cannot resurface it through the
    # curated leg either.
    assembled = await assemble_memory_context(
        backend, "Berlin trip", config=UserConfig())
    assert "Berlin" not in assembled["text"]


@pytest.mark.asyncio
async def test_service_deletes_episode_from_fts_and_embeddings(
        kg_db, epi_db, memory_home, dead_embedder):
    await episodic.store_episode(
        "task-berlin", "Plan my Berlin trip",
        "Planned the Berlin trip itinerary", ["travel"],
        "Booked the Berlin hostel and listed museums")
    episodic.init_embeddings_db()
    with episodic._get_conn(episodic.EMBEDDINGS_DB_PATH) as conn:
        conn.execute(
            "INSERT INTO episodic_embeddings (task_id, embedding, model, dimension) "
            "VALUES (?, ?, ?, ?)",
            ("task-berlin", b"\x00\x01", "fake", 2))
        conn.commit()
    assert _episode_embedding_count("task-berlin") == 1
    found = await episodic.query_episodes("Berlin")
    assert [row["task_id"] for row in found] == ["task-berlin"]

    backend = NativeMemoryBackend()
    outcome = backend.forget_memory(
        "Berlin", phrase="forget about the Berlin trip",
        config=UserConfig())

    assert outcome["counts"]["episodes"] == 1
    assert episodic.get_episode_snapshot("task-berlin") is None
    assert await episodic.query_episodes("Berlin") == []
    assert _episode_embedding_count("task-berlin") == 0


def test_service_removes_learned_lines(kg_db, epi_db, memory_home, dead_embedder):
    learned.record_lessons([
        "Always rehearse the Berlin trip itinerary aloud",
        "Prefer small, focused commits",
    ], task_id="seed")
    assert len(learned.load_lessons()) == 2

    backend = NativeMemoryBackend()
    outcome = backend.forget_memory("Berlin", config=UserConfig())

    assert outcome["counts"]["lessons"] == 1
    assert learned.load_lessons() == ["Prefer small, focused commits"]


# ----------------------------------------------------------------------
# Tombstone undo
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tombstone_restore_roundtrip(
        kg_db, epi_db, memory_home, dead_embedder):
    _seed_berlin_fact(kg_db, conf=0.9, provenance="stated", importance=0.8)
    await episodic.store_episode(
        "task-berlin", "Plan my Berlin trip",
        "Planned the Berlin trip itinerary", ["travel"],
        "Booked the Berlin hostel")
    learned.record_lessons(
        ["Always rehearse the Berlin trip itinerary aloud"], task_id="seed")

    backend = NativeMemoryBackend()
    outcome = backend.forget_memory("Berlin trip", config=UserConfig())
    assert outcome["counts"] == {"facts": 1, "episodes": 1, "lessons": 1}
    assert kg.search_facts("Berlin") == []

    restored = backend.restore_last_forget(config=UserConfig())
    assert restored is not None
    assert len(restored["restored"]["facts"]) == 1
    assert len(restored["restored"]["episodes"]) == 1
    assert len(restored["restored"]["lessons"]) == 1
    assert restored["unrestored"] == []

    # The fact is back with its ORIGINAL id, provenance, confidence.
    snap = kg.get_fact_snapshot("f-berlin")
    assert snap is not None
    assert snap["fact_text"] == "User is planning a trip to Berlin in June"
    assert snap["provenance"] == "stated"
    assert float(snap["confidence"]) == 0.9
    assert float(snap["importance"]) == 0.8
    # The episode is searchable again, the lesson is back.
    found = await episodic.query_episodes("Berlin")
    assert [row["task_id"] for row in found] == ["task-berlin"]
    assert learned.load_lessons() == [
        "Always rehearse the Berlin trip itinerary aloud"]
    # The tombstone is consumed — a second undo finds nothing.
    assert backend.restore_last_forget(config=UserConfig()) is None


def test_restore_with_nothing_to_undo(kg_db, epi_db, memory_home):
    backend = NativeMemoryBackend()
    assert backend.restore_last_forget(config=UserConfig()) is None
    assert forget.latest_tombstone() is None


# ----------------------------------------------------------------------
# Fast chat end-to-end: the engine deletes, the note announces
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fast_chat_forget_end_to_end(
        kg_db, epi_db, memory_home, dead_embedder):
    _seed_berlin_fact(kg_db)
    provider = ChunkProvider()

    events = await _collect(run_fast_chat(
        "forget about my Berlin trip", provider, [],
        user_config=UserConfig()))

    assert events[-1].type == EventType.TASK_COMPLETED
    notes = _notes(provider)
    assert len(notes) == 1
    assert "User is planning a trip to Berlin in June" in notes[0]
    assert "undo forget" in notes[0]
    # The deletion actually happened in the store — the note is
    # not the model claiming it did something.
    assert kg.search_facts("Berlin") == []


@pytest.mark.asyncio
async def test_fast_chat_undo_forget_restores(
        kg_db, epi_db, memory_home, dead_embedder):
    _seed_berlin_fact(kg_db)
    backend = NativeMemoryBackend()
    backend.forget_memory("Berlin trip", config=UserConfig())
    assert kg.search_facts("Berlin") == []
    provider = ChunkProvider()

    events = await _collect(run_fast_chat(
        "undo forget", provider, [], user_config=UserConfig()))

    assert events[-1].type == EventType.TASK_COMPLETED
    notes = _notes(provider)
    assert len(notes) == 1
    assert "restored" in notes[0]
    assert "User is planning a trip to Berlin in June" in notes[0]
    assert kg.search_facts("Berlin") != []


@pytest.mark.asyncio
async def test_fast_chat_no_match_deletes_nothing(
        kg_db, epi_db, memory_home, dead_embedder):
    _seed_berlin_fact(kg_db)
    provider = ChunkProvider()

    events = await _collect(run_fast_chat(
        "forget about my Mars colony", provider, [],
        user_config=UserConfig()))

    assert events[-1].type == EventType.TASK_COMPLETED
    notes = _notes(provider)
    assert len(notes) == 1
    assert "NOTHING" in notes[0] and "was deleted" in notes[0]
    # Nothing deleted, no tombstone left behind.
    assert kg.search_facts("Berlin") != []
    assert forget.latest_tombstone() is None


@pytest.mark.asyncio
async def test_fast_chat_forget_everything_executes_nothing(
        kg_db, epi_db, memory_home, dead_embedder):
    _seed_berlin_fact(kg_db)
    provider = ChunkProvider()

    events = await _collect(run_fast_chat(
        "forget everything", provider, [], user_config=UserConfig()))

    assert events[-1].type == EventType.TASK_COMPLETED
    notes = _notes(provider)
    assert len(notes) == 1
    assert "NOT done" in notes[0]
    assert "Settings → Memory Management" in notes[0]
    assert kg.search_facts("Berlin") != []
    assert forget.latest_tombstone() is None


@pytest.mark.asyncio
async def test_fast_chat_forget_that_takes_most_recent_fact(
        kg_db, epi_db, memory_home, dead_embedder):
    _insert_fact(kg_db, "f-old", "e-user", "User", "Person",
                 "User's name is Arindam Roy", ts=NOW - 3600)
    _seed_berlin_fact(kg_db, ts=NOW - 60)
    provider = ChunkProvider()

    events = await _collect(run_fast_chat(
        "forget that", provider, [], user_config=UserConfig()))

    assert events[-1].type == EventType.TASK_COMPLETED
    notes = _notes(provider)
    assert len(notes) == 1
    assert "most recently learned item" in notes[0]
    assert "Berlin" in notes[0]
    # Only the newest fact went; the older one is untouched.
    assert kg.search_facts("Berlin") == []
    assert kg.search_facts("Arindam") != []


def test_note_hook_ignores_non_commands(kg_db, epi_db, memory_home):
    # A remember request must not produce a forget note (and must
    # not delete anything) — the hook returns None outright.
    assert forget.note_for_message_if_enabled(
        "don't forget to remind me about the PTM",
        user_config=UserConfig()) is None
    assert forget.note_for_message_if_enabled(
        "hello, how are you?", user_config=UserConfig()) is None


# ----------------------------------------------------------------------
# Supervisor parity + policy classification
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_supervisor_forget_tool_executes(
        kg_db, epi_db, memory_home, dead_embedder):
    _seed_berlin_fact(kg_db)

    result = await _execute_supervisor_memory_tool(
        "forget_memory", {"topic": "Berlin trip"}, None,
        "forget my Berlin trip", "task-s8", user_config=UserConfig())

    assert 'Forgot "Berlin trip"' in result
    assert "User is planning a trip to Berlin in June" in result
    assert "undo" in result
    assert kg.search_facts("Berlin") == []
    # A no-match call reports the truth instead of a deletion.
    result = await _execute_supervisor_memory_tool(
        "forget_memory", {"topic": "Mars colony"}, None,
        "forget my Mars colony", "task-s8", user_config=UserConfig())
    assert "nothing was deleted" in result


def test_forget_memory_policy_classification():
    # Name-level: DESTRUCTIVE — the default verdict is "ask" and
    # an "Always Allow" setting cannot auto-allow it.
    assert tool_risk_by_name("forget_memory") is ToolRisk.DESTRUCTIVE
    assert resolve_policy_by_name("forget_memory", {}) == "ask"
    assert resolve_policy_by_name(
        "forget_memory", {"forget_memory": "Always Allow"}) == "ask"
    # The supervisor memory-tool gate honours an explicit Deny.
    config = {"configurable": {
        "user_config": UserConfig(
            tool_policies={"forget_memory": "Deny"})}}
    gate = _memory_tool_gate("forget_memory", config)
    assert gate is not None and "blocked by user policy" in gate
    # "forget_memory" is a real member of the tool surfaces.
    assert "forget_memory" in {
        t["function"]["name"] for t in SUPERVISOR_MEMORY_TOOLS
    }
    from aether_engine.langgraph.nodes.supervisor import (
        build_supervisor_memory_tools,
    )
    assert "forget_memory" in {
        t.name for t in build_supervisor_memory_tools()
    }


# ----------------------------------------------------------------------
# Honest degradation on a backend without snapshot/restore
# ----------------------------------------------------------------------

class _FakeBackend(MemoryBackend):
    """A minimal non-native backend: facade-level find/delete
    only — snapshots raise the base class's NotImplementedError."""

    name = "fake-test"

    def __init__(self):
        self.facts = [{
            "fact_id": "x1", "entity_name": "Berlin trip",
            "entity_type": "Concept",
            "fact_text": "User is planning a trip to Berlin in June",
            "timestamp": NOW, "confidence": 0.9, "status": "active",
            "provenance": "inferred", "importance": 0.5,
        }]
        self.episodes = []

    def search_facts(self, query, limit=10, include_history=False):
        return list(self.facts)

    def list_facts(self, limit=100, include_history=False):
        return list(self.facts)

    def list_episodes(self, limit=50):
        return list(self.episodes)

    def delete_fact(self, fact_id):
        before = len(self.facts)
        self.facts = [f for f in self.facts if f["fact_id"] != fact_id]
        return len(self.facts) < before

    def delete_episode(self, task_id):
        return False


def test_forget_on_backend_without_snapshots_is_honest(
        kg_db, epi_db, memory_home):
    backend = _FakeBackend()
    outcome = backend.forget_memory("Berlin trip", config=UserConfig())

    # The deletion still happens through the facade...
    assert outcome["counts"]["facts"] == 1
    assert backend.facts == []
    # ...but undo is reported unsupported, with the reason named.
    assert outcome["undo_supported"] is False
    assert outcome["unsupported"]
    assert "fake-test" in outcome["unsupported"][0]
    # And an undo attempt restores nothing and says which items
    # could not come back — never a fake success.
    restored = backend.restore_last_forget(config=UserConfig())
    assert restored is not None
    assert restored["restored"]["facts"] == []
    assert len(restored["unrestored"]) == 1


# ---------------------------------------------------------------------------
# Lead addition (14 Nov 2026): the post-turn learn loop must never
# learn from a forget/undo exchange — its reply quotes the deleted
# item, so extraction could re-store what was just ordered forgotten.
# ---------------------------------------------------------------------------


class _RecordingBackend:
    def __init__(self):
        self.episodes = []

    async def record_episode(self, **kwargs):
        self.episodes.append(kwargs)


def test_learn_loop_skips_forget_exchanges(monkeypatch):
    import asyncio

    from aether_engine.langgraph import fast_chat
    from aether_engine.memory import facade as facade_mod

    backend = _RecordingBackend()
    monkeypatch.setattr(
        facade_mod, "backend_or_none", lambda *a, **k: backend)

    asyncio.run(fast_chat.learn_fast_chat_exchange(
        "t-forget", "forget about the Berlin trip",
        "Forgotten: the Berlin trip fact.", None))
    assert backend.episodes == []

    asyncio.run(fast_chat.learn_fast_chat_exchange(
        "t-normal", "I went to Berlin last spring",
        "Nice — how was it?", None))
    assert len(backend.episodes) == 1
