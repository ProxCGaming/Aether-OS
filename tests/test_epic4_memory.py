"""Epic 4 — memory that actually works, end to end.

E4-01 facade + native delegation + loud unavailability; E4-02
supersede-on-contradiction with history; E4-03 fact edit + export;
E4-04 embedding stats; E4-05 fast-chat learn parity; E4-06 external
adapters (stubbed libraries + the unavailable path); E4-07 briefing
budget, the master prompt's acceptance flow, and a seeded scale
check with recorded timings.
"""
import importlib.util
import json
import sqlite3
import sys
import time
import types
from unittest.mock import patch

import pytest

from aether_engine.providers.base import StreamChunk

# ----------------------------------------------------------------------
# Fixtures: isolated native stores (the pruning tests' recipe)
# ----------------------------------------------------------------------

@pytest.fixture()
def kg_db(tmp_path):
    db = tmp_path / "kg.db"
    with patch("aether_engine.memory.knowledge_graph.DB_PATH", db):
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


@pytest.fixture(autouse=True)
def _clear_backend_cache():
    from aether_engine.memory import facade

    facade._BACKEND_CACHE.clear()
    yield
    facade._BACKEND_CACHE.clear()


class FactProvider:
    """Extraction stub: answers by sniffing the conversation; the
    reconciliation prompt (it lists OLD facts) gets one pair."""

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        blob = json.dumps(messages)
        if "OLD facts:" in blob:
            yield StreamChunk(text='[{"old": 0, "new": 0}]', finish_reason="stop")
        elif "Mumbai" in blob:
            yield StreamChunk(
                text='[{"entity_name": "User", "entity_type": "Person", '
                     '"fact": "User lives in Mumbai", "confidence": 0.95}]',
                finish_reason="stop")
        else:
            yield StreamChunk(
                text='[{"entity_name": "User", "entity_type": "Person", '
                     '"fact": "User lives in Kolkata", "confidence": 0.9}]',
                finish_reason="stop")


# ----------------------------------------------------------------------
# E4-02 — supersede
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_fact_supersedes_contradicted_old_fact(kg_db):
    from aether_engine.memory import knowledge_graph as kg

    provider = FactProvider()
    await kg.extract_and_store_facts(provider, "I live in Kolkata", "Noted!", "t1")
    facts = kg.get_all_facts()
    assert [f["fact_text"] for f in facts] == ["User lives in Kolkata"]
    kolkata_id = facts[0]["fact_id"]

    await kg.extract_and_store_facts(provider, "I moved to Mumbai last week", "Congrats!", "t2")

    active = kg.get_all_facts()
    assert [f["fact_text"] for f in active] == ["User lives in Mumbai"], active
    history = kg.get_all_facts(include_history=True)
    assert len(history) == 2
    old = next(f for f in history if f["fact_id"] == kolkata_id)
    assert old["status"] == "superseded"
    assert old["superseded_by"] == active[0]["fact_id"]
    # Default retrieval (what the supervisor sees) never shows history.
    assert kg.search_facts("Kolkata") == []
    assert len(kg.search_facts("Kolkata", include_history=True)) == 1
    assert [f["fact_text"] for f in kg.get_entity_facts("User")] == ["User lives in Mumbai"]


@pytest.mark.asyncio
async def test_reconciliation_failure_keeps_everything_active(kg_db):
    from aether_engine.memory import knowledge_graph as kg

    class BrokenReconcileProvider(FactProvider):
        async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
            blob = json.dumps(messages)
            if "OLD facts:" in blob:
                raise RuntimeError("provider down")
            async for chunk in super().call_stream(messages, tools=tools, node_name=node_name, **kwargs):
                yield chunk

    provider = BrokenReconcileProvider()
    await kg.extract_and_store_facts(provider, "I live in Kolkata", "Noted!", "t1")
    await kg.extract_and_store_facts(provider, "I moved to Mumbai", "Ok!", "t2")
    # Both facts stored, neither superseded — no data loss, no crash.
    assert len(kg.get_all_facts(include_history=True)) == 2
    assert len(kg.get_all_facts()) == 2


# ----------------------------------------------------------------------
# E4-03 — update + export
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_fact_and_export(kg_db, epi_db):
    from aether_engine.memory import knowledge_graph as kg
    from aether_engine.memory.facade import NativeMemoryBackend

    provider = FactProvider()
    await kg.extract_and_store_facts(provider, "I live in Kolkata", "Noted!", "t1")
    fact_id = kg.get_all_facts()[0]["fact_id"]

    assert kg.update_fact(fact_id, "User lives in Kolkata, India") is True
    assert kg.get_all_facts()[0]["fact_text"] == "User lives in Kolkata, India"
    assert kg.update_fact(fact_id, "   ") is False
    assert kg.update_fact("no-such-id", "x") is False

    backend = NativeMemoryBackend()
    await backend.record_episode("task-x", "prompt", "summary", ["task"], "outcome")
    export = backend.export()
    assert export["backend"] == "native" and export["version"] == 1
    assert [f["fact_text"] for f in export["facts"]] == ["User lives in Kolkata, India"]
    assert [e["task_id"] for e in export["episodes"]] == ["task-x"]


# ----------------------------------------------------------------------
# E4-01 — facade behaviour
# ----------------------------------------------------------------------

def test_facade_native_default_and_unknown_backend():
    from aether_engine.config import UserConfig
    from aether_engine.memory.facade import (
        MemoryBackendUnavailable,
        NativeMemoryBackend,
        backend_or_none,
        get_memory_backend,
    )

    assert isinstance(get_memory_backend(UserConfig()), NativeMemoryBackend)
    with pytest.raises(MemoryBackendUnavailable):
        get_memory_backend(backend_name="bogus")
    # backend_or_none degrades loudly to None, never raises.
    assert backend_or_none(backend_name="bogus") is None


@pytest.mark.asyncio
async def test_acceptance_flow_task_facts_used_deleted_exported(kg_db, epi_db):
    """Master prompt acceptance: task -> facts appear; a later
    lookup visibly uses them; delete -> gone from retrieval;
    export -> contains what remains."""
    from aether_engine.langgraph.nodes.supervisor import _execute_supervisor_memory_tool
    from aether_engine.memory.facade import NativeMemoryBackend

    backend = NativeMemoryBackend()
    provider = FactProvider()
    await backend.learn(provider, "I live in Kolkata and work on Aether-OS",
                        "Got it.", "task-accept-1")

    result = await _execute_supervisor_memory_tool(
        "search_knowledge_graph", {"query": "Kolkata"}, None, "where do I live?", "task-accept-2")
    assert "Kolkata" in result

    fact_id = backend.list_facts()[0]["fact_id"]
    assert backend.delete_fact(fact_id) is True
    result_after = await _execute_supervisor_memory_tool(
        "search_knowledge_graph", {"query": "Kolkata"}, None, "where do I live?", "task-accept-3")
    assert "Kolkata" not in result_after

    await backend.learn(provider, "I live in Kolkata", "Noted.", "task-accept-4")
    export = backend.export()
    assert any("Kolkata" in f["fact_text"] for f in export["facts"])


# ----------------------------------------------------------------------
# E4-04 — embedding stats
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_embedding_stats_counts_pending(epi_db):
    from aether_engine.memory import episodic

    # No Ollama in this environment: embeddings fail and are counted
    # as pending, never as embedded.
    await episodic.store_episode("s1", "first task prompt", "sum", ["task"], "done")
    await episodic.store_episode("s2", "second task prompt", "sum", ["task"], "done")
    stats = episodic.embedding_stats()
    assert stats["episodes"] == 2
    assert stats["embedded"] == 0
    assert stats["pending"] == 2
    assert stats["model"] == episodic.EMBEDDING_MODEL


# ----------------------------------------------------------------------
# E4-05 — fast-chat learn parity
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fast_chat_exchange_is_learned(kg_db, epi_db):
    from aether_engine.langgraph.fast_chat import learn_fast_chat_exchange
    from aether_engine.memory import episodic
    from aether_engine.memory import knowledge_graph as kg

    await learn_fast_chat_exchange(
        "chat-task-1", "I live in Kolkata", "Noted — Kolkata it is.",
        FactProvider(), None)
    assert [f["fact_text"] for f in kg.get_all_facts()] == ["User lives in Kolkata"]
    episodes = episodic.get_all_episodes()
    assert [e["task_id"] for e in episodes] == ["chat-task-1"]
    assert "fast" in episodes[0]["tags"]


# ----------------------------------------------------------------------
# E4-07 — briefing budget
# ----------------------------------------------------------------------

def test_supervisor_memory_result_budget(kg_db):
    from aether_engine.langgraph.nodes.supervisor import (
        MEMORY_TOOL_RESULT_MAX_CHARS,
        _execute_supervisor_memory_tool,
    )
    from aether_engine.memory import knowledge_graph as kg

    kg.init_kg_db()
    import aether_engine.memory.knowledge_graph as kg_module

    conn = sqlite3.connect(kg_module.DB_PATH)
    conn.row_factory = sqlite3.Row
    now = int(time.time())
    conn.execute(
        "INSERT INTO entities (id, name, entity_type, first_seen, last_updated) VALUES ('e1', 'Budget', 'Concept', ?, ?)",
        (now, now))
    for i in range(5):
        conn.execute(
            "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, confidence) VALUES (?, 'e1', ?, 'seed', ?, 0.9)",
            (f"f{i}", "budgetword " + "x" * 900, now))
    conn.commit()
    conn.close()

    import asyncio

    result = asyncio.run(_execute_supervisor_memory_tool(
        "search_knowledge_graph", {"query": "budgetword"}, None, "budgetword", ""))
    assert len(result) <= MEMORY_TOOL_RESULT_MAX_CHARS + 60
    assert "truncated to budget" in result


# ----------------------------------------------------------------------
# E4-07 — scale check (numbers recorded in the PR summary)
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_retrieval_at_scale(kg_db, epi_db):
    from aether_engine.memory import episodic
    from aether_engine.memory import knowledge_graph as kg

    kg.init_kg_db()
    import aether_engine.memory.knowledge_graph as kg_module

    conn = sqlite3.connect(kg_module.DB_PATH)
    now = int(time.time())
    conn.execute(
        "INSERT INTO entities (id, name, entity_type, first_seen, last_updated) VALUES ('e1', 'User', 'Person', ?, ?)",
        (now, now))
    conn.executemany(
        "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, confidence) VALUES (?, 'e1', ?, 'seed', ?, 0.8)",
        [(f"fact-{i}", f"User fact number {i} about topic {i % 50}", now - i) for i in range(500)],
    )
    conn.commit()
    conn.close()

    episodic.init_episodic_db()

    conn = sqlite3.connect(epi_db)
    conn.executemany(
        "INSERT INTO episodic_memory (task_id, timestamp, user_prompt, task_summary, tags, outcome) VALUES (?, ?, ?, ?, ?, ?)",
        [(f"task-{i}", now - i, f"task about subsystem {i % 80} number {i}",
          "summary", "task", "completed fine") for i in range(2000)],
    )
    conn.commit()
    conn.close()

    start = time.perf_counter()
    facts = kg.search_facts("topic 42")
    fact_ms = (time.perf_counter() - start) * 1000
    assert facts, "seeded facts must be findable"

    start = time.perf_counter()
    episodes = await episodic.query_episodes("subsystem 42", limit=5)
    episode_ms = (time.perf_counter() - start) * 1000
    assert episodes, "seeded episodes must be findable"

    print(f"\n[scale] 500 facts search: {fact_ms:.1f}ms | 2000 episodes hybrid query: {episode_ms:.1f}ms")
    assert fact_ms < 2000
    assert episode_ms < 2000


# ----------------------------------------------------------------------
# E4-06 — external adapters
# ----------------------------------------------------------------------

def test_external_backends_unavailable_without_packages():
    from aether_engine.memory.facade import MemoryBackendUnavailable, get_memory_backend

    for name in ("mem0", "langmem", "graphiti"):
        if importlib.util.find_spec({"mem0": "mem0", "langmem": "langmem", "graphiti": "graphiti_core"}[name]) is not None:
            pytest.skip(f"{name} is installed in this environment")
        with pytest.raises(MemoryBackendUnavailable):
            get_memory_backend(backend_name=name)


def test_mem0_adapter_mapping_with_stubbed_library(monkeypatch):
    calls = []
    store = {}

    class FakeMem0:
        def add(self, messages, user_id=None, metadata=None, infer=True):
            calls.append(("add", messages, metadata, infer))
            mid = f"m{len(store)}"
            store[mid] = {"id": mid, "memory": messages[0]["content"],
                          "metadata": metadata or {}, "created_at": 1}
            return {"results": [{"id": mid}]}

        def search(self, query, user_id=None, limit=10):
            return {"results": [v for v in store.values() if query.lower() in v["memory"].lower()][:limit]}

        def get_all(self, user_id=None):
            return {"results": list(store.values())}

        def delete(self, mid):
            calls.append(("delete", mid))
            store.pop(mid, None)

        def update(self, mid, text):
            store[mid]["memory"] = text

        def delete_all(self, user_id=None):
            store.clear()

    fake_module = types.SimpleNamespace(Memory=FakeMem0)
    monkeypatch.setitem(sys.modules, "mem0", fake_module)

    from aether_engine.memory.backends.mem0_backend import Mem0MemoryBackend

    backend = Mem0MemoryBackend(memory=FakeMem0())
    import asyncio

    asyncio.run(backend.learn(None, "I live in Kolkata", "Noted", "t1"))
    assert calls[0][0] == "add"
    facts = backend.search_facts("Kolkata")
    assert facts and facts[0]["fact_text"] == "I live in Kolkata"
    assert backend.update_fact(facts[0]["fact_id"], "I live in Mumbai") is True
    assert backend.search_facts("Mumbai")
    assert backend.delete_fact(facts[0]["fact_id"]) is True


def test_langmem_adapter_round_trip_with_stubbed_package(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "langmem", types.SimpleNamespace())
    from aether_engine.memory.backends.langmem_backend import LangMemMemoryBackend

    store_path = tmp_path / "langmem.json"
    backend = LangMemMemoryBackend(store_path=store_path)
    import asyncio

    asyncio.run(backend.record_episode("ep1", "prompt one", "sum", ["chat"], "outcome one"))
    asyncio.run(backend.learn(FactProvider(), "I live in Kolkata", "Noted", "t1"))
    facts = backend.search_facts("Kolkata")
    assert facts and facts[0]["fact_text"] == "User lives in Kolkata"
    episodes = backend.list_episodes()
    assert [e["task_id"] for e in episodes] == ["ep1"]
    assert backend.update_fact(facts[0]["fact_id"], "User lives in Kolkata, India") is True
    assert "India" in backend.list_facts()[0]["fact_text"]
    # Persistence: a fresh backend over the same file sees the data.
    backend2 = LangMemMemoryBackend(store_path=store_path)
    assert backend2.list_facts()
    assert backend2.delete_fact(facts[0]["fact_id"]) is True
    assert backend2.list_facts() == []
    assert backend2.clear_episodes() is True
    assert backend2.list_episodes() == []


def test_graphiti_adapter_mapping_and_gates(monkeypatch):

    class FakeEdge:
        uuid = "edge-1"
        name = "LIVES_IN"
        fact = "User lives in Kolkata"

    class FakeGraphiti:
        def __init__(self):
            self.episodes = []

        async def add_episode(self, **kwargs):
            self.episodes.append(kwargs)

        async def search(self, query, num_results=10):
            return [FakeEdge()]

    nodes_module = types.SimpleNamespace(
        EpisodeType=types.SimpleNamespace(text="text"))
    fake_module = types.SimpleNamespace(Graphiti=FakeGraphiti)
    monkeypatch.setitem(sys.modules, "graphiti_core", fake_module)
    monkeypatch.setitem(sys.modules, "graphiti_core.nodes", nodes_module)

    from aether_engine.memory.backends.graphiti_backend import GraphitiMemoryBackend

    backend = GraphitiMemoryBackend(graphiti=FakeGraphiti())
    facts = backend.search_facts("Kolkata")
    assert facts[0]["fact_text"] == "User lives in Kolkata"
    import asyncio

    asyncio.run(backend.learn(None, "I live in Kolkata", "Noted", "t1"))
    with pytest.raises(NotImplementedError):
        backend.delete_fact("edge-1")

    # Without NEO4J_PASSWORD the default construction refuses loudly.
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    from aether_engine.memory.facade import MemoryBackendUnavailable

    with pytest.raises(MemoryBackendUnavailable, match="NEO4J_PASSWORD"):
        GraphitiMemoryBackend()
