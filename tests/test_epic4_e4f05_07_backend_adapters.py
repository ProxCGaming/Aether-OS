"""Epic 4 — E4-F05 / E4-F06 / E4-F07: pluggable backend adapter defects.

E4-F05 (Mem0): facts and episodes share one Mem0 user. Fact search
must not return episodes, clearing facts must not delete episodes,
and deleting an episode must not delete a fact learned under the
same task ID.

E4-F06 (LangMem JsonFileStore): two concurrent puts can both report
success while the older snapshot's save discards one durable write.

E4-F07 (LangMem JsonFileStore): a store whose initial load failed
(transient I/O error, corrupt or wrong-shaped file) must never
become a writable empty snapshot whose next save overwrites the
valid existing store. Writes reload-or-refuse.
"""
from __future__ import annotations

import asyncio
import copy
import os
import pathlib
import sys
import threading
import types

import pytest

from aether_engine.memory.backends.langmem_backend import JsonFileStore
from aether_engine.memory.backends.mem0_backend import Mem0MemoryBackend

_FACTS_NS = ("aether", "facts")
_EPISODES_NS = ("aether", "episodes")


# ---------------------------------------------------------------------------
# E4-F05 — Mem0 scope mixing
# ---------------------------------------------------------------------------

class RecordingMem0:
    """Raw-record Mem0 fake: seeds permitted SDK-shaped records and
    records every call the adapter emits, in insertion order."""

    def __init__(self, records):
        self.store = {r["id"]: copy.deepcopy(r) for r in records}
        self.calls: list = []

    def add(self, messages, user_id=None, metadata=None, infer=True):
        self.calls.append(("add", metadata, infer))
        mid = f"m{len(self.store)}"
        self.store[mid] = {"id": mid, "memory": messages[0]["content"],
                           "metadata": metadata or {}, "created_at": 99}
        return {"results": [{"id": mid}]}

    def search(self, query, user_id=None, limit=10):
        hits = [v for v in self.store.values()
                if query.lower() in v["memory"].lower()]
        return {"results": hits[:limit]}

    def get_all(self, user_id=None):
        return {"results": list(self.store.values())}

    def delete(self, mid):
        self.calls.append(("delete", mid))
        self.store.pop(mid, None)

    def update(self, mid, text):
        self.store[mid]["memory"] = text

    def delete_all(self, user_id=None):
        self.calls.append(("delete_all", user_id))
        self.store.clear()

    def deleted_ids(self):
        return [c[1] for c in self.calls if c[0] == "delete"]


# A fact and an episode from the SAME task, fact returned first —
# the audit's probe arrangement.
_FACT_RECORD = {
    "id": "fact-1", "memory": "User lives in Kolkata",
    "metadata": {"task_id": "t1"}, "created_at": 1,
}
_EPISODE_RECORD = {
    "id": "ep-1", "memory": "Task: plan the Kolkata trip\nOutcome: done",
    "metadata": {"aether_type": "episode", "task_id": "t1", "tags": []},
    "created_at": 2,
}


@pytest.fixture
def mem0_fake():
    return RecordingMem0([_FACT_RECORD, _EPISODE_RECORD])


@pytest.fixture
def mem0_backend(mem0_fake):
    return Mem0MemoryBackend(memory=mem0_fake)


class TestMem0Scopes:
    def test_search_facts_excludes_episodes(self, mem0_backend):
        facts = mem0_backend.search_facts("Kolkata")
        assert [f["fact_text"] for f in facts] == ["User lives in Kolkata"]

    def test_list_facts_control(self, mem0_backend):
        # Already correct pre-fix: listing filters episodes.
        assert len(mem0_backend.list_facts()) == 1
        assert len(mem0_backend.list_episodes()) == 1

    def test_clear_facts_keeps_episodes(self, mem0_backend, mem0_fake):
        assert mem0_backend.clear_facts() is True
        assert "fact-1" not in mem0_fake.store
        assert "ep-1" in mem0_fake.store, "clear_facts deleted the episode too"
        assert not any(c[0] == "delete_all" for c in mem0_fake.calls), \
            "clear_facts must not use the user-wide delete_all"

    def test_clear_episodes_keeps_facts(self, mem0_backend, mem0_fake):
        assert mem0_backend.clear_episodes() is True
        assert "ep-1" not in mem0_fake.store
        assert "fact-1" in mem0_fake.store

    def test_delete_episode_deletes_episode_not_fact(self, mem0_backend, mem0_fake):
        assert mem0_backend.delete_episode("t1") is True
        assert "ep-1" not in mem0_fake.store, "the episode itself must be deleted"
        assert "fact-1" in mem0_fake.store, \
            "delete_episode deleted the fact learned under the same task"
        assert mem0_fake.deleted_ids() == ["ep-1"]

    def test_delete_episode_unknown_task(self, mem0_backend, mem0_fake):
        assert mem0_backend.delete_episode("no-such-task") is False
        assert mem0_fake.deleted_ids() == []
        assert set(mem0_fake.store) == {"fact-1", "ep-1"}


# ---------------------------------------------------------------------------
# E4-F06 — LangMem concurrent puts lose a durable write
# ---------------------------------------------------------------------------

class TestLangMemConcurrentPuts:
    def test_concurrent_puts_are_both_durable(self, tmp_path, monkeypatch):
        """Deterministic interleave (the audit's): thread A's save is
        paused after its JSON snapshot was taken; thread B's put saves
        fully; A's older snapshot then lands last. Pre-fix the file
        ends up with only A's key although both puts returned."""
        path = tmp_path / "store.json"
        store = JsonFileStore(path)

        a_in_gate = threading.Event()
        b_saved = threading.Event()
        real_write_text = pathlib.Path.write_text
        real_replace = os.replace
        gate = {"used": False}

        def gated_write_text(self, data, *args, **kwargs):
            # Only the store's temp file, only the first writer (A).
            if self.name == "store.tmp" and not gate["used"]:
                gate["used"] = True
                a_in_gate.set()
                # Pre-fix B's save releases us; post-fix the store lock
                # means B cannot save first, so the timeout releases us
                # and B then saves after A — either way both must land.
                b_saved.wait(timeout=2)
            return real_write_text(self, data, *args, **kwargs)

        def tracking_replace(src, dst):
            result = real_replace(src, dst)
            if pathlib.Path(dst) == path and a_in_gate.is_set():
                b_saved.set()
            return result

        monkeypatch.setattr(pathlib.Path, "write_text", gated_write_text)
        monkeypatch.setattr(os, "replace", tracking_replace)

        errors: list = []

        def put(key, value):
            try:
                store.put(_FACTS_NS, key, value)
            except Exception as e:  # noqa: BLE001 — recorded, asserted below
                errors.append(e)

        ta = threading.Thread(target=put, args=("a", {"n": "a"}))
        ta.start()
        assert a_in_gate.wait(timeout=5), "thread A never reached its save"
        tb = threading.Thread(target=put, args=("b", {"n": "b"}))
        tb.start()
        ta.join(timeout=10)
        tb.join(timeout=10)
        assert not ta.is_alive() and not tb.is_alive()
        assert errors == [], "both puts must report success"

        fresh = JsonFileStore(path)
        assert fresh.get(_FACTS_NS, "a") is not None
        assert fresh.get(_FACTS_NS, "b") is not None, \
            "concurrent put lost a durable write"

    def test_sequential_puts_control(self, tmp_path):
        store = JsonFileStore(tmp_path / "store.json")
        store.put(_FACTS_NS, "a", {"n": "a"})
        store.put(_FACTS_NS, "b", {"n": "b"})
        fresh = JsonFileStore(tmp_path / "store.json")
        assert fresh.get(_FACTS_NS, "a") is not None
        assert fresh.get(_FACTS_NS, "b") is not None

    def test_many_concurrent_puts_all_durable(self, tmp_path):
        store = JsonFileStore(tmp_path / "store.json")

        def worker(n):
            for i in range(5):
                store.put(_FACTS_NS, f"k{n}-{i}", {"n": n, "i": i})

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        fresh = JsonFileStore(tmp_path / "store.json")
        missing = [f"k{n}-{i}" for n in range(8) for i in range(5)
                   if fresh.get(_FACTS_NS, f"k{n}-{i}") is None]
        assert missing == []


# ---------------------------------------------------------------------------
# E4-F07 — LangMem read failure must not enable overwriting the store
# ---------------------------------------------------------------------------

def _seeded_store(path) -> JsonFileStore:
    store = JsonFileStore(path)
    store.put(_FACTS_NS, "existing", {"fact_text": "EXISTING_FACT"})
    return store


class TestLangMemReadFailure:
    def test_transient_read_failure_does_not_overwrite(self, tmp_path, monkeypatch):
        path = tmp_path / "store.json"
        _seeded_store(path)

        real_read_text = pathlib.Path.read_text
        failing = {"on": True}

        def flaky_read_text(self, *args, **kwargs):
            if failing["on"] and self == path:
                raise OSError("transient read failure")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "read_text", flaky_read_text)
        store = JsonFileStore(path)  # initial load fails (transient)
        failing["on"] = False  # reads recover, as in the audit probe

        store.put(_FACTS_NS, "new", {"fact_text": "NEW_FACT"})

        fresh = JsonFileStore(path)
        assert fresh.get(_FACTS_NS, "existing") is not None, \
            "the valid existing store was overwritten from an empty snapshot"
        assert fresh.get(_FACTS_NS, "new") is not None

    def test_corrupt_store_refuses_writes_and_preserves_file(self, tmp_path):
        path = tmp_path / "store.json"
        path.write_bytes(b"{not json")
        before = path.read_bytes()

        store = JsonFileStore(path)
        with pytest.raises(RuntimeError, match="refusing to write"):
            store.put(_FACTS_NS, "new", {"fact_text": "NEW_FACT"})
        with pytest.raises(RuntimeError, match="refusing to write"):
            store.delete(_FACTS_NS, "existing")
        assert path.read_bytes() == before, "a refused write must not touch the file"

    def test_non_dict_store_refuses_writes(self, tmp_path):
        path = tmp_path / "store.json"
        path.write_text('["not", "a", "store"]', "utf-8")
        store = JsonFileStore(path)
        with pytest.raises(RuntimeError, match="refusing to write"):
            store.put(_FACTS_NS, "new", {"fact_text": "NEW_FACT"})
        assert path.read_text("utf-8") == '["not", "a", "store"]'

    def test_backend_surfaces_write_refusal(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "langmem", types.SimpleNamespace())
        from aether_engine.memory.backends.langmem_backend import LangMemMemoryBackend

        path = tmp_path / "store.json"
        path.write_bytes(b"{not json")
        backend = LangMemMemoryBackend(store=JsonFileStore(path))
        with pytest.raises(RuntimeError, match="refusing to write"):
            asyncio.run(backend.record_episode("t1", "prompt", "sum", [], "outcome"))

    def test_missing_file_is_a_fresh_store_control(self, tmp_path):
        path = tmp_path / "store.json"
        store = JsonFileStore(path)
        store.put(_FACTS_NS, "a", {"n": "a"})
        assert JsonFileStore(path).get(_FACTS_NS, "a") is not None

    def test_valid_file_loads_control(self, tmp_path):
        path = tmp_path / "store.json"
        _seeded_store(path)
        fresh = JsonFileStore(path)
        assert fresh.get(_FACTS_NS, "existing").value == {"fact_text": "EXISTING_FACT"}
