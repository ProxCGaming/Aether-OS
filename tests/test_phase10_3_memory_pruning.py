"""Phase 10.3 A1: memory retention — every tier must prune.

Before the fix there was no pruning, decay, or TTL in any tier: no
``prune_*`` function existed anywhere in ``aether_engine/memory/``, so
every test in this file failed pre-fix with ImportError, and the
underlying stores grew without bound.

These tests insert rows past the retention bound (direct SQL, so no
network embedding is needed), run pruning, and assert the old rows are
gone, recent rows remain, the knowledge-graph sole-fact guard holds, and
pruning is logged to the audit log. All DBs are tmp files — nothing here
touches the real ~/.aether/*.db.
"""
import pickle
import sqlite3
import time
import uuid
from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from aether_engine.config import UserConfig, load_config, save_config
from aether_engine.memory import episodic, knowledge_graph, session_store
from aether_engine.memory.pruning import prune_all
from aether_engine.scheduler.capability_jobs import CapabilitySchedulerManager

DAY = 86400
NOW = int(time.time())


@pytest.fixture
def isolated_memory(tmp_path):
    """Point all three memory tiers at tmp DBs and capture audit logs.

    Production puts episodic + KG + sessions in one file (~/.aether/
    memory.db) with embeddings in ~/.aether/embeddings.db; the fixture
    mirrors that: one tmp memory.db shared by all three tiers, one tmp
    embeddings.db. ``_get_conn`` binds DB_PATH as a default arg at import
    time, so patching the episodic constants alone does not redirect it —
    redirect explicitly, as in test_phase10_1_embedding_failure.py.
    Session store's ``_initialized`` guard is process-global, so reset it
    (otherwise a fresh tmp DB is skipped as "already initialised" — the
    pre-existing test_session_store failure mode).
    """
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

    session_store._initialized = False
    stack = ExitStack()
    stack.enter_context(patch("aether_engine.memory.episodic.DB_PATH", mem_db))
    stack.enter_context(patch("aether_engine.memory.episodic.EMBEDDINGS_DB_PATH", emb_db))
    stack.enter_context(patch("aether_engine.memory.episodic._get_conn", side_effect=fake_get_conn))
    stack.enter_context(patch("aether_engine.memory.knowledge_graph.DB_PATH", mem_db))
    stack.enter_context(patch("aether_engine.memory.session_store.DB_PATH", mem_db))
    ep_log = stack.enter_context(patch("aether_engine.memory.episodic._audit_logger.log_event"))
    kg_log = stack.enter_context(patch("aether_engine.memory.knowledge_graph._audit_logger.log_event"))
    sess_log = stack.enter_context(patch("aether_engine.memory.session_store._audit_logger.log_event"))
    prune_log = stack.enter_context(patch("aether_engine.memory.pruning._audit_logger.log_event"))
    try:
        yield {
            "mem_db": mem_db,
            "emb_db": emb_db,
            "ep_log": ep_log,
            "kg_log": kg_log,
            "sess_log": sess_log,
            "prune_log": prune_log,
        }
    finally:
        stack.close()
        session_store._initialized = False


def _audit_calls(mock_log, action):
    return [c for c in mock_log.call_args_list if c.args and c.args[0] == action]


# --- helpers: insert rows directly (no network embedding) -------------------

def _insert_episode(task_id, age_days, with_thinking=True, with_embedding=True):
    episodic.init_episodic_db()
    episodic.init_embeddings_db()
    ts = NOW - int(age_days * DAY)
    with episodic._get_conn() as conn:
        conn.execute(
            "INSERT INTO episodic_memory (task_id, timestamp, user_prompt, task_summary, tags, outcome)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, ts, f"prompt {task_id}", f"summary {task_id}", "test", "SUCCESS"),
        )
        if with_thinking:
            conn.execute(
                "INSERT INTO episodic_thinking (task_id, thinking_log) VALUES (?, ?)",
                (task_id, "thinking"),
            )
        conn.commit()
    if with_embedding:
        with episodic._get_conn(episodic.EMBEDDINGS_DB_PATH) as e_conn:
            e_conn.execute(
                "INSERT INTO episodic_embeddings (task_id, embedding) VALUES (?, ?)",
                (task_id, pickle.dumps([0.1, 0.2, 0.3])),
            )
            e_conn.commit()


def _episode_task_ids():
    with episodic._get_conn() as conn:
        return {r[0] for r in conn.execute("SELECT task_id FROM episodic_memory")}


def _embedding_task_ids(emb_db):
    conn = sqlite3.connect(emb_db)
    try:
        return {r[0] for r in conn.execute("SELECT task_id FROM episodic_embeddings")}
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()


def _insert_fact(entity_name, fact_text, age_days):
    knowledge_graph.init_kg_db()
    now = NOW - int(age_days * DAY)
    with knowledge_graph._get_conn() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT id FROM entities WHERE name = ?", (entity_name,)).fetchone()
        if row:
            entity_id = row[0]
        else:
            entity_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO entities (id, name, entity_type, first_seen, last_updated) VALUES (?, ?, ?, ?, ?)",
                (entity_id, entity_name, "Concept", now, now),
            )
        cur.execute(
            "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, confidence)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), entity_id, fact_text, "task-x", now, 0.9),
        )
        conn.commit()


def _facts_by_entity():
    facts = knowledge_graph.get_all_facts(limit=1000)
    by_entity = {}
    for f in facts:
        by_entity.setdefault(f["entity_name"], []).append(f["fact_text"])
    return by_entity


def _entities():
    with knowledge_graph._get_conn() as conn:
        return {r[0] for r in conn.execute("SELECT name FROM entities")}


def _insert_session(title, age_days, n_messages=2):
    session_id = session_store.create_session(title)
    for i in range(n_messages):
        session_store.add_message(session_id, "user", f"message {i}")
    # Backdate last activity (create/add use now).
    ts = NOW - int(age_days * DAY)
    with session_store._get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET created_at = ?, updated_at = ? WHERE id = ?",
            (ts, ts, session_id),
        )
        conn.commit()
    return session_id


def _session_message_count(session_id):
    with session_store._get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM session_messages WHERE session_id = ?", (session_id,)
        ).fetchone()[0]


# --- episodic ----------------------------------------------------------------

def test_episodic_age_pruning_removes_old_keeps_recent(isolated_memory):
    _insert_episode("task-old", age_days=200)
    _insert_episode("task-recent", age_days=1)
    assert _episode_task_ids() == {"task-old", "task-recent"}
    assert _embedding_task_ids(isolated_memory["emb_db"]) == {"task-old", "task-recent"}

    result = episodic.prune_episodes(retention_days=90, max_episodes=0)

    assert result["pruned_episodes"] == 1
    assert _episode_task_ids() == {"task-recent"}
    # The pruned episode loses its thinking + embedding rows too.
    assert _embedding_task_ids(isolated_memory["emb_db"]) == {"task-recent"}
    with episodic._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM episodic_thinking").fetchone()[0] == 1

    pruned = _audit_calls(isolated_memory["ep_log"], "EPISODIC_MEMORY_PRUNED")
    assert len(pruned) == 1
    assert pruned[0].args[1]["pruned_episodes"] == 1


def test_episodic_count_cap_keeps_newest(isolated_memory):
    for i, age in enumerate([5, 4, 3, 2, 1]):
        _insert_episode(f"task-{i}", age_days=age)

    result = episodic.prune_episodes(retention_days=0, max_episodes=3)

    assert result["pruned_episodes"] == 2
    assert _episode_task_ids() == {"task-2", "task-3", "task-4"}


def test_episodic_pruning_disabled_when_bounds_zero(isolated_memory):
    _insert_episode("task-old", age_days=200)

    result = episodic.prune_episodes(retention_days=0, max_episodes=0)

    assert result["pruned_episodes"] == 0
    assert _episode_task_ids() == {"task-old"}


# --- knowledge graph ---------------------------------------------------------

def test_kg_age_pruning_keeps_sole_fact_of_entity(isolated_memory):
    _insert_fact("Solo Entity", "only fact, very old", age_days=400)
    _insert_fact("Multi Entity", "old fact one", age_days=400)
    _insert_fact("Multi Entity", "old fact two", age_days=400)
    _insert_fact("Fresh Entity", "recent fact", age_days=10)

    result = knowledge_graph.prune_facts(retention_days=365, max_facts=0)

    assert result["pruned_facts"] == 2
    assert result["kept_referenced"] == 1
    assert result["pruned_entities"] == 1
    by_entity = _facts_by_entity()
    # The sole fact of Solo Entity survives: deleting it would erase the
    # entity's only record. Both of Multi Entity's facts go, and the
    # now-empty entity is removed with them.
    assert by_entity == {
        "Solo Entity": ["only fact, very old"],
        "Fresh Entity": ["recent fact"],
    }
    assert _entities() == {"Solo Entity", "Fresh Entity"}

    pruned = _audit_calls(isolated_memory["kg_log"], "KNOWLEDGE_GRAPH_PRUNED")
    assert len(pruned) == 1
    assert pruned[0].args[1]["pruned_facts"] == 2
    assert pruned[0].args[1]["kept_referenced"] == 1


def test_kg_count_cap_keeps_newest(isolated_memory):
    _insert_fact("Capped", "oldest fact", age_days=30)
    _insert_fact("Capped", "middle fact", age_days=20)
    _insert_fact("Capped", "newest fact", age_days=10)
    _insert_fact("Other", "other recent fact", age_days=5)

    result = knowledge_graph.prune_facts(retention_days=0, max_facts=2)

    assert result["pruned_facts"] == 2
    assert result["kept_referenced"] == 0
    assert _facts_by_entity() == {
        "Capped": ["newest fact"],
        "Other": ["other recent fact"],
    }


# --- sessions ----------------------------------------------------------------

def test_session_age_pruning_deletes_session_and_messages(isolated_memory):
    old_id = _insert_session("Old Chat", age_days=200, n_messages=2)
    new_id = _insert_session("New Chat", age_days=1, n_messages=2)

    result = session_store.prune_sessions(retention_days=180, max_sessions=0)

    assert result["pruned_sessions"] == 1
    assert result["pruned_messages"] == 2
    assert session_store.get_session(old_id) is None
    assert _session_message_count(old_id) == 0  # cascade, no orphans
    assert session_store.get_session(new_id) is not None
    assert len(session_store.get_session(new_id)["messages"]) == 2

    pruned = _audit_calls(isolated_memory["sess_log"], "SESSIONS_PRUNED")
    assert len(pruned) == 1
    assert pruned[0].args[1]["pruned_sessions"] == 1


def test_session_count_cap_keeps_newest(isolated_memory):
    ids = [
        _insert_session(f"Chat {i}", age_days=age)
        for i, age in enumerate([3, 2, 1])
    ]

    result = session_store.prune_sessions(retention_days=0, max_sessions=2)

    assert result["pruned_sessions"] == 1
    assert session_store.get_session(ids[0]) is None
    assert session_store.get_session(ids[1]) is not None
    assert session_store.get_session(ids[2]) is not None


# --- prune_all + config ------------------------------------------------------

def test_prune_all_runs_all_tiers_and_logs(isolated_memory):
    _insert_episode("task-old", age_days=200)
    _insert_fact("Multi Entity", "old fact one", age_days=400)
    _insert_fact("Multi Entity", "old fact two", age_days=400)
    old_session = _insert_session("Old Chat", age_days=200, n_messages=1)
    cfg = UserConfig(
        episodic_retention_days=90, episodic_max_episodes=0,
        kg_fact_retention_days=365, kg_max_facts=0,
        session_retention_days=180, session_max_sessions=0,
    )

    results = prune_all(config=cfg)

    assert results["episodic"]["pruned_episodes"] == 1
    assert results["knowledge_graph"]["pruned_facts"] == 2
    assert results["sessions"]["pruned_sessions"] == 1
    assert session_store.get_session(old_session) is None
    logged = _audit_calls(isolated_memory["prune_log"], "MEMORY_PRUNED_ALL")
    assert len(logged) == 1
    assert logged[0].args[1]["episodic"]["pruned_episodes"] == 1


def test_retention_defaults_are_documented_and_configurable(tmp_path, isolated_memory):
    cfg = UserConfig()
    assert cfg.episodic_retention_days == 90
    assert cfg.episodic_max_episodes == 1000
    assert cfg.kg_fact_retention_days == 365
    assert cfg.kg_max_facts == 5000
    assert cfg.session_retention_days == 180
    assert cfg.session_max_sessions == 200

    # Config round-trips through config.json (load_config default path is
    # already isolated to a tmp file by conftest).
    custom = UserConfig(episodic_retention_days=10, episodic_max_episodes=0)
    path = tmp_path / "config.json"
    save_config(custom, path=path)
    assert load_config(path=path).episodic_retention_days == 10

    # And a prune call with no explicit bounds reads them from the config,
    # not from a hardcoded number in the memory module.
    _insert_episode("task-20d", age_days=20)
    result = episodic.prune_episodes(config=custom)
    assert result["pruned_episodes"] == 1
    assert result["retention_days"] == 10


def test_scheduler_registers_weekly_prune_job(tmp_path, isolated_memory):
    # APScheduler is not installed in this Linux venv (HAS_APSCHEDULER is
    # False here; requirements.txt declares APScheduler>=3.10.4 for the
    # dev machine), so stub the trigger class and verify the wiring:
    # job id, weekly interval, replace_existing.
    class FakeTrigger:
        def __init__(self, **kwargs):
            self.interval = timedelta(**kwargs)

    manager = CapabilitySchedulerManager(db_path=tmp_path / "cap.db")
    fake_scheduler = MagicMock()
    fake_scheduler.running = True
    manager.scheduler = fake_scheduler

    with patch("aether_engine.scheduler.capability_jobs.HAS_APSCHEDULER", True), patch(
        "aether_engine.scheduler.capability_jobs.IntervalTrigger", FakeTrigger
    ):
        manager._register_memory_prune_job()

    fake_scheduler.add_job.assert_called_once()
    _, kwargs = fake_scheduler.add_job.call_args
    assert kwargs["id"] == "memory_prune_weekly"
    assert kwargs["replace_existing"] is True
    assert kwargs["trigger"].interval == timedelta(weeks=1)


def test_scheduler_run_memory_prune_now_prunes_all_tiers(tmp_path, isolated_memory):
    _insert_episode("task-old", age_days=200)
    manager = CapabilitySchedulerManager(db_path=tmp_path / "cap.db")

    results = manager.run_memory_prune_now()

    # Defaults from load_config (conftest-isolated config): 90d episodic.
    assert results["episodic"]["pruned_episodes"] == 1
    assert _episode_task_ids() == set()
