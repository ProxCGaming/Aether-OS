"""Wave 2 remainder — RAG: N-21, N-12 (prose indexer).

- N-21: ``backfill_pending`` must count the pending queue with
  COUNT(*) instead of materialising it just to ``len()`` it, and
  ``search_candidates`` must stream its rows instead of
  ``fetchall()``-ing every chunk in the granted roots at once.
- N-12: ``_index_pass``'s full ``scan_workspace`` walk and
  ``_index_file``'s file read must not run on the event loop.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from aether_engine.memory import episodic
from aether_engine.rag import prose_indexer
from aether_engine.rag.prose_indexer import ProseIndexer, ScanEntry, ScanResult
from aether_engine.rag.store import WorkspaceRagStore


@pytest.fixture
def store(tmp_path):
    return WorkspaceRagStore(tmp_path / "workspace_rag.db")


@pytest.fixture
def indexer(store):
    return ProseIndexer(store=store)


async def _probe_delay(coro, probe_s=0.05):
    task = asyncio.create_task(coro)
    start = time.monotonic()
    await asyncio.sleep(probe_s)
    delay = time.monotonic() - start
    result = await task
    return delay, result


def _seed_doc_with_chunks(store, root: str, rel: str, n_chunks: int) -> str:
    doc_id = store.upsert_document(
        root, rel, mtime=1, file_size=10, sha256="x", status="pending")
    store.replace_chunks(doc_id, [
        {"chunk_index": i, "line_start": i + 1, "line_end": i + 2,
         "content": f"chunk {i} of {rel}"}
        for i in range(n_chunks)
    ])
    return doc_id


# ---------------------------------------------------------------------------
# N-21 — pending count via COUNT(*); streamed candidate reads
# ---------------------------------------------------------------------------

def test_count_pending_chunks_matches_queue(store):
    root = "/ws"
    doc_id = _seed_doc_with_chunks(store, root, "a.md", 3)
    _seed_doc_with_chunks(store, root, "b.md", 2)
    assert store.count_pending_chunks() == 5
    assert store.count_pending_chunks() == len(store.pending_chunks())
    store.set_chunk_embedding(
        store.pending_chunks(doc_id=doc_id)[0]["id"], [0.1, 0.2])
    assert store.count_pending_chunks() == 4
    assert store.count_pending_chunks(doc_id=doc_id) == 2


@pytest.mark.asyncio
async def test_backfill_pending_reports_count_without_draining(
        indexer, store, monkeypatch):
    root = "/ws"
    _seed_doc_with_chunks(store, root, "a.md", 3)

    async def no_embed(text, task_id=None, context=None, config=None):
        return None

    monkeypatch.setattr(episodic, "embed_text", no_embed)
    result = await indexer.backfill_pending()
    assert result == {"embedded": 0, "pending": 3}


class _CountingCursor(sqlite3.Cursor):
    _sql = ""

    def execute(self, sql, parameters=()):
        self._sql = sql
        return super().execute(sql, parameters)

    def fetchall(self):
        rows = super().fetchall()
        self.connection.record(self._sql, len(rows))
        return rows


class _CountingConn(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events: list[tuple[str, int]] = []

    def cursor(self, factory=None):
        return super().cursor(_CountingCursor)

    def execute(self, sql, parameters=()):
        # The C-level shortcut does not dispatch to the overridden
        # cursor() — route it through the counting cursor explicitly.
        return self.cursor().execute(sql, parameters)

    def record(self, sql: str, n: int):
        self.events.append((sql, n))


def test_search_candidates_streams_rows(store, monkeypatch):
    root = "/ws"
    doc_id = _seed_doc_with_chunks(store, root, "a.md", 6)
    for chunk in store.pending_chunks(doc_id=doc_id):
        store.set_chunk_embedding(chunk["id"], [0.3, 0.4])

    created: list[_CountingConn] = []

    def counting_get_conn():
        store.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(store.db_path, factory=_CountingConn)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        created.append(conn)
        return conn

    monkeypatch.setattr(store, "_get_conn", counting_get_conn)
    candidates = store.search_candidates([root])
    assert len(candidates) == 6
    assert all(c["embedding"] == [0.3, 0.4] for c in candidates)
    fetched = sum(
        n for conn in created for sql, n in conn.events
        if "FROM prose_chunks" in sql and "SELECT" in sql.upper())
    assert fetched == 0, (
        f"search_candidates fetchall()ed {fetched} chunk rows at once")


# ---------------------------------------------------------------------------
# N-12 — scan + file read off the event loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_index_pass_scan_runs_off_event_loop(
        indexer, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    indexer.grant_consent(str(ws))

    def slow_scan(root, known_indexable=None):
        time.sleep(0.4)
        return ScanResult()

    monkeypatch.setattr(prose_indexer, "scan_workspace", slow_scan)
    delay, result = await _probe_delay(indexer.index_workspace(str(ws)))
    assert result["workspace_root"] == str(ws)
    assert delay < 0.25, f"event loop stalled {delay:.2f}s by workspace scan"


@pytest.mark.asyncio
async def test_index_file_read_runs_off_event_loop(
        indexer, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    path = ws / "note.md"
    path.write_text("# Title\n\nsome prose content here")

    def slow_read_bytes(self):
        time.sleep(0.4)
        return b"# Title\n\nsome prose content here"

    monkeypatch.setattr(Path, "read_bytes", slow_read_bytes)

    async def fake_embed(text, task_id=None, context=None, config=None):
        return [0.1, 0.2]

    monkeypatch.setattr(episodic, "embed_text", fake_embed)
    entry = ScanEntry(rel_path="note.md", abspath=path, size=path.stat().st_size)
    delay, _ = await _probe_delay(indexer._index_file(str(ws), entry))
    assert delay < 0.25, f"event loop stalled {delay:.2f}s by file read"
    doc = indexer.store.get_document(str(ws), "note.md")
    assert doc is not None and doc["status"] == "indexed"
