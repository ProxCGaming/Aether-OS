"""E4-F04: native episode deletion commits one database before the
other can fail (full-product review, section F).

Episodic memory lives in two SQLite files: episodes + thinking in
``memory.db``, their vectors in ``embeddings.db``. ``delete_episode``,
``clear_all_episodes`` and the prune helper ``_delete_episode_rows``
each committed the memory.db deletes FIRST and the embeddings.db
delete SECOND, as two independent transactions. A failure at the
second commit (the audit injected ``sqlite3.OperationalError`` at
the embedding-delete connection, no concurrency) left the episode
and its thinking gone but the vector orphaned — stats went to
0 episodes / 1 embedded / pending = -1, and nothing could roll the
first commit back.

The fix deletes through ONE connection and ONE transaction (the
embeddings file ATTACHed to the episodic connection, mirroring the
E4-F03 fence), so a failure at any statement rolls back BOTH sides.
Fault injection here is at the statement level — real SQLite
triggers that RAISE(ABORT) on DELETE inside the target database —
not a mock of the delete functions.

Crash window, honestly bounded: memory.db is shared with the
knowledge-graph/session stores, which put it in WAL mode, and
SQLite does not promise cross-file atomicity for a transaction
spanning a WAL database if the process dies DURING the commit. The
pinned subprocess test covers a kill before the commit (nothing is
committed on either side, in any journal mode — uncommitted work is
never visible). The mid-commit WAL residue, if it ever occurred,
can only be an orphan vector (an episode without a vector is the
store's ordinary "unembedded" state and backfill rebuilds it), and
the init-time reconciliation sweep removes exactly those orphans —
pinned by the reconciliation test below, which also heals splits
left on disk by the pre-fix code.

Controls (pass before and after): a failure in the FIRST store
leaves both sides intact; a clean delete removes all three rows;
deleting a nonexistent episode behaves as today; pruning with no
fault still removes old episodes from both stores and keeps fresh
ones.
"""
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

from aether_engine.config import UserConfig
from aether_engine.embedding_codec import encode_embedding
from aether_engine.memory import episodic
from aether_engine.memory.episodic import (
    clear_all_episodes,
    delete_episode,
    embedding_stats,
    prune_episodes,
)

TASK = "task-e4f04"
TASK2 = "task-e4f04-b"
VEC = [1.0, 0.0]


@pytest.fixture
def isolated_episodic(tmp_path):
    """Point both episodic DBs at tmp files (the redirect pattern
    from test_epic4_e4f03_backfill_fences.py) and capture audit
    log calls."""
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
    stack.enter_context(patch("aether_engine.memory.episodic.EMBEDDINGS_DB_PATH", emb_db))
    stack.enter_context(patch("aether_engine.memory.episodic._get_conn", side_effect=fake_get_conn))
    mock_log = stack.enter_context(
        patch("aether_engine.memory.episodic._audit_logger.log_event")
    )
    try:
        yield {"mem_db": mem_db, "emb_db": emb_db, "log": mock_log}
    finally:
        stack.close()


def _seed_episode(task_id, *, timestamp=None, with_thinking=True):
    """Insert one episode + thinking + current-model vector directly."""
    episodic.init_episodic_db()
    episodic.init_embeddings_db()
    with episodic._get_conn() as conn:
        conn.execute(
            "INSERT INTO episodic_memory (task_id, timestamp, user_prompt, task_summary, tags, outcome)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, timestamp or int(time.time()), f"prompt {task_id}",
             f"summary {task_id}", "test", "SUCCESS"),
        )
        if with_thinking:
            conn.execute(
                "INSERT INTO episodic_thinking (task_id, thinking_log) VALUES (?, ?)",
                (task_id, f"thinking {task_id}"),
            )
        conn.commit()
    with episodic._get_conn(episodic.EMBEDDINGS_DB_PATH) as conn:
        conn.execute(
            "INSERT INTO episodic_embeddings (task_id, embedding, model, dimension)"
            " VALUES (?, ?, ?, ?)",
            (task_id, encode_embedding(VEC), episodic.resolve_embedder(), len(VEC)),
        )
        conn.commit()


def _rows(mem_db, emb_db, task_id):
    """(episode, thinking, embedding) presence for ``task_id`` from
    fresh raw connections — what any later reader would observe."""
    mem = sqlite3.connect(mem_db)
    try:
        episode = mem.execute(
            "SELECT COUNT(*) FROM episodic_memory WHERE task_id = ?",
            (task_id,)).fetchone()[0]
        thinking = mem.execute(
            "SELECT COUNT(*) FROM episodic_thinking WHERE task_id = ?",
            (task_id,)).fetchone()[0]
    finally:
        mem.close()
    emb = sqlite3.connect(emb_db)
    try:
        vector = emb.execute(
            "SELECT COUNT(*) FROM episodic_embeddings WHERE task_id = ?",
            (task_id,)).fetchone()[0]
    finally:
        emb.close()
    return episode, thinking, vector


def _inject_delete_trigger(db_file, table, trigger):
    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            f"CREATE TRIGGER {trigger} BEFORE DELETE ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'e4f04 injected delete failure'); END"
        )
        conn.commit()
    finally:
        conn.close()


def _drop_trigger(db_file, trigger):
    conn = sqlite3.connect(db_file)
    try:
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The defect: failure at the SECOND store splits the delete
# ---------------------------------------------------------------------------

def test_second_store_failure_rolls_back_whole_delete(isolated_episodic):
    mem_db, emb_db = isolated_episodic["mem_db"], isolated_episodic["emb_db"]
    _seed_episode(TASK)
    _inject_delete_trigger(emb_db, "episodic_embeddings", "e4f04_fail_emb")

    with pytest.raises(sqlite3.Error):
        delete_episode(TASK)

    # Pre-fix: the memory.db commit had already landed, so the
    # episode + thinking were gone and only the vector survived.
    assert _rows(mem_db, emb_db, TASK) == (1, 1, 1)
    stats = embedding_stats()
    assert stats["episodes"] == 1
    assert stats["embedded"] == 1
    assert stats["pending"] == 0

    # With the fault removed the same delete removes all three rows.
    _drop_trigger(emb_db, "e4f04_fail_emb")
    assert delete_episode(TASK) is True
    assert _rows(mem_db, emb_db, TASK) == (0, 0, 0)
    stats = embedding_stats()
    assert stats["episodes"] == 0
    assert stats["embedded"] == 0
    assert stats["pending"] == 0


def test_clear_all_failure_rolls_back_everything(isolated_episodic):
    mem_db, emb_db = isolated_episodic["mem_db"], isolated_episodic["emb_db"]
    _seed_episode(TASK)
    _seed_episode(TASK2)
    _inject_delete_trigger(emb_db, "episodic_embeddings", "e4f04_fail_emb")

    with pytest.raises(sqlite3.Error):
        clear_all_episodes()

    # Pre-fix: memory.db was wiped, both vectors orphaned.
    assert _rows(mem_db, emb_db, TASK) == (1, 1, 1)
    assert _rows(mem_db, emb_db, TASK2) == (1, 1, 1)

    _drop_trigger(emb_db, "e4f04_fail_emb")
    assert clear_all_episodes() is True
    assert _rows(mem_db, emb_db, TASK) == (0, 0, 0)
    assert _rows(mem_db, emb_db, TASK2) == (0, 0, 0)


def test_prune_failure_rolls_back_both_stores(isolated_episodic):
    mem_db, emb_db = isolated_episodic["mem_db"], isolated_episodic["emb_db"]
    now = int(time.time())
    _seed_episode(TASK, timestamp=now - 40 * 86400)   # prunable by age
    _seed_episode(TASK2, timestamp=now)               # fresh, kept
    _inject_delete_trigger(emb_db, "episodic_embeddings", "e4f04_fail_emb")

    with pytest.raises(sqlite3.Error):
        prune_episodes(retention_days=30, max_episodes=0,
                       config=UserConfig(), now=now)

    # Pre-fix: the age-pruned episode's memory rows were committed
    # away before the embedding delete failed.
    assert _rows(mem_db, emb_db, TASK) == (1, 1, 1)
    assert _rows(mem_db, emb_db, TASK2) == (1, 1, 1)

    _drop_trigger(emb_db, "e4f04_fail_emb")
    result = prune_episodes(retention_days=30, max_episodes=0,
                            config=UserConfig(), now=now)
    assert result["pruned_episodes"] == 1
    assert result["pruned_embedding_rows"] == 1
    assert _rows(mem_db, emb_db, TASK) == (0, 0, 0)
    assert _rows(mem_db, emb_db, TASK2) == (1, 1, 1)


# ---------------------------------------------------------------------------
# Controls — pass before and after
# ---------------------------------------------------------------------------

def test_first_store_failure_leaves_both_sides_intact(isolated_episodic):
    mem_db, emb_db = isolated_episodic["mem_db"], isolated_episodic["emb_db"]
    _seed_episode(TASK)
    # episodic_thinking lives in memory.db — the FIRST store — and
    # shares its transaction with the FTS delete, so failing there
    # must leave everything (including the vector) untouched.
    _inject_delete_trigger(mem_db, "episodic_thinking", "e4f04_fail_think")

    with pytest.raises(sqlite3.Error):
        delete_episode(TASK)

    assert _rows(mem_db, emb_db, TASK) == (1, 1, 1)
    _drop_trigger(mem_db, "e4f04_fail_think")


def test_clean_delete_removes_all_three_rows(isolated_episodic):
    mem_db, emb_db = isolated_episodic["mem_db"], isolated_episodic["emb_db"]
    _seed_episode(TASK)
    assert delete_episode(TASK) is True
    assert _rows(mem_db, emb_db, TASK) == (0, 0, 0)
    stats = embedding_stats()
    assert stats["episodes"] == 0
    assert stats["embedded"] == 0
    assert stats["pending"] == 0


def test_delete_nonexistent_episode_behaves_as_today(isolated_episodic):
    mem_db, emb_db = isolated_episodic["mem_db"], isolated_episodic["emb_db"]
    _seed_episode(TASK)
    assert delete_episode("task-never-existed") is True
    assert _rows(mem_db, emb_db, TASK) == (1, 1, 1)


# ---------------------------------------------------------------------------
# Reconciliation: an orphan vector (the crash-split / legacy residue)
# is removed at init; legitimate rows are untouched
# ---------------------------------------------------------------------------

def test_orphan_embedding_is_reconciled_on_init(isolated_episodic):
    mem_db, emb_db = isolated_episodic["mem_db"], isolated_episodic["emb_db"]
    _seed_episode(TASK)
    _seed_episode(TASK2)
    # Simulate the split state a mid-commit crash (or the pre-fix
    # two-commit delete) leaves behind: episode + thinking deleted
    # straight out of memory.db, vector left behind.
    conn = sqlite3.connect(mem_db)
    try:
        conn.execute("DELETE FROM episodic_memory WHERE task_id = ?", (TASK,))
        conn.execute("DELETE FROM episodic_thinking WHERE task_id = ?", (TASK,))
        conn.commit()
    finally:
        conn.close()
    assert _rows(mem_db, emb_db, TASK) == (0, 0, 1)
    stats = embedding_stats()
    assert stats["episodes"] == 1
    assert stats["embedded"] == 2
    assert stats["pending"] == -1

    # A fresh process runs the sweep at init. The guard is
    # process-local, so reset it to simulate the restart in-process
    # (the same technique the KG reopen checks use).
    guard = getattr(episodic, "_RECONCILED_DB_PAIRS", None)
    if guard is not None:
        guard.clear()
    episodic.init_episodic_db()

    assert _rows(mem_db, emb_db, TASK) == (0, 0, 0)
    # The legitimate episode and its vector are untouched.
    assert _rows(mem_db, emb_db, TASK2) == (1, 1, 1)
    stats = embedding_stats()
    assert stats["episodes"] == 1
    assert stats["embedded"] == 1
    assert stats["pending"] == 0


# ---------------------------------------------------------------------------
# Process-level: killed mid-delete, before the commit — nothing lands
# ---------------------------------------------------------------------------

_CHILD_SCRIPT = r'''
import os
import sqlite3
import sys
from pathlib import Path

from aether_engine.memory import episodic

mem_db = Path(sys.argv[1])
emb_db = Path(sys.argv[2])
task_id = sys.argv[3]

def _is_episode_vector_delete(sql):
    # The delete path's own vector statement — not the init-time
    # reconciliation sweep (whose DELETE carries a NOT IN subquery,
    # not "task_id =").
    return (
        isinstance(sql, str)
        and "episodic_embeddings" in sql
        and "task_id =" in sql
        and sql.lstrip().upper().startswith("DELETE")
    )


class _DyingCursor:
    """Delegating cursor that kills the process at the vector
    delete (sqlite3.Connection is an immutable C type, so the
    injection wraps the connections _get_conn hands out instead)."""

    def __init__(self, real):
        self._real = real

    def execute(self, sql, parameters=(), /):
        if _is_episode_vector_delete(sql):
            os._exit(97)  # process death at the second store's delete
        return self._real.execute(sql, parameters)

    def executemany(self, sql, seq, /):
        if _is_episode_vector_delete(sql):
            os._exit(97)
        return self._real.executemany(sql, seq)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __iter__(self):
        return iter(self._real)


class _DyingConnection:
    def __init__(self, real):
        self._real = real

    def execute(self, sql, parameters=(), /):
        if _is_episode_vector_delete(sql):
            os._exit(97)
        return self._real.execute(sql, parameters)

    def executemany(self, sql, seq, /):
        if _is_episode_vector_delete(sql):
            os._exit(97)
        return self._real.executemany(sql, seq)

    def cursor(self):
        return _DyingCursor(self._real.cursor())

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)


_orig_get_conn = episodic._get_conn
_real_db = episodic.DB_PATH
_real_emb_db = episodic.EMBEDDINGS_DB_PATH


def _fake_get_conn(db_path=None):
    if db_path is None or db_path in (_real_db, mem_db):
        return _DyingConnection(_orig_get_conn(mem_db))
    if db_path in (_real_emb_db, emb_db):
        return _DyingConnection(_orig_get_conn(emb_db))
    return _DyingConnection(_orig_get_conn(db_path))


episodic.DB_PATH = mem_db
episodic.EMBEDDINGS_DB_PATH = emb_db
episodic._get_conn = _fake_get_conn

episodic.delete_episode(task_id)
'''


def test_process_killed_mid_delete_leaves_consistent_state(tmp_path):
    mem_db = tmp_path / "memory.db"
    emb_db = tmp_path / "embeddings.db"

    # Seed the files through the engine's own initialisers.
    real_db = episodic.DB_PATH
    real_emb_db = episodic.EMBEDDINGS_DB_PATH
    orig_get_conn = episodic._get_conn

    def fake_get_conn(db_path=None):
        if db_path is None or db_path in (real_db, mem_db):
            return orig_get_conn(mem_db)
        if db_path in (real_emb_db, emb_db):
            return orig_get_conn(emb_db)
        return orig_get_conn(db_path)

    with patch("aether_engine.memory.episodic.DB_PATH", mem_db), \
            patch("aether_engine.memory.episodic.EMBEDDINGS_DB_PATH", emb_db), \
            patch("aether_engine.memory.episodic._get_conn", side_effect=fake_get_conn):
        _seed_episode(TASK)
    assert _rows(mem_db, emb_db, TASK) == (1, 1, 1)

    script = tmp_path / "e4f04_child.py"
    script.write_text(_CHILD_SCRIPT)
    repo_root = str(Path(episodic.__file__).resolve().parents[2])
    env = dict(os.environ)
    env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")
    env["HOME"] = str(tmp_path / "home")
    proc = subprocess.run(
        [sys.executable, str(script), str(mem_db), str(emb_db), TASK],
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 97, proc.stderr

    # Pre-fix: memory.db had already committed, so the child died
    # with the episode gone and the vector orphaned. Post-fix the
    # delete is one transaction and the kill lands before its only
    # commit: a fresh reader sees the episode fully intact.
    assert _rows(mem_db, emb_db, TASK) == (1, 1, 1)
