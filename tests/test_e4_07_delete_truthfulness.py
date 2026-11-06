"""E4-07: the native stores' delete paths must tell the truth.

Episodic ``delete_episode`` discarded the counts
``_delete_episode_data`` returns, then unconditionally logged
``EPISODIC_MEMORY_DELETED`` and returned True. The knowledge graph's
``delete_fact`` / ``delete_entity`` executed their DELETEs, never
checked ``cur.rowcount``, and likewise logged DELETED + returned
True unconditionally — in pointed contrast to ``update_fact`` in the
same file, which checks rowcount and logs only when changed.

Deleting an id that never existed therefore wrote a DELETED event
into the audit log — the forensic record the rest of this epic
family works hard to keep truthful — for a record that was never
there, and no caller could distinguish "deleted" from "was never
there". The fix: a delete reports (and logs) success only when a
row actually went away.
"""
import sqlite3
import time
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from aether_engine.embedding_codec import encode_embedding
from aether_engine.memory import episodic, knowledge_graph

# ---------------------------------------------------------------------------
# Episodic (fixture recipe from test_epic4_e4f04_delete_atomicity.py)
# ---------------------------------------------------------------------------

TASK = "task-e4-07"


@pytest.fixture
def isolated_episodic(tmp_path):
    mem_db = tmp_path / "memory.db"
    emb_db = tmp_path / "embeddings.db"
    real_db = episodic.DB_PATH
    real_emb_db = episodic.EMBEDDINGS_DB_PATH
    orig_get_conn = episodic._get_conn

    def fake_get_conn(db_path=None):
        if db_path is None or db_path in (real_db, mem_db):
            return orig_get_conn(mem_db)
        if db_path in (real_emb_db, emb_db):
            return orig_get_conn(emb_db)
        return orig_get_conn(db_path)

    stack = ExitStack()
    stack.enter_context(patch("aether_engine.memory.episodic.DB_PATH", mem_db))
    stack.enter_context(
        patch("aether_engine.memory.episodic.EMBEDDINGS_DB_PATH", emb_db))
    stack.enter_context(
        patch("aether_engine.memory.episodic._get_conn", side_effect=fake_get_conn))
    mock_log = stack.enter_context(
        patch("aether_engine.memory.episodic._audit_logger.log_event"))
    try:
        yield {"mem_db": mem_db, "emb_db": emb_db, "log": mock_log}
    finally:
        stack.close()


def _seed_episode(task_id):
    episodic.init_episodic_db()
    episodic.init_embeddings_db()
    with episodic._get_conn() as conn:
        conn.execute(
            "INSERT INTO episodic_memory (task_id, timestamp, user_prompt, task_summary, tags, outcome)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, int(time.time()), f"prompt {task_id}",
             f"summary {task_id}", "test", "SUCCESS"),
        )
        conn.commit()
    with episodic._get_conn(episodic.EMBEDDINGS_DB_PATH) as conn:
        conn.execute(
            "INSERT INTO episodic_embeddings (task_id, embedding, model, dimension)"
            " VALUES (?, ?, ?, ?)",
            (task_id, encode_embedding([1.0, 0.0]),
             episodic.resolve_embedder(), 2),
        )
        conn.commit()


def _events(mock_log, name):
    return [c for c in mock_log.call_args_list if c.args and c.args[0] == name]


def test_delete_episode_of_missing_id_reports_not_deleted(isolated_episodic):
    _seed_episode(TASK)

    assert episodic.delete_episode("task-never-existed") is False
    assert _events(isolated_episodic["log"], "EPISODIC_MEMORY_DELETED") == []
    # The real episode is untouched.
    assert episodic.delete_episode(TASK) is True
    assert len(_events(isolated_episodic["log"], "EPISODIC_MEMORY_DELETED")) == 1


# ---------------------------------------------------------------------------
# Knowledge graph
# ---------------------------------------------------------------------------


@pytest.fixture
def kg_env(tmp_path):
    db = tmp_path / "kg.db"
    stack = ExitStack()
    stack.enter_context(
        patch("aether_engine.memory.knowledge_graph.DB_PATH", db))
    mock_log = stack.enter_context(
        patch("aether_engine.memory.knowledge_graph._audit_logger.log_event"))
    try:
        yield {"db": db, "log": mock_log}
    finally:
        stack.close()


def _seed_kg(db):
    knowledge_graph.init_kg_db()
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO entities (id, name, entity_type, first_seen, last_updated)"
            " VALUES ('e1', 'User', 'Person', 1000, 1000)")
        conn.execute(
            "INSERT INTO entities (id, name, entity_type, first_seen, last_updated)"
            " VALUES ('e2', 'Empty Entity', 'Person', 1000, 1000)")
        conn.execute(
            "INSERT INTO facts (id, entity_id, fact_text, source_task_id,"
            " timestamp, confidence, status, superseded_by)"
            " VALUES ('f1', 'e1', 'User lives in Kolkata', 't1', 1000, 0.9,"
            " 'active', NULL)")
        conn.commit()
    finally:
        conn.close()


def test_delete_fact_of_missing_id_reports_not_deleted(kg_env):
    _seed_kg(kg_env["db"])

    assert knowledge_graph.delete_fact("fact-never-existed") is False
    assert _events(kg_env["log"], "KNOWLEDGE_FACT_DELETED") == []

    assert knowledge_graph.delete_fact("f1") is True
    assert len(_events(kg_env["log"], "KNOWLEDGE_FACT_DELETED")) == 1


def test_delete_entity_of_missing_name_reports_not_deleted(kg_env):
    _seed_kg(kg_env["db"])

    assert knowledge_graph.delete_entity("Nobody") is False
    assert _events(kg_env["log"], "KNOWLEDGE_ENTITY_DELETED") == []

    assert knowledge_graph.delete_entity("Empty Entity") is True
    assert len(_events(kg_env["log"], "KNOWLEDGE_ENTITY_DELETED")) == 1
