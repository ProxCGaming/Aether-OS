"""Epic 8, E8-1.1a: per-task rewind manifest — recording + surfacing.

The manifest is recorded AS A TASK RUNS (grill Q5): step -> checkpoint,
label, files-written-so-far, for the session's LAST task only. This
slice records the points (graph executor + fast chat), stores them in
memory.db, and serves them over REWIND_MANIFEST_GET. Rewind EXECUTION
is E8-1.1b and deliberately absent here.

Layers covered:
  * rewind.manifest — store semantics: entry shape/order, latest-task
    resolution, older tasks return nothing, session-delete cascade.
  * langgraph.fast_chat — message-level points for fast-chat tasks.
  * langgraph.executor — superstep points carrying real LangGraph
    checkpoint ids, with files-written-so-far tracked per point.
  * routes.websocket — the REWIND_MANIFEST_GET contract shape.

All providers are scripted stubs (house pattern): no network.
"""
import sqlite3
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.fast_chat import run_fast_chat
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.memory import session_store
from aether_engine.providers.base import RateLimitError, StreamChunk, ToolCall
from aether_engine.rewind import manifest as rewind_manifest
from aether_engine.routes import websocket as ws_module
from aether_engine.tools.registry import ToolRegistry, build_tool_registry

# ---------------------------------------------------------------------------
# Fixtures / stubs
# ---------------------------------------------------------------------------


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    """Session store (and therefore the manifest, which shares its DB)
    isolated to a tmp memory.db."""
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    return tmp_path


class ChunkProvider:
    """Fast-chat stub: yields fixed chunks, completes."""

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        for text in ("Hello ", "there", "!"):
            yield StreamChunk(text=text)
        yield StreamChunk(finish_reason="stop")


class FailingProvider:
    """Fast-chat stub: raises a provider error on stream."""

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        raise RateLimitError("slow down")
        yield  # pragma: no cover — makes this an async generator


class RoutingProvider:
    """Full-graph stub (parity pattern): the supervisor always routes
    to the planner; the planner answers with plain text."""

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "planner":
            yield StreamChunk(text="Planner answer.", finish_reason="stop")
            return
        yield StreamChunk(
            text='{"next": "planner", "reason": "test routing", "briefing": ""}',
            finish_reason="stop",
        )


class WritingProvider:
    """Full-graph stub: the planner's first call writes a file via the
    write_file tool; its later calls return final text."""

    def __init__(self, target_path):
        self._planner_calls = 0
        self._target_path = target_path

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "planner":
            self._planner_calls += 1
            if self._planner_calls == 1:
                yield StreamChunk(
                    tool_calls=[ToolCall(
                        name="write_file",
                        args={"path": self._target_path, "content": "hello"},
                        call_id="call-write-1",
                    )],
                    finish_reason="tool_calls",
                )
            else:
                yield StreamChunk(text="Wrote the file.", finish_reason="stop")
            return
        yield StreamChunk(
            text='{"next": "planner", "reason": "test routing", "briefing": ""}',
            finish_reason="stop",
        )


@pytest.fixture
def graph_env(store_env, monkeypatch):
    """Isolate a full executor run (parity pattern): tmp checkpoint DB,
    no real memory writes on completion, no artifact files on disk."""
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", store_env / "checkpoints.db")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    from aether_engine.langgraph.nodes import planner as planner_module
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "test/artifact.txt")
    # The subprocess sandbox needs Windows Job Objects (pywin32),
    # absent on this Linux VM; dispatching to run_worker in-process
    # exercises the identical worker code path, minus isolation
    # (the test_epic2_edit_file fixture pattern).
    from aether_engine.workers import sandbox
    from aether_worker.__main__ import run_worker

    async def _in_process_worker(tool_name, args, task_class="standard",
                                 audit_logger=None):
        return await run_worker({"tool_name": tool_name, "args": args})

    monkeypatch.setattr(sandbox, "execute_tool_in_worker", _in_process_worker)
    return store_env


async def _collect(agen):
    return [event async for event in agen]


# ---------------------------------------------------------------------------
# Manifest store semantics
# ---------------------------------------------------------------------------


def test_record_and_get_entries_shape_and_order(store_env):
    sid = session_store.create_session("Manifest")
    rewind_manifest.record_point(sid, "task-1", 1, "cp-b", "After planner", ["a.txt"])
    rewind_manifest.record_point(sid, "task-1", 0, "cp-a", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-1", 2, "cp-c", "Task complete", ["a.txt", "b.txt"])

    entries = rewind_manifest.get_entries(sid, "task-1")
    assert [e["step_index"] for e in entries] == [0, 1, 2]
    assert entries[0] == {
        "step_index": 0,
        "checkpoint_id": "cp-a",
        "label": "Before this task began",
        "files_written": [],
        # N01: entries carry their step's write events; these points
        # were recorded without any, so the list is honestly empty.
        "write_events": [],
        "timestamp": entries[0]["timestamp"],
        # M12: entries carry their message boundary; this session
        # has no stored messages, so the anchor is honestly None.
        "cut_message_id": None,
    }
    assert entries[1]["files_written"] == ["a.txt"]
    assert entries[2]["files_written"] == ["a.txt", "b.txt"]
    assert all(isinstance(e["timestamp"], int) for e in entries)


def test_get_manifest_defaults_to_latest_task(store_env):
    sid = session_store.create_session("Two tasks")
    rewind_manifest.record_point(sid, "task-old", 0, "cp-0", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-new", 0, "cp-1", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-new", 1, "cp-2", "Reply complete", [])

    result = rewind_manifest.get_manifest(sid)
    assert result["session_id"] == sid
    assert result["task_id"] == "task-new"
    assert [e["step_index"] for e in result["entries"]] == [0, 1]


def test_get_manifest_older_task_returns_nothing(store_env):
    """Rewind points exist only for the LAST task (Q5): asking for an
    older task's manifest returns no entries, never its points."""
    sid = session_store.create_session("Two tasks")
    rewind_manifest.record_point(sid, "task-old", 0, "cp-0", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-new", 0, "cp-1", "Before this task began", [])

    result = rewind_manifest.get_manifest(sid, task_id="task-old")
    assert result["entries"] == []
    assert result["task_id"] == "task-old"


def test_get_manifest_unknown_session(store_env):
    result = rewind_manifest.get_manifest("no-such-session")
    assert result == {"session_id": "no-such-session", "task_id": None, "entries": []}


def test_session_delete_cascades_manifest(store_env):
    sid = session_store.create_session("Doomed")
    rewind_manifest.record_point(sid, "task-1", 0, "cp-a", "Before this task began", [])
    assert rewind_manifest.get_entries(sid, "task-1")

    assert session_store.delete_session(sid) is True
    assert rewind_manifest.get_entries(sid, "task-1") == []
    assert rewind_manifest.get_latest_task_id(sid) is None


# ---------------------------------------------------------------------------
# Fast-chat recording (message-level points)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fast_chat_records_message_level_points(store_env):
    sid = session_store.create_session("Chat")
    events = await _collect(run_fast_chat(
        "hi", ChunkProvider(), session_id=sid, task_id="fc-task-1"))
    assert events[-1].type == EventType.TASK_COMPLETED

    entries = rewind_manifest.get_entries(sid, "fc-task-1")
    assert len(entries) == 2
    assert entries[0]["step_index"] == 0
    assert entries[0]["label"] == "Before this task began"
    # Fast chat writes no LangGraph checkpoints — the point is honest
    # about that instead of inventing an id.
    assert entries[0]["checkpoint_id"] == ""
    assert entries[0]["files_written"] == []
    assert entries[1]["step_index"] == 1
    assert entries[1]["label"] == "Reply complete"


@pytest.mark.asyncio
async def test_fast_chat_failed_task_still_records_its_points(store_env):
    sid = session_store.create_session("Chat")
    events = await _collect(run_fast_chat(
        "hi", FailingProvider(), session_id=sid, task_id="fc-task-fail"))
    assert events[-1].type == EventType.TASK_FAILED

    entries = rewind_manifest.get_entries(sid, "fc-task-fail")
    assert [e["step_index"] for e in entries] == [0, 1]
    assert entries[1]["label"] == "Task failed"


@pytest.mark.asyncio
async def test_fast_chat_second_task_becomes_the_rewindable_one(store_env):
    sid = session_store.create_session("Chat")
    await _collect(run_fast_chat("one", ChunkProvider(), session_id=sid, task_id="fc-1"))
    await _collect(run_fast_chat("two", ChunkProvider(), session_id=sid, task_id="fc-2"))

    result = rewind_manifest.get_manifest(sid)
    assert result["task_id"] == "fc-2"
    assert len(result["entries"]) == 2
    # The first task's points are no longer served (last task only).
    assert rewind_manifest.get_manifest(sid, task_id="fc-1")["entries"] == []


# ---------------------------------------------------------------------------
# Graph executor recording (superstep points, real checkpoint ids)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_task_records_points_with_real_checkpoints(graph_env):
    from aether_engine.config import UserConfig

    sid = session_store.create_session("Graph")
    events = await _collect(executor_module.run_langgraph_task(
        "please plan something",
        RoutingProvider(),
        tools=ToolRegistry(),
        task_id="graph-task-1",
        request_id="req-graph-1",
        session_id=sid,
        user_config=UserConfig(),
    ))
    assert events[-1].type == EventType.TASK_COMPLETED

    entries = rewind_manifest.get_entries(sid, "graph-task-1")
    assert len(entries) >= 2
    assert entries[0]["step_index"] == 0
    assert entries[0]["label"] == "Before this task began"
    assert [e["step_index"] for e in entries] == list(range(len(entries)))
    assert all(e["files_written"] == [] for e in entries)

    # Every later point carries a checkpoint id that really exists in
    # the LangGraph checkpoint store for this session's thread.
    checkpoint_ids = {e["checkpoint_id"] for e in entries[1:]}
    assert checkpoint_ids and all(checkpoint_ids)
    with sqlite3.connect(graph_env / "checkpoints.db") as conn:
        stored = {row[0] for row in conn.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = ?", (sid,))}
    assert checkpoint_ids <= stored


@pytest.mark.asyncio
async def test_graph_task_records_files_written_so_far(graph_env):
    from aether_engine.config import UserConfig

    sid = session_store.create_session("Graph write")
    # The REAL builtin write_file, executed by the worker in-process
    # (graph_env): pre-flight write validation (Epic 2) only permits
    # writes inside the workspace roots, which the executor takes as
    # its own argument (the tool node reads them from the graph
    # config), so the write lands under the tmp workspace root.
    target = graph_env / "notes" / "out.txt"
    events = await _collect(executor_module.run_langgraph_task(
        "please write a file",
        WritingProvider(str(target)),
        tools=build_tool_registry(),
        task_id="graph-task-write",
        request_id="req-graph-write",
        session_id=sid,
        user_config=UserConfig(approval_mode="write"),
        workspace_roots=[str(graph_env)],
    ))
    assert events[-1].type == EventType.TASK_COMPLETED
    assert target.read_text(encoding="utf-8") == "hello"

    entries = rewind_manifest.get_entries(sid, "graph-task-write")
    assert entries[0]["files_written"] == []
    assert entries[-1]["files_written"] == [str(target)]


# ---------------------------------------------------------------------------
# Websocket contract
# ---------------------------------------------------------------------------


@pytest.fixture
def ws_env(store_env, monkeypatch):
    """Engine state isolated to tmp, mirroring test_epic1_fast_chat."""
    engine_state.token_path = store_env / "engine.token"
    engine_state.audit_logger.log_path = store_env / "audit.log"
    engine_state.initialize_auth()
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))
    return {"tmp_path": store_env}


def _get_manifest(ws, session_id, task_id=None):
    payload = {"session_id": session_id}
    if task_id is not None:
        payload["task_id"] = task_id
    ws.send_text(Event(
        type=EventType.REWIND_MANIFEST_GET_REQUEST, payload=payload).to_json())
    return Event.from_json(ws.receive_text())


def test_ws_rewind_manifest_get_contract(ws_env):
    sid = session_store.create_session("WS")
    rewind_manifest.record_point(sid, "task-old", 0, "cp-0", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-new", 0, "cp-1", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-new", 1, "cp-2", "After planner", ["x.txt"])

    with TestClient(app) as client:
        # The app lifespan rotates the auth token at startup; read it
        # only after the client (and therefore the lifespan) is up.
        # The query is built by concatenation, not an f-string.
        url = "/ws/tasks?token=" + engine_state.auth_token
        with client.websocket_connect(url) as ws:
            ws.receive_text()  # HELLO

            resp = _get_manifest(ws, sid)
            assert resp.type == EventType.REWIND_MANIFEST_GET_RESPONSE
            assert resp.payload["session_id"] == sid
            assert resp.payload["task_id"] == "task-new"
            assert [e["step_index"] for e in resp.payload["entries"]] == [0, 1]
            assert resp.payload["entries"][1]["files_written"] == ["x.txt"]

            older = _get_manifest(ws, sid, task_id="task-old")
            assert older.type == EventType.REWIND_MANIFEST_GET_RESPONSE
            assert older.payload["entries"] == []

            unknown = _get_manifest(ws, "no-such-session")
            assert unknown.payload["task_id"] is None
            assert unknown.payload["entries"] == []
