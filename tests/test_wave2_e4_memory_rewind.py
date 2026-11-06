"""Wave 2 regression tests for E4-08..E4-12, E4-14, and E4-15."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from aether_engine.memory import episodic, facade, session_store
from aether_engine.memory.backends.graphiti_backend import (
    GraphitiMemoryBackend,
    _LoopThread,
)
from aether_engine.memory.backends.langmem_backend import (
    JsonFileStore,
    LangMemMemoryBackend,
)
from aether_engine.memory.backends.mem0_backend import Mem0MemoryBackend
from aether_engine.rewind import fork, manifest, service
from aether_engine.session_export.importer import _rewrite_attachment_paths

_FACTS_NS = ("aether", "facts")
_EPISODES_NS = ("aether", "episodes")


# ---------------------------------------------------------------------------
# E4-08 — LangMem clears batch their file writes
# ---------------------------------------------------------------------------

def test_langmem_clear_namespaces_saves_once_per_clear(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "langmem", types.SimpleNamespace())
    store = JsonFileStore(tmp_path / "store.json")
    for index in range(3):
        store.put(_FACTS_NS, f"fact-{index}", {"fact_text": f"fact {index}"})
        store.put(_EPISODES_NS, f"ep-{index}", {"task_id": f"ep-{index}"})

    saves = 0
    real_save = store._save

    def counted_save():
        nonlocal saves
        saves += 1
        real_save()

    store._save = counted_save
    backend = LangMemMemoryBackend(store=store)

    assert backend.clear_facts() is True
    assert saves == 1
    assert store.search(_FACTS_NS, limit=10) == []
    assert len(store.search(_EPISODES_NS, limit=10)) == 3

    assert backend.clear_episodes() is True
    assert saves == 2
    assert store.search(_EPISODES_NS, limit=10) == []

    # Individual deletes retain their original one-item semantics.
    store.put(_FACTS_NS, "fact-x", {"fact_text": "fact x"})
    assert store.delete(_FACTS_NS, "fact-x") is True
    assert store.delete(_FACTS_NS, "fact-x") is False


# ---------------------------------------------------------------------------
# E4-09/E4-11 — one step-boundary rule for manifest, service, and fork
# ---------------------------------------------------------------------------

def _rows(*specs):
    return [
        {"rowid": rowid, "id": message_id, "role": role}
        for rowid, message_id, role in specs
    ]


def test_shared_step_boundary_rule_cases():
    from aether_engine.rewind.boundary import step_boundary

    rows = _rows(
        (10, "prev-user", "user"),
        (11, "prev-answer", "assistant"),
        (12, "task-user", "user"),
        (13, "task-answer", "assistant"),
    )

    step0 = step_boundary(rows, 0, 2)
    assert step0.anchored is True
    assert step0.cut_message_id == "prev-answer"
    assert step0.discarded_rowids == (12, 13)
    assert step0.is_empty is False

    step1 = step_boundary(rows, 1, 2)
    assert step1.cut_message_id == "task-user"
    assert step1.discarded_rowids == (13,)
    assert step1.is_empty is False

    step2 = step_boundary(rows, 2, 2)
    assert step2.cut_message_id == "task-answer"
    assert step2.discarded_rowids == ()
    assert step2.is_empty is False

    only_task = step_boundary(_rows((20, "u", "user"), (21, "a", "assistant")), 0, 1)
    assert only_task.cut_message_id is None
    assert only_task.discarded_rowids == (20, 21)
    assert only_task.is_empty is True

    unanchored = step_boundary(_rows((30, "a", "assistant")), 0, 0)
    assert unanchored.anchored is False
    assert unanchored.cut_message_id is None
    assert unanchored.discarded_rowids == ()
    assert unanchored.is_empty is False


@pytest.fixture
def rewind_env(tmp_path, monkeypatch):
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(manifest, "_initialized", False)
    monkeypatch.setattr(service, "_initialized_for", False)
    session_store.init_db()
    return tmp_path


def test_rewind_consumers_agree_on_shared_boundary(rewind_env):
    sid = session_store.create_session("Boundary")
    ids = [
        session_store.add_message(sid, "user", "previous question"),
        session_store.add_message(sid, "assistant", "previous answer"),
        session_store.add_message(sid, "user", "task question"),
        session_store.add_message(sid, "assistant", "task answer"),
    ]
    entries = [{"step_index": step} for step in range(3)]

    assert manifest._cut_message_ids(sid, entries) == [ids[1], ids[2], ids[3]]

    with service._get_conn() as conn:
        rowids = [row[0] for row in conn.execute(
            "SELECT rowid FROM session_messages WHERE session_id = ? ORDER BY rowid",
            (sid,),
        )]
        assert service._message_cut_rowids(conn, sid, 0, 2) == rowids[2:]
        assert service._message_cut_rowids(conn, sid, 1, 2) == rowids[3:]
        assert service._message_cut_rowids(conn, sid, 2, 2) == []

    assert fork._step_cut_message_id(sid, 0, 2) == (ids[1], False)
    assert fork._step_cut_message_id(sid, 1, 2) == (ids[2], False)
    assert fork._step_cut_message_id(sid, 2, 2) == (ids[3], False)


def test_fork_step_zero_of_only_task_is_empty(rewind_env):
    sid = session_store.create_session("Only task")
    session_store.add_message(sid, "user", "question")
    session_store.add_message(sid, "assistant", "answer")

    assert fork._step_cut_message_id(sid, 0, 1) == (None, True)


# ---------------------------------------------------------------------------
# E4-10 — the unfenced episodic embedding writer is gone
# ---------------------------------------------------------------------------

def test_episodic_unfenced_embedding_writer_is_removed():
    assert not hasattr(episodic, "_store_embedding_row")
    assert hasattr(episodic, "_store_embedding_row_if_current")


# ---------------------------------------------------------------------------
# E4-12 — same-basename attachments stay connected to their own bytes
# ---------------------------------------------------------------------------

def test_importer_reconnects_same_basename_attachments_by_origin(tmp_path):
    staged = {
        "a/report.txt": tmp_path / "staging" / "a" / "report.txt",
        "b/report.txt": tmp_path / "staging" / "b" / "report.txt",
    }
    staged["a/report.txt"].parent.mkdir(parents=True)
    staged["b/report.txt"].parent.mkdir(parents=True)
    staged["a/report.txt"].write_text("from A", encoding="utf-8")
    staged["b/report.txt"].write_text("from B", encoding="utf-8")
    final_dir = tmp_path / "attachments" / "restored"
    session_data = {
        "messages": [
            {
                "attachments": [
                    {"name": "report.txt", "path": "/origin/a/report.txt"},
                    {"name": "report.txt", "path": "/origin/b/report.txt"},
                ],
            },
            {
                "attachments": [
                    {"name": "report.txt", "path": "attachments/a/report.txt"},
                    {"name": "report.txt", "path": "attachments/b/report.txt"},
                ],
            },
        ],
    }

    rewritten = _rewrite_attachment_paths(session_data, staged, final_dir)
    paths = [
        Path(att["path"])
        for message in rewritten["messages"]
        for att in message["attachments"]
    ]

    assert paths == [
        final_dir / "a" / "report.txt",
        final_dir / "b" / "report.txt",
        final_dir / "a" / "report.txt",
        final_dir / "b" / "report.txt",
    ]
    # Translate each restored path back to its staged source and prove
    # the pairing did not cross the two files' contents.
    contents = []
    for path in paths:
        rel = path.relative_to(final_dir).as_posix()
        contents.append(staged[rel].read_text(encoding="utf-8"))
    assert contents == ["from A", "from B", "from A", "from B"]


def test_importer_pairs_bare_same_basename_references(tmp_path):
    staged = {
        "a/report.txt": tmp_path / "staging" / "a" / "report.txt",
        "b/report.txt": tmp_path / "staging" / "b" / "report.txt",
    }
    final_dir = tmp_path / "attachments" / "restored"
    session_data = {
        "messages": [{
            "attachments": [
                {"name": "report.txt", "path": "/foreign/report.txt"},
                {"name": "report.txt", "path": "/foreign/report.txt"},
            ],
        }],
    }

    rewritten = _rewrite_attachment_paths(session_data, staged, final_dir)
    paths = [Path(att["path"]) for att in rewritten["messages"][0]["attachments"]]

    assert paths == [
        final_dir / "a" / "report.txt",
        final_dir / "b" / "report.txt",
    ]


# ---------------------------------------------------------------------------
# E4-14 — Graphiti's loop thread has a real shutdown path
# ---------------------------------------------------------------------------

class _FakeGraphiti:
    async def search(self, query, num_results=10):
        return []


def test_graphiti_backend_close_joins_loop_thread_and_evicts_cache():
    loop_thread = _LoopThread()
    backend = GraphitiMemoryBackend(graphiti=_FakeGraphiti(), loop_thread=loop_thread)
    facade._BACKEND_CACHE["graphiti"] = backend
    try:
        assert loop_thread._thread.is_alive()
        assert facade.close_memory_backend("graphiti") is True
        assert "graphiti" not in facade._BACKEND_CACHE
        assert not loop_thread._thread.is_alive()
        # Closing is idempotent.
        backend.close()
        assert not loop_thread._thread.is_alive()
    finally:
        facade._BACKEND_CACHE.pop("graphiti", None)
        if loop_thread._thread.is_alive():
            backend.close()


# ---------------------------------------------------------------------------
# E4-15 — Mem0 fact mutations honour the episode marker and existence
# ---------------------------------------------------------------------------

class _RecordingMem0:
    def __init__(self):
        self.store = {
            "fact-1": {
                "id": "fact-1",
                "memory": "User lives in Kolkata",
                "metadata": {"task_id": "t1"},
            },
            "ep-1": {
                "id": "ep-1",
                "memory": "Task episode",
                "metadata": {"aether_type": "episode", "task_id": "t1"},
            },
        }
        self.deleted = []
        self.updated = []

    def get_all(self, user_id=None):
        return {"results": list(self.store.values())}

    def delete(self, memory_id):
        self.deleted.append(memory_id)
        self.store.pop(memory_id, None)

    def update(self, memory_id, text):
        self.updated.append((memory_id, text))
        self.store[memory_id]["memory"] = text


@pytest.fixture
def mem0_backend():
    fake = _RecordingMem0()
    return Mem0MemoryBackend(memory=fake), fake


def test_mem0_delete_fact_is_truthful_and_marker_aware(mem0_backend):
    backend, fake = mem0_backend

    assert backend.delete_fact("missing") is False
    assert backend.delete_fact("ep-1") is False
    assert fake.deleted == []
    assert set(fake.store) == {"fact-1", "ep-1"}

    assert backend.delete_fact("fact-1") is True
    assert fake.deleted == ["fact-1"]
    assert "fact-1" not in fake.store


def test_mem0_update_fact_is_truthful_and_marker_aware(mem0_backend):
    backend, fake = mem0_backend

    assert backend.update_fact("missing", "new") is False
    assert backend.update_fact("ep-1", "new") is False
    assert fake.updated == []
    assert fake.store["ep-1"]["memory"] == "Task episode"

    assert backend.update_fact("fact-1", "User lives in Delhi") is True
    assert fake.updated == [("fact-1", "User lives in Delhi")]
    assert fake.store["fact-1"]["memory"] == "User lives in Delhi"
