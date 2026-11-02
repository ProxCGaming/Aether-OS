"""Phase 10.1, A3: knowledge-graph extraction must run on the normal path.

Regression test for the audit finding in critical/02 #1: ``executor.py``
called ``knowledge_graph.extract_and_store_facts`` only on the resumed-task
path (``resume_langgraph_task``); the normal completion path in
``run_langgraph_task`` stored only the episodic episode, so a task that ran
to completion once left no facts in the knowledge graph. This test runs a
task to normal completion (fake graph, fake provider) and then queries the
knowledge graph: at least one fact must have been stored. It failed before
the fix (zero facts) and passes after.
"""
from types import SimpleNamespace

import aether_engine.langgraph.executor as executor_mod
import aether_engine.memory.episodic as episodic_mod
import aether_engine.memory.knowledge_graph as kg_mod
from aether_common.contracts import EventType
from aether_engine.langgraph.executor import run_langgraph_task
from aether_engine.memory.knowledge_graph import get_all_facts


class _Chunk:
    def __init__(self, text):
        self.text = text


class _FakeProvider:
    """Provider whose extraction call returns one fact as JSON."""

    async def call_stream(self, messages):
        yield _Chunk(
            '[{"entity_name": "User", "entity_type": "Person", '
            '"fact": "User prefers Python for scripting", "confidence": 0.9}]'
        )


class _CompletingGraph:
    """Fake compiled graph that yields one assistant message, then ends."""

    async def astream(self, *args, **kwargs):
        yield {
            "assistant": {
                "messages": [
                    {"role": "assistant", "content": "I prefer Python for scripting."}
                ]
            }
        }

    async def aget_state(self, config):
        return SimpleNamespace(values={})


async def test_normal_completion_stores_knowledge_graph_fact(tmp_path, monkeypatch):
    """Normal completion -> TASK_COMPLETED and >=1 fact in the KG."""
    monkeypatch.setattr(
        executor_mod, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db"
    )
    monkeypatch.setattr(
        executor_mod, "compile_graph", lambda checkpointer=None: _CompletingGraph()
    )
    # Isolate both memory tiers off the real ~/.aether/memory.db.
    memory_db = tmp_path / "memory.db"
    monkeypatch.setattr(kg_mod, "DB_PATH", memory_db)
    monkeypatch.setattr(episodic_mod, "DB_PATH", memory_db)
    monkeypatch.setattr(
        episodic_mod, "EMBEDDINGS_DB_PATH", tmp_path / "embeddings.db"
    )

    events = [ev async for ev in run_langgraph_task(
        "I really like coding in Python.",
        _FakeProvider(),
        task_id="task-kg-normal",
        request_id="req-kg-1",
        session_id="test-session",
    )]
    types = [ev.type for ev in events]

    assert types.count(EventType.TASK_COMPLETED) == 1
    assert EventType.TASK_FAILED not in types

    facts = get_all_facts(limit=10)
    assert len(facts) >= 1
    assert any(
        f["fact_text"] == "User prefers Python for scripting" for f in facts
    )
    assert all(f["entity_name"] == "User" for f in facts)
