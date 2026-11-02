"""Epic 8, E8-1.1b: rewind execution + 5-second undo/delete.

E8-1.1a recorded the per-task rewind manifest; this slice EXECUTES
against it (grill Q2/Q3/Q5):

  * Rewind is STATE-ONLY — conversation rows and LangGraph checkpoints
    roll back to the chosen point; files on disk are never touched.
  * Points exist for the session's LAST task only; the earliest point
    is "before this task began".
  * The discarded steps are held for a 5-second undo window: Undo
    restores them in full; expiry — or a new message, which commits
    through the same path — prunes them from SQLite outright.

Layers covered:
  * rewind.service — staging (hard rollback + backup), undo, commit,
    validation refusals, the async expiry timer.
  * routes.websocket — TASK_REWIND / TASK_REWIND_UNDO contracts.

All providers are scripted stubs (house pattern): no network.
"""
import asyncio
import sqlite3
import time
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
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.rewind import manifest as rewind_manifest
from aether_engine.rewind import service as rewind_service
from aether_engine.routes import websocket as ws_module
from aether_engine.tools.registry import build_tool_registry

# ---------------------------------------------------------------------------
# Fixtures / stubs (mirroring test_epic8_rewind_manifest.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    """Session store (and therefore the manifest + rewind backups,
    which share its DB) isolated to a tmp memory.db."""
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    return tmp_path


class ChunkProvider:
    """Fast-chat stub: yields fixed chunks, completes."""

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        for text in ("Hello ", "there", "!"):
            yield StreamChunk(text=text)
        yield StreamChunk(finish_reason="stop")


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
    # exercises the identical worker code path, minus isolation.
    from aether_engine.workers import sandbox
    from aether_worker.__main__ import run_worker

    async def _in_process_worker(tool_name, args, task_class="standard",
                                 audit_logger=None):
        return await run_worker({"tool_name": tool_name, "args": args})

    monkeypatch.setattr(sandbox, "execute_tool_in_worker", _in_process_worker)
    return store_env


async def _collect(agen):
    return [event async for event in agen]


async def _run_writing_task(graph_env, sid, task_id="graph-task-write"):
    """Run the real writing task the way the websocket layer drives
    it: the user row is saved before the run, the assistant response
    row after TASK_COMPLETED. Returns (target_path, manifest entries).
    """
    from aether_engine.config import UserConfig

    target = graph_env / "notes" / "out.txt"
    session_store.add_message(sid, "user", "please write a file")
    events = await _collect(executor_module.run_langgraph_task(
        "please write a file",
        WritingProvider(str(target)),
        tools=build_tool_registry(),
        task_id=task_id,
        request_id=f"req-{task_id}",
        session_id=sid,
        user_config=UserConfig(approval_mode="write"),
        workspace_roots=[str(graph_env)],
    ))
    assert events[-1].type == EventType.TASK_COMPLETED
    session_store.add_message(sid, "assistant", "Wrote the file.")
    entries = rewind_manifest.get_entries(sid, task_id)
    assert len(entries) >= 3, entries
    assert entries[-1]["files_written"] == [str(target)]
    return target, entries


async def _head_checkpoint_id(db_path, thread_id):
    """The checkpoint id LangGraph currently reports as the thread's
    head (None when the thread has no checkpoints)."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        tup = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    if tup is None:
        return None
    return tup.config["configurable"]["checkpoint_id"]


def _stored_checkpoint_ids(db_path, thread_id):
    with sqlite3.connect(db_path) as conn:
        return {row[0] for row in conn.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = ?",
            (thread_id,))}


def _messages(sid):
    sess = session_store.get_session(sid)
    return [(m["role"], m["content"]) for m in sess["messages"]]


# ---------------------------------------------------------------------------
# Staging + undo: the full rollback cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rewind_to_intermediate_point_then_undo_restores_all(graph_env):
    sid = session_store.create_session("Graph write")
    target, entries = await _run_writing_task(graph_env, sid)
    bytes_before = target.read_bytes()
    step1 = entries[1]
    final = entries[-1]
    assert await _head_checkpoint_id(graph_env / "checkpoints.db", sid) == final["checkpoint_id"]

    result = rewind_service.stage_rewind(
        sid, "graph-task-write", step1["step_index"], step1["checkpoint_id"])
    assert result["ok"] is True
    assert result["session_id"] == sid
    assert result["rewound_to_step"] == step1["step_index"]
    assert result["error"] is None
    assert result["undo_expires_at"] > time.time()
    # Files written after step 1 are reported as NOT reverted...
    assert result["files_not_reverted"] == [str(target)]
    # ...and the file itself is byte-identical (state-only rewind).
    assert target.read_bytes() == bytes_before
    # The rollback is real immediately: graph head is the step-1
    # checkpoint and the task's response row is gone, prompt kept.
    assert await _head_checkpoint_id(graph_env / "checkpoints.db", sid) == step1["checkpoint_id"]
    assert _messages(sid) == [("user", "please write a file")]
    # The manifest now ends at the rewound-to point.
    remaining = rewind_manifest.get_entries(sid, "graph-task-write")
    assert [e["step_index"] for e in remaining] == [e["step_index"] for e in entries[:2]]
    assert rewind_service.has_staged(sid) is True

    undo = rewind_service.undo_staged(sid)
    assert undo == {"ok": True, "restored": True, "error": None}
    # Everything is back: head checkpoint, both rows, the full trail.
    assert await _head_checkpoint_id(graph_env / "checkpoints.db", sid) == final["checkpoint_id"]
    assert _messages(sid) == [
        ("user", "please write a file"), ("assistant", "Wrote the file.")]
    restored = rewind_manifest.get_entries(sid, "graph-task-write")
    assert [e["step_index"] for e in restored] == [e["step_index"] for e in entries]
    assert rewind_service.has_staged(sid) is False


@pytest.mark.asyncio
async def test_rewind_then_commit_prunes_discarded_steps(graph_env):
    sid = session_store.create_session("Graph write")
    _target, entries = await _run_writing_task(graph_env, sid)
    step1, step2, final = entries[1], entries[2], entries[-1]
    stored_before = _stored_checkpoint_ids(graph_env / "checkpoints.db", sid)
    assert step2["checkpoint_id"] in stored_before
    assert final["checkpoint_id"] in stored_before

    result = rewind_service.stage_rewind(
        sid, "graph-task-write", step1["step_index"], step1["checkpoint_id"])
    assert result["ok"] is True

    assert rewind_service.commit_staged(sid) is True
    assert rewind_service.has_staged(sid) is False

    # Discarded checkpoints are pruned from SQLite; the target stays.
    stored_after = _stored_checkpoint_ids(graph_env / "checkpoints.db", sid)
    assert step1["checkpoint_id"] in stored_after
    assert step2["checkpoint_id"] not in stored_after
    assert final["checkpoint_id"] not in stored_after
    with sqlite3.connect(graph_env / "checkpoints.db") as conn:
        write_cps = {row[0] for row in conn.execute(
            "SELECT DISTINCT checkpoint_id FROM writes WHERE thread_id = ?", (sid,))}
    assert final["checkpoint_id"] not in write_cps
    # Messages stay rewound and do not reappear on reload.
    assert _messages(sid) == [("user", "please write a file")]
    assert [e["step_index"] for e in rewind_manifest.get_entries(sid, "graph-task-write")] == [0, 1]
    # Undo after the commit has nothing left to restore.
    undo = rewind_service.undo_staged(sid)
    assert undo["ok"] is False
    assert undo["restored"] is False
    assert undo["error"]


@pytest.mark.asyncio
async def test_rewind_to_step_zero_removes_the_whole_task(graph_env):
    sid = session_store.create_session("Graph write")
    target, entries = await _run_writing_task(graph_env, sid)
    bytes_before = target.read_bytes()

    result = rewind_service.stage_rewind(
        sid, "graph-task-write", 0, entries[0]["checkpoint_id"])
    assert result["ok"] is True
    assert result["rewound_to_step"] == 0
    assert result["files_not_reverted"] == [str(target)]
    # Both of the task's rows are discarded; the file is untouched.
    assert _messages(sid) == []
    assert target.read_bytes() == bytes_before

    assert rewind_service.commit_staged(sid) is True
    # The fresh thread's checkpoints all belonged to this task.
    assert await _head_checkpoint_id(graph_env / "checkpoints.db", sid) is None
    assert _stored_checkpoint_ids(graph_env / "checkpoints.db", sid) == set()
    assert _messages(sid) == []


# ---------------------------------------------------------------------------
# Validation refusals
# ---------------------------------------------------------------------------


def test_rewind_refuses_an_older_task(store_env):
    """Rewind points exist only for the LAST task (Q5): asking to
    rewind an earlier task is refused and stages nothing."""
    sid = session_store.create_session("Two tasks")
    session_store.add_message(sid, "user", "one")
    session_store.add_message(sid, "assistant", "first")
    rewind_manifest.record_point(sid, "task-old", 0, "", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-old", 1, "", "Reply complete", [])
    session_store.add_message(sid, "user", "two")
    session_store.add_message(sid, "assistant", "second")
    rewind_manifest.record_point(sid, "task-new", 0, "", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-new", 1, "", "Reply complete", [])

    result = rewind_service.stage_rewind(sid, "task-old", 0, "")
    assert result["ok"] is False
    assert result["error"]
    assert rewind_service.has_staged(sid) is False
    assert len(_messages(sid)) == 4


def test_rewind_refuses_unknown_step_and_checkpoint_mismatch(store_env):
    sid = session_store.create_session("Guarded")
    session_store.add_message(sid, "user", "hi")
    session_store.add_message(sid, "assistant", "hello")
    rewind_manifest.record_point(sid, "task-1", 0, "cp-a", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-1", 1, "cp-b", "Reply complete", [])

    unknown = rewind_service.stage_rewind(sid, "task-1", 7, "cp-b")
    assert unknown["ok"] is False and unknown["error"]

    mismatch = rewind_service.stage_rewind(sid, "task-1", 1, "cp-WRONG")
    assert mismatch["ok"] is False and mismatch["error"]

    assert rewind_service.has_staged(sid) is False
    assert len(_messages(sid)) == 2


def test_second_rewind_refused_while_one_is_pending(store_env):
    sid = session_store.create_session("Pending")
    session_store.add_message(sid, "user", "hi")
    session_store.add_message(sid, "assistant", "hello")
    rewind_manifest.record_point(sid, "task-1", 0, "", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-1", 1, "", "Reply complete", [])

    first = rewind_service.stage_rewind(sid, "task-1", 0, "")
    assert first["ok"] is True
    second = rewind_service.stage_rewind(sid, "task-1", 0, "")
    assert second["ok"] is False and second["error"]
    # The first rewind is still the staged one and can be undone.
    assert rewind_service.undo_staged(sid)["restored"] is True
    assert _messages(sid) == [("user", "hi"), ("assistant", "hello")]


def test_undo_without_a_pending_rewind_is_a_clean_refusal(store_env):
    sid = session_store.create_session("Nothing pending")
    undo = rewind_service.undo_staged(sid)
    assert undo["ok"] is False
    assert undo["restored"] is False
    assert undo["error"]
    assert rewind_service.commit_staged(sid) is False


# ---------------------------------------------------------------------------
# Fast chat: message-level points, no checkpoints touched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fast_chat_rewind_preserves_prior_graph_checkpoints(graph_env):
    sid = session_store.create_session("Mixed")
    _target, g_entries = await _run_writing_task(graph_env, sid, task_id="graph-task-1")
    graph_cps = _stored_checkpoint_ids(graph_env / "checkpoints.db", sid)
    assert graph_cps

    # A later fast-chat task becomes the session's rewindable one.
    session_store.add_message(sid, "user", "hi")
    events = await _collect(run_fast_chat(
        "hi", ChunkProvider(), session_id=sid, task_id="fc-task-2"))
    assert events[-1].type == EventType.TASK_COMPLETED
    session_store.add_message(sid, "assistant", "Hello there!")
    assert len(_messages(sid)) == 4

    result = rewind_service.stage_rewind(sid, "fc-task-2", 0, "")
    assert result["ok"] is True
    assert result["files_not_reverted"] == []
    # Only the fast-chat turn is discarded; the graph task's rows stay.
    assert _messages(sid) == [
        ("user", "please write a file"), ("assistant", "Wrote the file.")]

    assert rewind_service.commit_staged(sid) is True
    # The earlier graph task's checkpoints were never this task's to prune.
    assert _stored_checkpoint_ids(graph_env / "checkpoints.db", sid) == graph_cps
    # Nor were its manifest entries: the graph task's trail is intact.
    assert [e["step_index"] for e in rewind_manifest.get_entries(sid, "graph-task-1")] == \
        [e["step_index"] for e in g_entries]


# ---------------------------------------------------------------------------
# The async shell: the 5-second window itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expiry_commits_and_late_undo_is_refused(store_env):
    sid = session_store.create_session("Expiring")
    session_store.add_message(sid, "user", "hi")
    session_store.add_message(sid, "assistant", "hello")
    rewind_manifest.record_point(sid, "task-1", 0, "", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-1", 1, "", "Reply complete", [])

    result = await rewind_service.rewind(
        sid, "task-1", 0, "", undo_window_seconds=0.2)
    assert result["ok"] is True
    assert _messages(sid) == []

    await asyncio.sleep(0.6)
    assert rewind_service.has_staged(sid) is False
    undo = await rewind_service.undo(sid)
    assert undo["ok"] is False and undo["restored"] is False


@pytest.mark.asyncio
async def test_undo_inside_the_window_cancels_the_expiry(store_env):
    sid = session_store.create_session("Saved by undo")
    session_store.add_message(sid, "user", "hi")
    session_store.add_message(sid, "assistant", "hello")
    rewind_manifest.record_point(sid, "task-1", 0, "", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-1", 1, "", "Reply complete", [])

    result = await rewind_service.rewind(
        sid, "task-1", 0, "", undo_window_seconds=0.3)
    assert result["ok"] is True
    undo = await rewind_service.undo(sid)
    assert undo == {"ok": True, "restored": True, "error": None}

    await asyncio.sleep(0.6)
    # The expiry never fired on the restored state.
    assert _messages(sid) == [("user", "hi"), ("assistant", "hello")]
    assert rewind_service.has_staged(sid) is False


@pytest.mark.asyncio
async def test_new_message_commit_path_prunes_immediately(store_env):
    """The START_TASK hook commits through rewind_service.commit —
    the same drop the expiry timer performs."""
    sid = session_store.create_session("Committed by message")
    session_store.add_message(sid, "user", "hi")
    session_store.add_message(sid, "assistant", "hello")
    rewind_manifest.record_point(sid, "task-1", 0, "", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-1", 1, "", "Reply complete", [])

    result = await rewind_service.rewind(
        sid, "task-1", 0, "", undo_window_seconds=60.0)
    assert result["ok"] is True
    assert await rewind_service.commit(sid) is True
    assert rewind_service.has_staged(sid) is False
    undo = await rewind_service.undo(sid)
    assert undo["restored"] is False
    assert _messages(sid) == []


# ---------------------------------------------------------------------------
# Websocket contracts
# ---------------------------------------------------------------------------


@pytest.fixture
def ws_env(store_env, monkeypatch):
    """Engine state isolated to tmp, mirroring test_epic8_rewind_manifest."""
    engine_state.token_path = store_env / "engine.token"
    engine_state.audit_logger.log_path = store_env / "audit.log"
    engine_state.initialize_auth()
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", store_env / "checkpoints.db")
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))
    return {"tmp_path": store_env}


# Crafted checkpoint chain for the WS test: the service treats
# checkpoint rows opaquely (copy/delete by id + parent chain), so
# dummy blobs are fine — real saver semantics are covered above.
_CP_CHAIN = ["cp-0001", "cp-0002", "cp-0003"]


def _craft_checkpoints(db_path, thread_id):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                parent_checkpoint_id TEXT,
                type TEXT,
                checkpoint BLOB,
                metadata BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS writes (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                task_path TEXT NOT NULL DEFAULT '',
                idx INTEGER NOT NULL,
                channel TEXT NOT NULL,
                type TEXT,
                value BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
            )""")
        parent = None
        for cp in _CP_CHAIN:
            conn.execute(
                "INSERT INTO checkpoints VALUES (?, '', ?, ?, 'msgpack', ?, ?)",
                (thread_id, cp, parent, b"cp", b"md"))
            conn.execute(
                "INSERT INTO writes VALUES (?, '', ?, 'task-x', '', 0, 'messages', 'msgpack', ?)",
                (thread_id, cp, b"w"))
            parent = cp
        conn.commit()


def test_ws_task_rewind_and_undo_contract(ws_env):
    sid = session_store.create_session("WS rewind")
    session_store.add_message(sid, "user", "please write a file")
    session_store.add_message(sid, "assistant", "Wrote the file.")
    _craft_checkpoints(ws_env["tmp_path"] / "checkpoints.db", sid)
    rewind_manifest.record_point(sid, "task-ws", 0, "", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-ws", 1, "cp-0002", "After planner", ["a.txt"])
    rewind_manifest.record_point(sid, "task-ws", 2, "cp-0003", "Task complete", ["a.txt"])

    with TestClient(app) as client:
        url = "/ws/tasks"
        with client.websocket_connect(url, headers={"Authorization": f"Bearer {engine_state.auth_token}"}) as ws:
            ws.receive_text()  # HELLO

            # An older task id is refused over the wire too.
            ws.send_text(Event(
                type=EventType.TASK_REWIND_REQUEST,
                payload={"session_id": sid, "task_id": "task-old",
                         "step_index": 0, "checkpoint_id": ""}).to_json())
            refused = Event.from_json(ws.receive_text())
            assert refused.type == EventType.TASK_REWIND_RESPONSE
            assert refused.payload["ok"] is False
            assert refused.payload["error"]

            ws.send_text(Event(
                type=EventType.TASK_REWIND_REQUEST,
                payload={"session_id": sid, "task_id": "task-ws",
                         "step_index": 1, "checkpoint_id": "cp-0002"}).to_json())
            resp = Event.from_json(ws.receive_text())
            assert resp.type == EventType.TASK_REWIND_RESPONSE
            assert resp.payload["ok"] is True
            assert resp.payload["session_id"] == sid
            assert resp.payload["rewound_to_step"] == 1
            assert resp.payload["files_not_reverted"] == []
            assert resp.payload["undo_expires_at"] > time.time()
            assert resp.payload["error"] is None
            # Rolled back: response row held, cp-0003 pruned from the live table.
            assert _messages(sid) == [("user", "please write a file")]
            assert _stored_checkpoint_ids(
                ws_env["tmp_path"] / "checkpoints.db", sid) == {"cp-0001", "cp-0002"}

            ws.send_text(Event(
                type=EventType.TASK_REWIND_UNDO_REQUEST,
                payload={"session_id": sid}).to_json())
            undo = Event.from_json(ws.receive_text())
            assert undo.type == EventType.TASK_REWIND_UNDO_RESPONSE
            assert undo.payload == {"ok": True, "restored": True, "error": None}
            assert _messages(sid) == [
                ("user", "please write a file"), ("assistant", "Wrote the file.")]
            assert _stored_checkpoint_ids(
                ws_env["tmp_path"] / "checkpoints.db", sid) == set(_CP_CHAIN)
