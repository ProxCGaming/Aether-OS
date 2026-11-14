"""Master-build S1: WAL rollout to the four remaining stores.

The N-07 fix (``knowledge_graph._ensure_wal_mode``, ``5e677a7e``)
made the knowledge graph's first open tolerant of the WAL
transition race: switching a fresh DB to WAL needs exclusive
access and SQLite does not apply busy_timeout to journal-mode
changes, so a connection that asked while another held a
transaction got "database is locked" immediately, and the error
escaped ``_get_conn`` — on the session store's path, that is the
user's conversation history losing a batch on concurrent first
open. Four stores still ran the bare pragma on every connection:

- ``aether_engine/memory/session_store.py``
- ``aether_engine/rag/store.py``
- ``aether_engine/rewind/fork.py``
- ``aether_engine/tools/approval_cache.py``

Each now carries the same tolerant ``_ensure_wal_mode`` (replicated
per store, as the calendar store did — the graph's helper cannot
be imported from the rag/tools layers without dragging the memory
layer's provider/config chain into stores that are deliberately
standalone). These tests mirror the KG harness in
``test_wave2_memory_remainder.py::test_get_conn_survives_concurrent_first_open``
per store (holder transaction + open through the store's own
connection path), plus a concurrent-first-open write test per
store: threads opening a fresh DB simultaneously must raise
nothing and lose no writes.
"""

from __future__ import annotations

import sqlite3
import threading
import time

from aether_engine.memory import session_store
from aether_engine.rag.store import WorkspaceRagStore
from aether_engine.rewind import fork
from aether_engine.tools.approval_cache import ApprovalCache

HOLDER_SECONDS = 0.4
THREADS = 6


def _start_holder(db_path):
    """Open a write transaction on a fresh file and hold it.

    Returns the releaser thread (already started); the holder
    commits and closes after ``HOLDER_SECONDS``. Mirrors the KG
    N-07 harness: ``check_same_thread=False`` because the holder
    is created here but committed/closed by the releaser thread
    (used strictly sequentially — this thread never touches it
    after start).
    """
    holder = sqlite3.connect(db_path, check_same_thread=False)
    holder.execute("CREATE TABLE wal_hold (x INTEGER)")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO wal_hold VALUES (1)")

    def release_holder():
        time.sleep(HOLDER_SECONDS)
        holder.commit()
        holder.close()

    releaser = threading.Thread(target=release_holder)
    releaser.start()
    return releaser


def _journal_mode(db_path) -> str:
    with sqlite3.connect(db_path) as conn:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()


def _run_concurrently(fn, count=THREADS):
    """Run ``fn(i)`` in ``count`` threads released simultaneously.

    Returns the list of exceptions raised (empty on success)."""
    barrier = threading.Barrier(count)
    errors: list[BaseException] = []

    def run_one(i: int):
        try:
            barrier.wait(timeout=10)
            fn(i)
        except BaseException as e:  # noqa: BLE001 — collected and asserted by the caller
            errors.append(e)

    threads = [threading.Thread(target=run_one, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads), "a worker thread hung"
    return errors


# ---------------------------------------------------------------------------
# session_store — the chat/session store (highest impact)
# ---------------------------------------------------------------------------

def test_session_store_get_conn_survives_concurrent_first_open(
        tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    monkeypatch.setattr(session_store, "DB_PATH", db)
    releaser = _start_holder(db)
    try:
        conn = session_store._get_conn()  # raised OperationalError pre-fix
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
    finally:
        releaser.join(timeout=10)
    assert str(mode).lower() == "wal"


def test_session_store_concurrent_first_open_creates_all_sessions(
        tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    monkeypatch.setattr(session_store, "DB_PATH", db)
    monkeypatch.setattr(session_store, "_initialized", False)

    errors = _run_concurrently(
        lambda i: session_store.create_session(title=f"concurrent-{i}"))

    assert errors == [], errors
    titles = {s["title"] for s in session_store.list_sessions()}
    assert titles == {f"concurrent-{i}" for i in range(THREADS)}
    assert _journal_mode(db) == "wal"


# ---------------------------------------------------------------------------
# rag/store — the workspace prose index
# ---------------------------------------------------------------------------

def test_rag_store_get_conn_survives_concurrent_first_open(tmp_path):
    db = tmp_path / "workspace_rag.db"
    store = WorkspaceRagStore(db_path=db)
    releaser = _start_holder(db)
    try:
        conn = store._get_conn()  # raised OperationalError pre-fix
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
    finally:
        releaser.join(timeout=10)
    assert str(mode).lower() == "wal"


def test_rag_store_concurrent_first_open_upserts_all_documents(tmp_path):
    db = tmp_path / "workspace_rag.db"
    store = WorkspaceRagStore(db_path=db)

    def upsert(i: int):
        store.upsert_document(
            "root", f"file{i}.md",
            mtime=1, file_size=10, sha256="a" * 64, status="indexed")

    errors = _run_concurrently(upsert)

    assert errors == [], errors
    docs = store.list_documents("root")
    assert {d["file_path"] for d in docs} == {
        f"file{i}.md" for i in range(THREADS)}
    assert _journal_mode(db) == "wal"


# ---------------------------------------------------------------------------
# rewind/fork — opens the session database through its own path
# ---------------------------------------------------------------------------

def test_fork_get_conn_survives_concurrent_first_open(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    monkeypatch.setattr(session_store, "DB_PATH", db)
    releaser = _start_holder(db)
    try:
        conn = fork._get_conn()  # raised OperationalError pre-fix
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
    finally:
        releaser.join(timeout=10)
    assert str(mode).lower() == "wal"


def test_fork_concurrent_first_open_loses_no_writes(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    monkeypatch.setattr(session_store, "DB_PATH", db)

    def write_one(i: int):
        conn = fork._get_conn()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS fork_probe (i INTEGER)")
            conn.execute("INSERT INTO fork_probe VALUES (?)", (i,))
            conn.commit()
        finally:
            conn.close()

    errors = _run_concurrently(write_one)

    assert errors == [], errors
    conn = fork._get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) FROM fork_probe").fetchone()[0]
    finally:
        conn.close()
    assert count == THREADS
    assert _journal_mode(db) == "wal"


# ---------------------------------------------------------------------------
# tools/approval_cache — remembered approval grants
# ---------------------------------------------------------------------------

def test_approval_cache_open_survives_concurrent_first_open(tmp_path):
    db = tmp_path / "approval-cache.db"
    releaser = _start_holder(db)
    try:
        cache = ApprovalCache(db)  # raised OperationalError pre-fix
    finally:
        releaser.join(timeout=10)
    try:
        assert _journal_mode(db) == "wal"
        grant_id = cache.grant("/tmp/wal-probe", "read_only", "probe_tool")
        assert grant_id > 0
        assert len(cache.list_grants()) == 1
    finally:
        cache.close()


def test_approval_cache_concurrent_first_open_loses_no_grants(tmp_path):
    db = tmp_path / "approval-cache.db"
    caches: list[ApprovalCache] = []

    def grant_one(i: int):
        cache = ApprovalCache(db)
        caches.append(cache)
        cache.grant(f"/tmp/wal-concurrent-{i}", "read_only", "probe_tool")

    errors = _run_concurrently(grant_one)

    try:
        assert errors == [], errors
        assert len(caches) == THREADS
        prefixes = {g["prefix"] for g in caches[0].list_grants()}
        # grant() resolves prefixes; the suffix survives resolution
        # on every platform, so compare on the distinctive tail.
        assert all(
            any(p.endswith(f"wal-concurrent-{i}") for p in prefixes)
            for i in range(THREADS)), prefixes
        assert _journal_mode(db) == "wal"
    finally:
        for cache in caches:
            cache.close()
