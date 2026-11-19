"""S22 contract — chat memory-used disclosure payload (SPEC-6B C6).

Pins the C6 contract from SPEC-6B:

* every chat reply's metadata gains ``memory_used:
  [{id, summary, kind}]`` — kind fact | episode | profile —
  the items that actually scored into that reply's context,
  capped at 5, summaries only (Rule 3: IDs and one-line
  summaries, never raw stored text beyond the summary);
* both assembly points populate it — the supervisor (its memory
  tools' scored items, folded by the executor into the
  TASK_COMPLETED reply) and fast chat (profile digest items +
  the assembler's scored items);
* empty list when memory was off (C4) or nothing scored — the
  affordance then simply doesn't render.

All stores are tmp files (the S7 fixture pattern); providers
are scripted; no live calls.
"""
import asyncio
import sqlite3
import time
from unittest.mock import AsyncMock, patch

import pytest

from aether_common.contracts import EventType
from aether_engine.config import UserConfig
from aether_engine.event_bus import event_bus
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.fast_chat import run_fast_chat
from aether_engine.langgraph.nodes import supervisor as supervisor_module
from aether_engine.memory import episodic
from aether_engine.memory import knowledge_graph as kg
from aether_engine.memory.context_assembler import (
    memory_used_entries,
    profile_used_entries,
)
from aether_engine.memory.facade import NativeMemoryBackend
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.tools.registry import ToolRegistry

NOW = int(time.time())


# ----------------------------------------------------------------------
# Fixtures + helpers (the S7 pattern)
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
def no_curated(tmp_path):
    with patch("aether_engine.memory.consolidation.MEMORY_DOCUMENT_PATH",
               tmp_path / "curated_memory.md"):
        yield


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


def _seed_profile(db_path):
    _insert_fact(db_path, "f-name", "e-user", "User", "Person",
                 "User's name is Arindam Roy", conf=0.95, importance=0.9)
    _insert_fact(db_path, "f-drink", "e-coffee", "Filter coffee", "Concept",
                 "User's favourite morning drink is filter coffee")


class ChunkProvider:
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


def _completed(events):
    return next(ev for ev in events if ev.type == EventType.TASK_COMPLETED)


# ----------------------------------------------------------------------
# Shaping: the assembler's items -> disclosure entries
# ----------------------------------------------------------------------

def test_shaping_maps_facts_and_episodes_skips_curated():
    items = [
        {"source": "fact", "id": "f-1", "text": "A fact"},
        {"source": "episode", "id": "task-9", "text": "An episode"},
        {"source": "curated", "id": "curated_memory.md:3",
         "text": "A derived line — no kind in the contract fits it"},
        {"source": "fact", "id": "f-1", "text": "A fact (duplicate)"},
    ]
    entries = memory_used_entries(items)
    assert entries == [
        {"id": "f-1", "summary": "A fact", "kind": "fact"},
        {"id": "task-9", "summary": "An episode", "kind": "episode"},
    ]


def test_shaping_caps_at_five_and_summaries_are_one_line():
    items = [
        {"source": "fact", "id": f"f-{i}",
         "text": f"Fact number {i}\nwith a second line"}
        for i in range(8)
    ]
    entries = memory_used_entries(items)
    assert len(entries) == 5
    assert all("\n" not in e["summary"] for e in entries)
    long = memory_used_entries(
        [{"source": "fact", "id": "f-long", "text": "x" * 300}])
    assert len(long[0]["summary"]) <= 120


def test_profile_entries_use_the_digests_shown_sets():
    profile = {
        "user": [{"fact_id": f"u-{i}", "text": f"User fact {i}"}
                 for i in range(4)],
        "people": {"Maa": [{"fact_id": "p-1", "text": "Maa fact"}]},
        "preferences": [{"fact_id": f"pr-{i}", "text": f"Pref {i}"}
                        for i in range(3)],
    }
    entries = profile_used_entries(profile)
    # Top-3 user + top-2 preferences; the people line of a digest
    # carries names only, so people items are not disclosed.
    assert [e["id"] for e in entries] == ["u-0", "u-1", "u-2", "pr-0", "pr-1"]
    assert all(e["kind"] == "profile" for e in entries)


# ----------------------------------------------------------------------
# Fast chat: the TASK_COMPLETED reply carries memory_used
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fast_chat_reply_discloses_what_it_used(
        kg_db, epi_db, no_curated, dead_embedder):
    _seed_profile(kg_db)
    provider = ChunkProvider()
    events = await _collect(run_fast_chat(
        "what is my favourite morning drink?", provider, [],
        user_config=UserConfig()))
    used = _completed(events).payload["memory_used"]
    by_id = {e["id"]: e for e in used}
    # The scored recall item, as a fact...
    assert by_id["f-drink"]["kind"] == "fact"
    assert by_id["f-drink"]["summary"] == \
        "User's favourite morning drink is filter coffee"
    # ...and the digest's profile item, as a profile entry.
    assert by_id["f-name"]["kind"] == "profile"
    assert all(set(e) == {"id", "summary", "kind"} for e in used)
    assert len(used) <= 5


@pytest.mark.asyncio
async def test_fast_chat_disclosure_capped_at_five(
        kg_db, epi_db, no_curated, dead_embedder):
    for i in range(7):
        _insert_fact(kg_db, f"f-coffee-{i}", f"e-c{i}", f"Coffee {i}",
                     "Concept", f"Coffee note {i} about coffee beans")
    provider = ChunkProvider()
    events = await _collect(run_fast_chat(
        "tell me about coffee", provider, [], user_config=UserConfig()))
    used = _completed(events).payload["memory_used"]
    assert len(used) == 5
    assert len({e["id"] for e in used}) == 5


@pytest.mark.asyncio
async def test_fast_chat_disclosure_empty_when_nothing_stored(
        kg_db, epi_db, no_curated, dead_embedder):
    provider = ChunkProvider()
    events = await _collect(run_fast_chat(
        "hello there", provider, [], user_config=UserConfig()))
    assert _completed(events).payload["memory_used"] == []


# ----------------------------------------------------------------------
# Supervisor: memory tools publish, the executor folds
# ----------------------------------------------------------------------

async def _run_memory_tool(tool_name, args, task_id):
    """Drive one supervisor memory tool against the native
    backend, capturing its bus events."""
    queue: asyncio.Queue = asyncio.Queue()
    event_bus.subscribe(task_id, queue)
    try:
        result = await supervisor_module._execute_supervisor_memory_tool(
            tool_name, args, "sess", "prompt", task_id,
            user_config=UserConfig())
    finally:
        event_bus.unsubscribe(task_id, queue)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return result, events


@pytest.mark.asyncio
async def test_supervisor_search_publishes_memory_used(
        kg_db, epi_db, no_curated, dead_embedder, monkeypatch):
    _seed_profile(kg_db)
    monkeypatch.setattr(
        supervisor_module, "backend_or_none",
        lambda *a, **k: NativeMemoryBackend())
    result, events = await _run_memory_tool(
        "search_knowledge_graph", {"query": "user name"}, "task-disc-1")
    assert "Arindam" in result
    completed = next(
        ev for ev in events
        if ev.type == EventType.TOOL_ACTIVITY
        and ev.payload.get("status") == "completed")
    used = completed.payload["memory_used"]
    assert {"id": "f-name",
            "summary": "User's name is Arindam Roy",
            "kind": "fact"} in used


@pytest.mark.asyncio
async def test_supervisor_profile_tool_publishes_profile_entries(
        kg_db, epi_db, no_curated, dead_embedder, monkeypatch):
    _seed_profile(kg_db)
    monkeypatch.setattr(
        supervisor_module, "backend_or_none",
        lambda *a, **k: NativeMemoryBackend())
    _result, events = await _run_memory_tool(
        "get_user_profile", {}, "task-disc-2")
    completed = next(
        ev for ev in events
        if ev.type == EventType.TOOL_ACTIVITY
        and ev.payload.get("status") == "completed")
    used = completed.payload["memory_used"]
    assert {"id": "f-name",
            "summary": "User's name is Arindam Roy",
            "kind": "profile"} in used


class _NativeMemoryProvider:
    """Supervisor's first call emits a native memory tool call;
    its follow-up decides END (the journey pattern)."""

    def __init__(self):
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({"node": node_name})
        if len(self.calls) == 1:
            yield StreamChunk(
                tool_calls=[ToolCall(name="search_knowledge_graph",
                                     args={"query": "user name"},
                                     call_id="mem-call-1")],
                finish_reason="tool_calls")
        else:
            yield StreamChunk(
                text='{"next": "END", "reason": "answered from memory", "briefing": ""}',
                finish_reason="stop")


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(episodic, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(kg, "extract_and_store_facts", AsyncMock(return_value=None))
    return tmp_path


@pytest.mark.asyncio
async def test_supervisor_reply_carries_memory_used_end_to_end(
        graph_env, kg_db, epi_db, no_curated, dead_embedder, monkeypatch):
    _seed_profile(kg_db)
    monkeypatch.setattr(
        supervisor_module, "backend_or_none",
        lambda *a, **k: NativeMemoryBackend())
    provider = _NativeMemoryProvider()
    events = [event async for event in executor_module.run_langgraph_task(
        "what is my name?", provider, tools=ToolRegistry(),
        task_id="task-disc-e2e", request_id="req-disc-e2e",
        session_id="disc-e2e", user_config=UserConfig())]
    completed = _completed(events)
    used = completed.payload["memory_used"]
    assert {"id": "f-name",
            "summary": "User's name is Arindam Roy",
            "kind": "fact"} in used
    assert len(used) <= 5
