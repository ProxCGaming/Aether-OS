"""S22 contract — memory stats + the D-5 config keys (SPEC-6B C4).

Pins the C4 contract from SPEC-6B:

* ``memory_stats()`` → ``{facts_active, facts_superseded,
  episodes, backend_id, backend_label, decay: {inferred_fade_days,
  last_cleanup_at, last_faded, last_removed}}`` — counts from the
  facade's own listings; decay numbers from the pruning run
  record (the audit log's MEMORY_PRUNED_ALL event), null when no
  run is recorded — never invented history (Rule 1).
* The D-5 pair — ``memory_use_enabled`` (read) and
  ``memory_learn_enabled`` (write), independent booleans, both
  default true — gate BOTH chat paths for assembly and learning.
  ``fast_chat_memory_enabled`` is retired: its stored value seeds
  ``memory_use_enabled`` when the pair was never set.
* The deciding case: use ON + learn OFF answers from memory and
  learns nothing new.

All stores are tmp files (the S7 fixture pattern); no live
provider calls.
"""
import json
import sqlite3
import time
from unittest.mock import AsyncMock, patch

import pytest

from aether_common.contracts import EventType
from aether_engine.config import UserConfig
from aether_engine.config_updates import (
    HANDED_OFF_FIELDS,
    validate_handed_off_updates,
)
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.fast_chat import (
    fast_chat_memory_briefing,
    learn_fast_chat_exchange,
    run_fast_chat,
)
from aether_engine.langgraph.nodes import supervisor as supervisor_module
from aether_engine.memory import episodic, learned
from aether_engine.memory import knowledge_graph as kg
from aether_engine.memory.facade import MemoryBackend, NativeMemoryBackend
from aether_engine.providers.base import StreamChunk
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
def learned_md(tmp_path):
    path = tmp_path / "learned.md"
    with patch.object(learned, "LEARNED_MD_PATH", path):
        yield path


@pytest.fixture()
def dead_embedder():
    with patch.object(episodic, "embed_text",
                      new=AsyncMock(return_value=None)), \
            patch.object(episodic, "_embed_text",
                         new=AsyncMock(return_value=None)):
        yield


def _insert_fact(db_path, fid, entity_id, entity_name, entity_type, text, *,
                 ts=NOW, conf=0.9, provenance="stated", importance=0.8,
                 status="active", superseded_by=None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO entities (id, name, entity_type, first_seen, last_updated) "
        "VALUES (?, ?, ?, ?, ?)", (entity_id, entity_name, entity_type, ts, ts))
    conn.execute(
        "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, "
        "confidence, status, superseded_by, provenance, importance) "
        "VALUES (?, ?, ?, 'seed', ?, ?, ?, ?, ?, ?)",
        (fid, entity_id, text, ts, conf, status, superseded_by,
         provenance, importance))
    conn.commit()
    conn.close()


def _seed_drink(kg_db):
    _insert_fact(kg_db, "f-drink", "e-coffee", "Filter coffee", "Concept",
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


def _episode_count():
    return len(episodic.get_all_episodes(limit=100))


# ----------------------------------------------------------------------
# The config pair: declaration, round-trip, migration
# ----------------------------------------------------------------------

def test_pair_defaults_on_and_independent():
    config = UserConfig()
    assert config.memory_use_enabled is True
    assert config.memory_learn_enabled is True
    config = UserConfig(memory_use_enabled=False)
    assert config.memory_use_enabled is False
    assert config.memory_learn_enabled is True
    config = UserConfig(memory_learn_enabled=False)
    assert config.memory_use_enabled is True
    assert config.memory_learn_enabled is False


def test_pair_round_trips_through_storage():
    config = UserConfig(memory_use_enabled=False, memory_learn_enabled=True)
    stored = config.to_dict()
    assert stored["memory_use_enabled"] is False
    assert stored["memory_learn_enabled"] is True
    loaded = UserConfig.from_dict(stored)
    assert loaded.memory_use_enabled is False
    assert loaded.memory_learn_enabled is True


def test_legacy_gate_seeds_use_on_load():
    """A config stored before the pair existed: the retired
    fast-chat gate's OFF is the user's memory choice — the pair
    must not silently re-enable it on upgrade."""
    loaded = UserConfig.from_dict({"fast_chat_memory_enabled": False})
    assert loaded.memory_use_enabled is False
    assert loaded.memory_learn_enabled is True
    loaded = UserConfig.from_dict({"fast_chat_memory_enabled": True})
    assert loaded.memory_use_enabled is True


def test_pair_wins_over_legacy_on_load():
    """Once the pair is stored, it is the state — a stale legacy
    value in the same file does not override it."""
    loaded = UserConfig.from_dict({
        "memory_use_enabled": True,
        "memory_learn_enabled": False,
        "fast_chat_memory_enabled": False,
    })
    assert loaded.memory_use_enabled is True
    assert loaded.memory_learn_enabled is False


def test_legacy_gate_seeds_use_on_direct_construction():
    """The pre-pair construction shape (the S7 suite's toggle)
    still reads as memory off."""
    config = UserConfig(fast_chat_memory_enabled=False)
    assert config.memory_use_enabled is False


def test_pair_is_strictly_validated_for_settings_writes():
    assert "memory_use_enabled" in HANDED_OFF_FIELDS
    assert "memory_learn_enabled" in HANDED_OFF_FIELDS
    cleaned = validate_handed_off_updates(
        {"memory_use_enabled": False, "memory_learn_enabled": True})
    assert cleaned == {
        "memory_use_enabled": False, "memory_learn_enabled": True}
    with pytest.raises(ValueError, match="memory_use_enabled"):
        validate_handed_off_updates({"memory_use_enabled": "yes"})
    with pytest.raises(ValueError, match="memory_learn_enabled"):
        validate_handed_off_updates({"memory_learn_enabled": 0})


# ----------------------------------------------------------------------
# memory_stats(): counts, labels, decay honesty
# ----------------------------------------------------------------------

class _StubBackend(MemoryBackend):
    name = "native"

    def __init__(self, facts, episodes):
        self._facts = facts
        self._episodes = episodes

    def list_facts(self, limit=100, include_history=False):
        return list(self._facts)

    def list_episodes(self, limit=50):
        return list(self._episodes)


def _fact(fid, status="active"):
    return {"fact_id": fid, "entity_name": "User", "entity_type": "Person",
            "fact_text": f"fact {fid}", "status": status,
            "superseded_by": "f-new" if status == "superseded" else None,
            "timestamp": NOW, "confidence": 0.9, "provenance": "stated",
            "importance": 0.8}


def test_stats_counts_active_superseded_episodes(tmp_path):
    backend = _StubBackend(
        [_fact("f-1"), _fact("f-2"), _fact("f-3", "superseded")],
        [{"task_id": "t-1"}, {"task_id": "t-2"}])
    stats = backend.memory_stats()
    assert stats["facts_active"] == 2
    assert stats["facts_superseded"] == 1
    assert stats["episodes"] == 2
    assert stats["backend_id"] == "native"
    assert stats["backend_label"] == "Native (built-in)"
    assert stats["decay"]["inferred_fade_days"] == 45


def test_stats_decay_nulls_without_a_run_record(tmp_path):
    """No cleanup has ever run → the decay fields are null,
    never invented history (Rule 1)."""
    backend = _StubBackend([_fact("f-1")], [])
    from aether_engine.memory import stats as stats_module

    result = stats_module.memory_stats(
        backend, audit_log_path=tmp_path / "no-audit.log")
    assert result["decay"]["last_cleanup_at"] is None
    assert result["decay"]["last_faded"] is None
    assert result["decay"]["last_removed"] is None


def test_stats_decay_from_the_prune_run_record(tmp_path):
    audit_log = tmp_path / "audit.log"
    older = {"type": "ENGINE_EVENT", "ts": NOW - 10_000,
             "action": "MEMORY_PRUNED_ALL",
             "details": {"decay": {"decayed": 1, "removed_below_floor": 0}}}
    newer = {"type": "ENGINE_EVENT", "ts": NOW - 100,
             "action": "MEMORY_PRUNED_ALL",
             "details": {"decay": {"decayed": 7, "removed_below_floor": 2}}}
    audit_log.write_text(
        "\n".join(json.dumps(r) for r in (older, newer)) + "\n",
        encoding="utf-8")
    from aether_engine.memory import stats as stats_module

    result = stats_module.memory_stats(
        _StubBackend([], []), audit_log_path=audit_log)
    assert result["decay"]["last_cleanup_at"] == float(NOW - 100)
    assert result["decay"]["last_faded"] == 7
    assert result["decay"]["last_removed"] == 2


def test_stats_decay_falls_back_to_the_decay_event(tmp_path):
    audit_log = tmp_path / "audit.log"
    record = {"type": "ENGINE_EVENT", "ts": NOW - 50,
              "action": "KNOWLEDGE_GRAPH_DECAYED",
              "details": {"decayed": 3, "removed_below_floor": 1}}
    audit_log.write_text(json.dumps(record) + "\n", encoding="utf-8")
    from aether_engine.memory import stats as stats_module

    result = stats_module.memory_stats(
        _StubBackend([], []), audit_log_path=audit_log)
    assert result["decay"]["last_cleanup_at"] == float(NOW - 50)
    assert result["decay"]["last_faded"] == 3
    assert result["decay"]["last_removed"] == 1


def test_stats_unservable_leg_reports_null_not_zero():
    class BrokenEpisodes(_StubBackend):
        def list_episodes(self, limit=50):
            raise NotImplementedError("no episode store")

    stats = BrokenEpisodes([_fact("f-1")], []).memory_stats()
    assert stats["facts_active"] == 1
    assert stats["episodes"] is None


def test_stats_on_the_native_backend(kg_db, epi_db, dead_embedder):
    _seed_drink(kg_db)
    _insert_fact(kg_db, "f-old", "e-coffee", "Filter coffee", "Concept",
                 "User's favourite morning drink was instant coffee",
                 status="superseded", superseded_by="f-drink")
    backend = NativeMemoryBackend()
    stats = backend.memory_stats()
    assert stats["facts_active"] == 1
    assert stats["facts_superseded"] == 1
    assert stats["episodes"] == 0
    assert stats["backend_id"] == "native"


# ----------------------------------------------------------------------
# Gating — fast chat
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deciding_case_use_on_learn_off_answers_and_stores_nothing(
        kg_db, epi_db, no_curated, learned_md, dead_embedder):
    """THE deciding case (D-5): use ON + learn OFF — the reply
    is built from stored memory, and the exchange leaves no new
    fact or episode behind."""
    _seed_drink(kg_db)
    config = UserConfig(memory_use_enabled=True, memory_learn_enabled=False)

    provider = ChunkProvider()
    events = await _collect(run_fast_chat(
        "what is my favourite morning drink?", provider, [],
        user_config=config))
    prompt_text = "\n".join(
        str(m.get("content", "")) for m in provider.calls[0])
    assert "filter coffee" in prompt_text, (
        "use ON: the stored fact must reach the reply's context")
    completed = next(ev for ev in events if ev.type == EventType.TASK_COMPLETED)
    assert completed.payload["memory_used"], (
        "the reply must disclose the memory it used")

    await learn_fast_chat_exchange(
        "task-deciding", "what is my favourite morning drink?",
        "Your favourite morning drink is filter coffee.", None, config)
    assert _episode_count() == 0, (
        "learn OFF: no episode may be persisted from the exchange")
    assert len(kg.get_all_facts(limit=100, include_history=True)) == 1, (
        "learn OFF: no new fact may be persisted from the exchange")


@pytest.mark.asyncio
async def test_learn_on_control_persists_the_episode(
        kg_db, epi_db, no_curated, learned_md, dead_embedder):
    """Control for the deciding case: with learning on, the same
    exchange DOES record an episode — the gate is the only
    difference."""
    _seed_drink(kg_db)
    config = UserConfig(memory_use_enabled=True, memory_learn_enabled=True)
    await learn_fast_chat_exchange(
        "task-control", "what is my favourite morning drink?",
        "Your favourite morning drink is filter coffee.", None, config)
    assert _episode_count() == 1


@pytest.mark.asyncio
async def test_use_off_fast_chat_assembles_nothing(
        kg_db, epi_db, no_curated, learned_md, dead_embedder):
    _seed_drink(kg_db)
    config = UserConfig(memory_use_enabled=False)
    assert await fast_chat_memory_briefing(
        "what is my favourite morning drink?", config) == ""
    provider = ChunkProvider()
    events = await _collect(run_fast_chat(
        "what is my favourite morning drink?", provider, [],
        user_config=config))
    prompt_text = "\n".join(
        str(m.get("content", "")) for m in provider.calls[0])
    assert "filter coffee" not in prompt_text
    completed = next(ev for ev in events if ev.type == EventType.TASK_COMPLETED)
    assert completed.payload["memory_used"] == []


# ----------------------------------------------------------------------
# Gating — supervisor path
# ----------------------------------------------------------------------

class _RecordingBackend(MemoryBackend):
    """Facade double for the supervisor's memory tools: records
    every read the assembler attempts."""

    name = "native"

    def __init__(self):
        self.searches = []

    def search_facts(self, query, limit=10, include_history=False):
        self.searches.append(query)
        return [{"fact_id": "f-name", "entity_name": "User",
                 "entity_type": "Person",
                 "fact_text": "The user is called Arindam",
                 "timestamp": NOW, "confidence": 0.95,
                 "provenance": "stated", "importance": 0.9,
                 "status": "active"}]

    def list_facts(self, limit=100, include_history=False):
        return self.search_facts("list", limit=limit)

    async def search_episodes(self, query, limit=5):
        self.searches.append(query)
        return []


@pytest.mark.asyncio
async def test_supervisor_memory_read_gated_when_use_off(monkeypatch):
    backend = _RecordingBackend()
    monkeypatch.setattr(
        supervisor_module, "backend_or_none", lambda *a, **k: backend)
    result = await supervisor_module._execute_supervisor_memory_tool(
        "search_knowledge_graph", {"query": "user name"}, "sess", "prompt",
        "task-gate", user_config=UserConfig(memory_use_enabled=False))
    assert "paused" in result
    assert "Arindam" not in result
    assert backend.searches == [], (
        "use OFF: the supervisor's assembly must not touch the store")


@pytest.mark.asyncio
async def test_supervisor_memory_read_flows_when_use_on(monkeypatch):
    backend = _RecordingBackend()
    monkeypatch.setattr(
        supervisor_module, "backend_or_none", lambda *a, **k: backend)
    result = await supervisor_module._execute_supervisor_memory_tool(
        "search_knowledge_graph", {"query": "user name"}, "sess", "prompt",
        "task-flow", user_config=UserConfig(memory_use_enabled=True))
    assert "Arindam" in result
    assert backend.searches


class _SingleSpecialistProvider:
    """Supervisor script: planner -> END (the journey pattern)."""

    def __init__(self):
        self.calls = []
        self._supervisor_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({"node": node_name})
        if node_name == "planner":
            yield StreamChunk(text="Specialist final answer.",
                              finish_reason="stop")
            return
        self._supervisor_calls += 1
        if self._supervisor_calls == 1:
            text = '{"next": "planner", "reason": "handle it", "briefing": ""}'
        else:
            text = '{"next": "END", "reason": "done", "briefing": ""}'
        yield StreamChunk(text=text, finish_reason="stop")


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    """The journey fixture, with the learn sinks kept as
    assertable mocks (mirrors test_epic1_supervisor_journey)."""
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    store_episode = AsyncMock(return_value=None)
    extract_facts = AsyncMock(return_value=None)
    monkeypatch.setattr(episodic, "store_episode", store_episode)
    monkeypatch.setattr(kg, "extract_and_store_facts", extract_facts)
    from aether_engine.langgraph.nodes import planner as planner_module
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "journey/plan.txt")
    return {"store_episode": store_episode, "extract_facts": extract_facts}


async def _run_graph(session_id, user_config):
    provider = _SingleSpecialistProvider()
    return [event async for event in executor_module.run_langgraph_task(
        "do the work", provider, tools=ToolRegistry(),
        task_id=f"task-{session_id}", request_id=f"req-{session_id}",
        session_id=session_id, user_config=user_config)]


@pytest.mark.asyncio
async def test_supervisor_learn_gate_blocks_completion_writes(graph_env):
    events = await _run_graph(
        "w6-learn-off", UserConfig(memory_learn_enabled=False))
    assert EventType.TASK_COMPLETED in [ev.type for ev in events]
    assert graph_env["store_episode"].call_count == 0, (
        "learn OFF: a completed graph task records no episode")
    assert graph_env["extract_facts"].call_count == 0, (
        "learn OFF: a completed graph task extracts no facts")


@pytest.mark.asyncio
async def test_supervisor_learn_on_control_writes(graph_env):
    events = await _run_graph(
        "w6-learn-on", UserConfig(memory_learn_enabled=True))
    assert EventType.TASK_COMPLETED in [ev.type for ev in events]
    assert graph_env["store_episode"].call_count == 1
    assert graph_env["extract_facts"].call_count == 1
