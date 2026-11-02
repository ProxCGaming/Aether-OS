"""Epic 4 — E4-F08: unavailable-backend delete/clear requests falsely
acknowledge successful deletion.

The defect (Astra full-product review, Epic 4): the websocket memory
management handlers skip the operation when the configured backend is
unavailable (``backend_or_none`` returns None) yet unconditionally
send ``success: True`` for episode delete, episodes clear, fact
delete, entity delete and graph clear. The responses are not derived
from the backend's boolean result either, and a backend that raises
(LangMem's honest NotImplementedError for entity delete, or its
E4-F07 refusal to write over an unloadable store) escapes the handler
and kills the receive loop instead of producing a failure reply.
Graph list turns unavailability into a bare ``facts: []`` with no
error field, unlike episodes list / export, which report it.

These tests pin the fixed behaviour:

* unavailable backend -> ``success: False`` + the same unavailable
  error the episodes-list and export handlers already use, for all
  five management operations; graph list carries the error too;
* the response is derived from the backend's actual result: a
  backend returning False yields ``success: False`` (and the call is
  really made), a backend returning True yields ``success: True``;
* a raising backend yields ``success: False`` + the exception's
  message, and the socket stays alive for the next request;
* a missing id is a failure, not a silent success;
* failed management operations are audit-logged
  (MEMORY_MANAGEMENT_FAILED);
* the native backend (available, working) still acknowledges real
  deletions with ``success: True`` and the data is actually gone.

TDD: the unavailable / derived / raising / missing-id tests fail on
the pre-fix code (false ``success: True``, or no response at all).
"""
import asyncio
import contextlib
import importlib.util
import json
import signal
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.memory import facade
from aether_engine.memory.facade import MemoryBackend
from aether_engine.providers.base import StreamChunk

UNAVAILABLE_ERROR = (
    "The configured memory backend is unavailable (see engine log)."
)


# ---------------------------------------------------------------------------
# Bounded receive (copied from tests/test_e3f04_mcp_config_save.py): pre-fix,
# a raising backend kills the receive loop and the socket never answers —
# an unbounded receive would hang the suite instead of failing.
# ---------------------------------------------------------------------------

class _NoResponse(Exception):
    """Raised when the socket sends nothing within the bound."""


@contextlib.contextmanager
def _bounded(seconds=20):
    def _handler(signum, frame):
        raise _NoResponse(f"no websocket response within {seconds}s")

    previous = signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class StubBackend(MemoryBackend):
    """A facade backend whose management results are scripted per
    method: True/False to return, or an exception instance to raise.
    Records every management call so tests can prove the handler
    really invoked the backend and honoured its answer."""

    name = "stub"

    def __init__(self):
        self.calls: list = []
        self.results: dict = {}
        self.facts: list = []
        self.episodes: list = []

    def _outcome(self, method, *args):
        self.calls.append((method, args))
        outcome = self.results.get(method, True)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def delete_episode(self, task_id):
        return self._outcome("delete_episode", task_id)

    def clear_episodes(self):
        return self._outcome("clear_episodes")

    def delete_fact(self, fact_id):
        return self._outcome("delete_fact", fact_id)

    def delete_entity(self, entity_name):
        return self._outcome("delete_entity", entity_name)

    def clear_facts(self):
        return self._outcome("clear_facts")

    def list_episodes(self, limit=50):
        return list(self.episodes)

    def list_facts(self, limit=100, include_history=False):
        return list(self.facts)


class _FactProvider:
    """Extraction stub (the test_epic4_memory FactProvider pattern)."""

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        blob = json.dumps(messages)
        if "OLD facts:" in blob:
            yield StreamChunk(text='[{"old": 0, "new": 0}]', finish_reason="stop")
        else:
            yield StreamChunk(
                text='[{"entity_name": "User", "entity_type": "Person", '
                     '"fact": "User lives in Kolkata", "confidence": 0.9}]',
                finish_reason="stop")


class _WsHarness:
    """Real-socket harness (the test_e3f04_mcp_config_save pattern):
    isolated engine auth + audit log + session store in a tmp dir."""

    def setup_method(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        engine_state.token_path = self.tmp_path / "engine.token"
        engine_state.audit_logger.log_path = self.tmp_path / "audit.log"
        engine_state.initialize_auth()
        self._session_db = patch(
            "aether_engine.memory.session_store.DB_PATH",
            self.tmp_path / "test_sessions.db",
        )
        self._session_db.start()
        facade._BACKEND_CACHE.clear()

    def teardown_method(self):
        facade._BACKEND_CACHE.clear()
        self._session_db.stop()
        self.tmp_dir.cleanup()

    def _connect(self, client):
        # The app lifespan re-runs engine_state.initialize() when the
        # TestClient starts, regenerating the token — read the current
        # one (and it also reloads user_config, so backend selection
        # happens per-test, after connect).
        return client.websocket_connect(
            "/ws/tasks",
            headers={"Authorization": f"Bearer {engine_state.auth_token}"},
        )

    def _send(self, ws, event_type, payload):
        with _bounded():
            ws.send_text(Event(type=event_type, payload=payload).to_json())
            return Event.from_json(ws.receive_text())

    def _select_backend(self, name):
        engine_state.user_config.memory_backend = name

    def _audit_text(self):
        path = self.tmp_path / "audit.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""


# ---------------------------------------------------------------------------
# Facade level: an unavailable backend is never fabricated into a stand-in
# whose management calls could "succeed".
# ---------------------------------------------------------------------------

class TestFacadeUnavailableContract:
    def test_backend_or_none_returns_none_for_absent_package(self):
        if importlib.util.find_spec("mem0") is not None:
            pytest.skip("mem0 is installed in this environment")
        config = types.SimpleNamespace(memory_backend="mem0")
        facade._BACKEND_CACHE.clear()
        assert facade.backend_or_none(config, context="test") is None
        with pytest.raises(facade.MemoryBackendUnavailable):
            facade.get_memory_backend(config)


# ---------------------------------------------------------------------------
# Handler level: the audit's probe — mem0 selected, package absent.
# ---------------------------------------------------------------------------

class TestUnavailableBackendAcks(_WsHarness):
    def test_all_management_ops_fail_honestly_when_backend_unavailable(self):
        if importlib.util.find_spec("mem0") is not None:
            pytest.skip("mem0 is installed in this environment")
        with TestClient(app) as client, self._connect(client) as ws:
            with _bounded():
                ws.receive_text()  # HELLO
            self._select_backend("mem0")

            resp = self._send(ws, EventType.MEMORY_EPISODE_DELETE_REQUEST,
                              {"task_id": "t1"})
            assert resp.type == EventType.MEMORY_EPISODE_DELETE_RESPONSE
            assert resp.payload["success"] is False
            assert resp.payload["error"] == UNAVAILABLE_ERROR
            assert resp.payload["episodes"] == []

            resp = self._send(ws, EventType.MEMORY_EPISODES_CLEAR_REQUEST, {})
            assert resp.type == EventType.MEMORY_EPISODES_CLEAR_RESPONSE
            assert resp.payload["success"] is False
            assert resp.payload["error"] == UNAVAILABLE_ERROR

            resp = self._send(ws, EventType.MEMORY_FACT_DELETE_REQUEST,
                              {"fact_id": "f1"})
            assert resp.type == EventType.MEMORY_FACT_DELETE_RESPONSE
            assert resp.payload["success"] is False
            assert resp.payload["error"] == UNAVAILABLE_ERROR
            assert resp.payload["facts"] == []

            resp = self._send(ws, EventType.MEMORY_ENTITY_DELETE_REQUEST,
                              {"entity_name": "User"})
            assert resp.type == EventType.MEMORY_ENTITY_DELETE_RESPONSE
            assert resp.payload["success"] is False
            assert resp.payload["error"] == UNAVAILABLE_ERROR

            resp = self._send(ws, EventType.MEMORY_GRAPH_CLEAR_REQUEST, {})
            assert resp.type == EventType.MEMORY_GRAPH_CLEAR_RESPONSE
            assert resp.payload["success"] is False
            assert resp.payload["error"] == UNAVAILABLE_ERROR

            # Graph list must report unavailability like episodes list
            # and export already do — not a bare empty list.
            resp = self._send(ws, EventType.MEMORY_GRAPH_REQUEST, {})
            assert resp.type == EventType.MEMORY_GRAPH_RESPONSE
            assert resp.payload["facts"] == []
            assert resp.payload["error"] == UNAVAILABLE_ERROR

            # The session is alive and the failures were audit-logged.
            resp = self._send(ws, EventType.MEMORY_EPISODES_REQUEST, {})
            assert resp.type == EventType.MEMORY_EPISODES_RESPONSE
            assert resp.payload["error"] == UNAVAILABLE_ERROR
        assert "MEMORY_MANAGEMENT_FAILED" in self._audit_text()


# ---------------------------------------------------------------------------
# Handler level: responses derive from the backend's real result.
# ---------------------------------------------------------------------------

class TestDerivedOutcomes(_WsHarness):
    def _use_stub(self):
        stub = StubBackend()
        facade._BACKEND_CACHE["mem0"] = stub
        return stub

    def test_backend_false_and_true_are_honoured(self):
        stub = self._use_stub()
        with TestClient(app) as client, self._connect(client) as ws:
            with _bounded():
                ws.receive_text()  # HELLO
            self._select_backend("mem0")

            stub.results["delete_episode"] = False
            resp = self._send(ws, EventType.MEMORY_EPISODE_DELETE_REQUEST,
                              {"task_id": "ghost"})
            assert resp.payload["success"] is False
            # The handler really called the backend (no skipping).
            assert ("delete_episode", ("ghost",)) in stub.calls

            stub.results["delete_episode"] = True
            resp = self._send(ws, EventType.MEMORY_EPISODE_DELETE_REQUEST,
                              {"task_id": "real"})
            assert resp.payload["success"] is True

            stub.results["clear_facts"] = False
            stub.facts = [{"fact_id": "f1", "fact_text": "still here"}]
            resp = self._send(ws, EventType.MEMORY_GRAPH_CLEAR_REQUEST, {})
            assert resp.payload["success"] is False
            # The refreshed list shows what actually remains.
            assert resp.payload["facts"] == stub.facts

            resp = self._send(ws, EventType.MEMORY_EPISODES_CLEAR_REQUEST, {})
            assert resp.payload["success"] is True
            resp = self._send(ws, EventType.MEMORY_FACT_DELETE_REQUEST,
                              {"fact_id": "f1"})
            assert resp.payload["success"] is True
            resp = self._send(ws, EventType.MEMORY_ENTITY_DELETE_REQUEST,
                              {"entity_name": "User"})
            assert resp.payload["success"] is True

    def test_missing_id_is_a_failure_not_a_silent_success(self):
        stub = self._use_stub()
        with TestClient(app) as client, self._connect(client) as ws:
            with _bounded():
                ws.receive_text()  # HELLO
            self._select_backend("mem0")

            resp = self._send(ws, EventType.MEMORY_EPISODE_DELETE_REQUEST, {})
            assert resp.payload["success"] is False
            assert "task_id" in resp.payload["error"]
            # Nothing was deleted — the backend was never called.
            assert stub.calls == []

            resp = self._send(ws, EventType.MEMORY_FACT_DELETE_REQUEST, {})
            assert resp.payload["success"] is False
            assert "fact_id" in resp.payload["error"]

            resp = self._send(ws, EventType.MEMORY_ENTITY_DELETE_REQUEST, {})
            assert resp.payload["success"] is False
            assert "entity_name" in resp.payload["error"]

    def test_raising_backend_reports_failure_and_socket_survives(self):
        stub = self._use_stub()
        stub.results["delete_fact"] = RuntimeError("store is locked")
        stub.results["delete_entity"] = NotImplementedError(
            "The LangMem backend has no entity grouping; "
            "delete individual facts.")
        with TestClient(app) as client, self._connect(client) as ws:
            with _bounded():
                ws.receive_text()  # HELLO
            self._select_backend("mem0")

            resp = self._send(ws, EventType.MEMORY_FACT_DELETE_REQUEST,
                              {"fact_id": "f1"})
            assert resp.type == EventType.MEMORY_FACT_DELETE_RESPONSE
            assert resp.payload["success"] is False
            assert "store is locked" in resp.payload["error"]

            resp = self._send(ws, EventType.MEMORY_ENTITY_DELETE_REQUEST,
                              {"entity_name": "User"})
            assert resp.type == EventType.MEMORY_ENTITY_DELETE_RESPONSE
            assert resp.payload["success"] is False
            assert "no entity grouping" in resp.payload["error"]

            # The receive loop is alive for the next request.
            resp = self._send(ws, EventType.MEMORY_GRAPH_REQUEST, {})
            assert resp.type == EventType.MEMORY_GRAPH_RESPONSE
        assert "MEMORY_MANAGEMENT_FAILED" in self._audit_text()

    def test_langmem_unloadable_store_refusal_is_reported(self, monkeypatch):
        # The real E4-F07 seam: a LangMem store whose file cannot be
        # loaded refuses mutations with RuntimeError. Through the WS
        # handler that refusal must become success: False, not a dead
        # socket (pre-fix) or a fake success.
        monkeypatch.setitem(sys.modules, "langmem", types.SimpleNamespace())
        from aether_engine.memory.backends.langmem_backend import (
            LangMemMemoryBackend,
        )

        store_path = self.tmp_path / "langmem_store.json"
        store_path.write_text("{ not json", encoding="utf-8")
        backend = LangMemMemoryBackend(store_path=store_path)
        facade._BACKEND_CACHE["langmem"] = backend

        with TestClient(app) as client, self._connect(client) as ws:
            with _bounded():
                ws.receive_text()  # HELLO
            self._select_backend("langmem")

            resp = self._send(ws, EventType.MEMORY_FACT_DELETE_REQUEST,
                              {"fact_id": "f1"})
            assert resp.type == EventType.MEMORY_FACT_DELETE_RESPONSE
            assert resp.payload["success"] is False
            assert "refusing to write" in resp.payload["error"]

            resp = self._send(ws, EventType.MEMORY_EPISODES_REQUEST, {})
            assert resp.type == EventType.MEMORY_EPISODES_RESPONSE


# ---------------------------------------------------------------------------
# Control: the native backend acknowledges real deletions truthfully.
# ---------------------------------------------------------------------------

class TestNativeBackendControl(_WsHarness):
    def setup_method(self):
        super().setup_method()
        from aether_engine.memory import episodic

        self._kg_db = patch(
            "aether_engine.memory.knowledge_graph.DB_PATH",
            self.tmp_path / "kg.db",
        )
        self._kg_db.start()
        mem_db = self.tmp_path / "episodes.db"
        emb_db = self.tmp_path / "embeddings.db"
        orig_get_conn = episodic._get_conn

        def fake_get_conn(db_path=None):
            if db_path is None or str(db_path) == str(episodic.DB_PATH):
                return orig_get_conn(mem_db)
            if str(db_path) == str(episodic.EMBEDDINGS_DB_PATH):
                return orig_get_conn(emb_db)
            return orig_get_conn(db_path)

        self._epi_patches = [
            patch.object(episodic, "_get_conn", fake_get_conn),
            patch.object(episodic, "DB_PATH", mem_db),
            patch.object(episodic, "EMBEDDINGS_DB_PATH", emb_db),
        ]
        for p in self._epi_patches:
            p.start()

    def teardown_method(self):
        for p in self._epi_patches:
            p.stop()
        self._kg_db.stop()
        super().teardown_method()

    def _seed(self):
        backend = facade.get_memory_backend(backend_name="native")
        asyncio.run(backend.record_episode(
            "task-1", "prompt one", "summary one", ["chat"], "done"))
        asyncio.run(backend.learn(
            _FactProvider(), "I live in Kolkata", "Noted", "task-1"))
        return backend

    def test_native_deletes_acknowledge_and_data_is_gone(self):
        backend = self._seed()
        assert backend.list_episodes()
        fact_id = backend.list_facts()[0]["fact_id"]

        with TestClient(app) as client, self._connect(client) as ws:
            with _bounded():
                ws.receive_text()  # HELLO
            self._select_backend("native")

            resp = self._send(ws, EventType.MEMORY_GRAPH_REQUEST, {})
            assert len(resp.payload["facts"]) == 1
            assert "error" not in resp.payload

            resp = self._send(ws, EventType.MEMORY_EPISODE_DELETE_REQUEST,
                              {"task_id": "task-1"})
            assert resp.payload["success"] is True
            assert resp.payload["episodes"] == []
            assert backend.list_episodes() == []

            resp = self._send(ws, EventType.MEMORY_FACT_DELETE_REQUEST,
                              {"fact_id": fact_id})
            assert resp.payload["success"] is True
            assert resp.payload["facts"] == []
            assert backend.list_facts() == []

            # Re-seed, then the clears acknowledge truthfully too.
            asyncio.run(backend.learn(
                _FactProvider(), "I live in Kolkata", "Noted", "task-2"))
            asyncio.run(backend.record_episode(
                "task-2", "prompt two", "summary two", ["chat"], "done"))
            resp = self._send(ws, EventType.MEMORY_GRAPH_CLEAR_REQUEST, {})
            assert resp.payload["success"] is True
            assert backend.list_facts() == []
            resp = self._send(ws, EventType.MEMORY_EPISODES_CLEAR_REQUEST, {})
            assert resp.payload["success"] is True
            assert backend.list_episodes() == []
