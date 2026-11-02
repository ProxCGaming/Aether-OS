"""Epic 8, E8-1.2: fork from anywhere.

Fork copies a session up to a completed step boundary into a NEW
session named "Fork of <title>" (grill Q6): any step, any task —
the last-task restriction is rewind-only. The source is never
altered, and the fork is immediately continuable: when the boundary
is a manifest point of the last task, the LangGraph checkpoint
thread is copied up to that point under the fork's own thread id;
at older boundaries the fork's thread starts empty and the executor
re-seeds it from the copied messages (the edit-and-resend flow).

Layers covered:
  * session_store.fork_session — the message-copy primitive.
  * rewind.fork.fork_session_at — boundary resolution, checkpoint
    copying, naming, refusals.
  * routes.websocket — the SESSION_FORK contract.

All providers are scripted stubs (house pattern): no network.
"""
import hashlib
import sqlite3

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
from aether_engine.rewind import fork as fork_service
from aether_engine.rewind import manifest as rewind_manifest
from aether_engine.routes import websocket as ws_module
from aether_engine.tools.registry import build_tool_registry

# ---------------------------------------------------------------------------
# Fixtures / stubs (mirroring test_epic8_rewind_execution.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    """Session store (and therefore the manifest, which shares its
    DB) isolated to a tmp memory.db."""
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
        episodic_module, "store_episode",
        _async_none())
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts",
        _async_none())
    from aether_engine.langgraph.nodes import planner as planner_module
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "test/artifact.txt")
    # The subprocess sandbox needs Windows Job Objects (pywin32),
    # absent on this Linux VM; dispatching to run_worker in-process
    # exercises the identical worker code path, minus isolation.
    from aether_engine.workers import sandbox
    from aether_worker.__main__ import run_worker

    async def _in_process_worker(tool_name, args, task_class="standard",
                                 audit_logger=None, task_classes=None):
        return await run_worker({"tool_name": tool_name, "args": args})

    monkeypatch.setattr(sandbox, "execute_tool_in_worker", _in_process_worker)
    return store_env


def _async_none():
    from unittest.mock import AsyncMock

    return AsyncMock(return_value=None)


async def _collect(agen):
    return [event async for event in agen]


async def _run_writing_task(graph_env, sid, task_id):
    """Run the real writing task the way the websocket layer drives
    it: the user row is saved before the run, the assistant response
    row after TASK_COMPLETED. Returns (target_path, manifest entries).
    """
    from aether_engine.config import UserConfig

    target = graph_env / "notes" / f"out-{task_id}.txt"
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
    if not db_path.exists():
        return set()
    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "checkpoints" not in tables:
            return set()
        return {row[0] for row in conn.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = ?",
            (thread_id,))}


def _messages(sid):
    sess = session_store.get_session(sid)
    return [(m["role"], m["content"]) for m in sess["messages"]]


def _source_fingerprint(store_env, cp_db, sid):
    """Hash of everything the fork must leave byte-identical: the
    source's session row, its message rows, and its checkpoint +
    writes rows (the acceptance criteria's row/hash check)."""
    digest = hashlib.sha256()
    with sqlite3.connect(store_env / "memory.db") as conn:
        for row in conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (sid,)):
            digest.update(repr(row).encode())
        for row in conn.execute(
                "SELECT rowid, * FROM session_messages WHERE session_id = ? "
                "ORDER BY rowid", (sid,)):
            digest.update(repr(row).encode())
    if cp_db.exists():
        with sqlite3.connect(cp_db) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            for table in ("checkpoints", "writes"):
                if table in tables:
                    for row in conn.execute(
                            f"SELECT rowid, * FROM {table} WHERE thread_id = ? "
                            "ORDER BY rowid", (sid,)):
                        digest.update(repr(row).encode())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# session_store.fork_session: the copy primitive
# ---------------------------------------------------------------------------


def test_store_fork_copies_prefix_with_fresh_ids(store_env):
    sid = session_store.create_session("Original")
    ids = [
        session_store.add_message(sid, "user", "one"),
        session_store.add_message(sid, "assistant", "first"),
        session_store.add_message(sid, "user", "two"),
        session_store.add_message(sid, "assistant", "second"),
    ]

    result = session_store.fork_session(sid, ids[1], "Fork of Original")
    assert result is not None
    assert result["title"] == "Fork of Original"
    assert result["messages_copied"] == 2

    fork = session_store.get_session(result["new_session_id"])
    assert fork["title"] == "Fork of Original"
    assert [(m["role"], m["content"]) for m in fork["messages"]] == [
        ("user", "one"), ("assistant", "first")]
    # Copied rows carry fresh ids — message ids are globally unique.
    assert {m["id"] for m in fork["messages"]}.isdisjoint(ids)
    # The source is untouched.
    assert _messages(sid) == [
        ("user", "one"), ("assistant", "first"),
        ("user", "two"), ("assistant", "second")]


def test_store_fork_whole_session_and_unresolvable_cuts(store_env):
    sid = session_store.create_session("Whole")
    session_store.add_message(sid, "user", "one")
    session_store.add_message(sid, "assistant", "first")
    other = session_store.create_session("Other")
    foreign_id = session_store.add_message(other, "user", "not yours")

    whole = session_store.fork_session(sid, None, "Copy")
    assert whole is not None and whole["messages_copied"] == 2

    # Unknown source, unknown cut, and a cut id that lives in ANOTHER
    # session all refuse with None (no row created).
    assert session_store.fork_session("no-such-session", None, "X") is None
    assert session_store.fork_session(sid, "no-such-message", "X") is None
    assert session_store.fork_session(sid, foreign_id, "X") is None
    titles = [s["title"] for s in session_store.list_sessions()]
    assert titles.count("X") == 0


# ---------------------------------------------------------------------------
# Step forks (manifest of the last task): messages + checkpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fork_at_intermediate_step_copies_state_up_to_the_point(graph_env):
    cp_db = graph_env / "checkpoints.db"
    sid = session_store.create_session("Graph write")
    session_store.add_message(sid, "user", "earlier question")
    session_store.add_message(sid, "assistant", "earlier answer")
    _target, entries = await _run_writing_task(graph_env, sid, "graph-task-1")
    step1, final = entries[1], entries[-1]
    before = _source_fingerprint(graph_env, cp_db, sid)
    source_head = await _head_checkpoint_id(cp_db, sid)
    assert source_head == final["checkpoint_id"]

    result = fork_service.fork_session_at(sid, step_index=step1["step_index"])
    assert result["ok"] is True
    assert result["error"] is None
    assert result["source_session_id"] == sid
    assert result["title"] == "Fork of Graph write"
    fork_id = result["new_session_id"]
    assert fork_id != sid

    # History ends at the point: the earlier exchange plus the task's
    # prompt — its response belongs to a later step.
    assert result["messages_copied"] == 3
    assert _messages(fork_id) == [
        ("user", "earlier question"), ("assistant", "earlier answer"),
        ("user", "please write a file")]
    # The fork's thread head is the point's checkpoint; only the
    # chain up to it was copied.
    assert await _head_checkpoint_id(cp_db, fork_id) == step1["checkpoint_id"]
    fork_cps = _stored_checkpoint_ids(cp_db, fork_id)
    assert final["checkpoint_id"] not in fork_cps
    assert step1["checkpoint_id"] in fork_cps
    # The source is byte-identical afterwards, head included.
    assert _source_fingerprint(graph_env, cp_db, sid) == before
    assert await _head_checkpoint_id(cp_db, sid) == source_head


@pytest.mark.asyncio
async def test_fork_at_step_zero_ends_before_the_task_began(graph_env):
    cp_db = graph_env / "checkpoints.db"
    sid = session_store.create_session("Two tasks")
    _t1, entries1 = await _run_writing_task(graph_env, sid, "graph-task-1")
    _t2, entries2 = await _run_writing_task(graph_env, sid, "graph-task-2")
    # Step 0 of the second task records the thread head the first
    # task left behind — a real checkpoint, not "".
    assert entries2[0]["checkpoint_id"] == entries1[-1]["checkpoint_id"]

    result = fork_service.fork_session_at(sid, step_index=0)
    assert result["ok"] is True
    fork_id = result["new_session_id"]
    # History ends before the second task: exactly the first task.
    assert _messages(fork_id) == [
        ("user", "please write a file"), ("assistant", "Wrote the file.")]
    assert await _head_checkpoint_id(cp_db, fork_id) == entries1[-1]["checkpoint_id"]
    # The source keeps both tasks.
    assert len(_messages(sid)) == 4


@pytest.mark.asyncio
async def test_fork_at_step_zero_of_the_only_task_is_an_empty_fork(graph_env):
    cp_db = graph_env / "checkpoints.db"
    sid = session_store.create_session("Only task")
    await _run_writing_task(graph_env, sid, "graph-task-1")

    result = fork_service.fork_session_at(sid, step_index=0)
    assert result["ok"] is True
    assert result["messages_copied"] == 0
    assert _messages(result["new_session_id"]) == []
    assert await _head_checkpoint_id(cp_db, result["new_session_id"]) is None
    assert len(_messages(sid)) == 2


@pytest.mark.asyncio
async def test_fork_at_final_step_is_continuable_and_isolated(graph_env):
    cp_db = graph_env / "checkpoints.db"
    sid = session_store.create_session("Graph write")
    _target, entries = await _run_writing_task(graph_env, sid, "graph-task-1")
    final = entries[-1]
    before = _source_fingerprint(graph_env, cp_db, sid)

    result = fork_service.fork_session_at(
        sid, step_index=final["step_index"])
    assert result["ok"] is True
    fork_id = result["new_session_id"]
    assert _messages(fork_id) == _messages(sid)
    assert await _head_checkpoint_id(cp_db, fork_id) == final["checkpoint_id"]

    # The fork immediately runs a new task of its own...
    _t, fork_entries = await _run_writing_task(
        graph_env, fork_id, "fork-task-2")
    assert await _head_checkpoint_id(cp_db, fork_id) == \
        fork_entries[-1]["checkpoint_id"]
    assert len(_messages(fork_id)) == 4
    # ...and nothing leaks back into the source: not its messages,
    # not its checkpoints, not a single stored row.
    assert _source_fingerprint(graph_env, cp_db, sid) == before
    assert len(_messages(sid)) == 2
    assert await _head_checkpoint_id(cp_db, sid) == final["checkpoint_id"]


@pytest.mark.asyncio
async def test_fork_at_intermediate_step_then_new_task_runs_in_fork(graph_env):
    sid = session_store.create_session("Graph write")
    _target, entries = await _run_writing_task(graph_env, sid, "graph-task-1")
    step1 = entries[1]

    result = fork_service.fork_session_at(sid, step_index=step1["step_index"])
    assert result["ok"] is True
    fork_id = result["new_session_id"]

    # A fresh task in the mid-task fork completes against the copied
    # state and grows only the fork.
    _t, fork_entries = await _run_writing_task(
        graph_env, fork_id, "fork-task-next")
    assert fork_entries[-1]["step_index"] > 0
    assert _messages(fork_id)[-1] == ("assistant", "Wrote the file.")
    assert len(_messages(sid)) == 2


# ---------------------------------------------------------------------------
# Older-task forks (message boundary) and whole-session forks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fork_from_an_older_task_step_by_message_id(graph_env):
    cp_db = graph_env / "checkpoints.db"
    sid = session_store.create_session("Two tasks")
    await _run_writing_task(graph_env, sid, "graph-task-1")
    await _run_writing_task(graph_env, sid, "graph-task-2")
    first_reply_id = session_store.get_session(sid)["messages"][1]["id"]
    before = _source_fingerprint(graph_env, cp_db, sid)

    result = fork_service.fork_session_at(sid, message_id=first_reply_id)
    assert result["ok"] is True
    fork_id = result["new_session_id"]
    # History ends exactly at the clicked step (inclusive).
    assert _messages(fork_id) == [
        ("user", "please write a file"), ("assistant", "Wrote the file.")]
    # No manifest point covers an older boundary, so no checkpoint
    # state is copied — the fork's thread starts empty and the
    # executor re-seeds it from the copied messages.
    assert await _head_checkpoint_id(cp_db, fork_id) is None
    assert _stored_checkpoint_ids(cp_db, fork_id) == set()

    # The fork accepts a new prompt immediately (fast chat here; the
    # graph path is covered by the step-fork tests).
    events = await _collect(run_fast_chat(
        "hi", ChunkProvider(), session_id=fork_id, task_id="fc-fork"))
    assert events[-1].type == EventType.TASK_COMPLETED
    session_store.add_message(fork_id, "user", "hi")
    session_store.add_message(fork_id, "assistant", "Hello there!")
    assert len(_messages(fork_id)) == 4
    assert _source_fingerprint(graph_env, cp_db, sid) == before


@pytest.mark.asyncio
async def test_whole_session_fork_duplicates_history_and_thread(graph_env):
    cp_db = graph_env / "checkpoints.db"
    sid = session_store.create_session("Whole session")
    _target, entries = await _run_writing_task(graph_env, sid, "graph-task-1")

    result = fork_service.fork_session_at(sid)
    assert result["ok"] is True
    fork_id = result["new_session_id"]
    assert _messages(fork_id) == _messages(sid)
    assert _stored_checkpoint_ids(cp_db, fork_id) == \
        _stored_checkpoint_ids(cp_db, sid)
    assert await _head_checkpoint_id(cp_db, fork_id) == \
        entries[-1]["checkpoint_id"]


def test_fork_naming_default_and_explicit(store_env):
    sid = session_store.create_session("My Chat")
    msg_id = session_store.add_message(sid, "user", "hi")

    default = fork_service.fork_session_at(sid, message_id=msg_id)
    assert default["ok"] is True
    assert default["title"] == "Fork of My Chat"
    assert session_store.get_session(
        default["new_session_id"])["title"] == "Fork of My Chat"

    named = fork_service.fork_session_at(
        sid, message_id=msg_id, new_title="Branch A")
    assert named["ok"] is True
    assert named["title"] == "Branch A"


# ---------------------------------------------------------------------------
# Refusals: nothing is created, nothing changes
# ---------------------------------------------------------------------------


def test_fork_refusals_create_nothing(store_env):
    sid = session_store.create_session("Guarded")
    session_store.add_message(sid, "user", "hi")
    session_store.add_message(sid, "assistant", "hello")
    sessions_before = len(session_store.list_sessions())

    unknown_session = fork_service.fork_session_at("no-such", step_index=0)
    assert unknown_session["ok"] is False and unknown_session["error"]
    assert unknown_session["new_session_id"] == ""

    no_manifest = fork_service.fork_session_at(sid, step_index=0)
    assert no_manifest["ok"] is False and no_manifest["error"]

    unknown_message = fork_service.fork_session_at(sid, message_id="nope")
    assert unknown_message["ok"] is False and unknown_message["error"]

    rewind_manifest.record_point(sid, "task-1", 0, "", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-1", 1, "", "Reply complete", [])
    unknown_step = fork_service.fork_session_at(sid, step_index=9)
    assert unknown_step["ok"] is False and unknown_step["error"]

    bad_step = fork_service.fork_session_at(sid, step_index="1")
    assert bad_step["ok"] is False and bad_step["error"]

    assert len(session_store.list_sessions()) == sessions_before
    assert len(_messages(sid)) == 2


def test_fork_at_fast_chat_points_copies_no_checkpoints(store_env):
    """Fast-chat points carry checkpoint_id "" — forking at one copies
    messages only, even at the final step (rewind's doctrine: an
    earlier graph task's checkpoints are not this task's to copy)."""
    sid = session_store.create_session("Fast chat")
    session_store.add_message(sid, "user", "hi")
    session_store.add_message(sid, "assistant", "hello")
    rewind_manifest.record_point(sid, "fc-1", 0, "", "Before this task began", [])
    rewind_manifest.record_point(sid, "fc-1", 1, "", "Reply complete", [])

    result = fork_service.fork_session_at(sid, step_index=1)
    assert result["ok"] is True
    assert _messages(result["new_session_id"]) == [
        ("user", "hi"), ("assistant", "hello")]


# ---------------------------------------------------------------------------
# Websocket contract
# ---------------------------------------------------------------------------


@pytest.fixture
def ws_env(store_env, monkeypatch):
    """Engine state isolated to tmp, mirroring test_epic8_rewind_execution."""
    from unittest.mock import AsyncMock

    engine_state.token_path = store_env / "engine.token"
    engine_state.audit_logger.log_path = store_env / "audit.log"
    engine_state.initialize_auth()
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", store_env / "checkpoints.db")
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))
    return {"tmp_path": store_env}


# Crafted checkpoint chain for the WS test: the fork service treats
# checkpoint rows opaquely (copy by id + parent chain), so dummy
# blobs are fine — real saver semantics are covered above.
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


def test_ws_session_fork_contract(ws_env):
    sid = session_store.create_session("WS fork")
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

            # An unknown session is refused over the wire too.
            ws.send_text(Event(
                type=EventType.SESSION_FORK_REQUEST,
                payload={"session_id": "no-such", "step_index": 1}).to_json())
            refused = Event.from_json(ws.receive_text())
            assert refused.type == EventType.SESSION_FORK_RESPONSE
            assert refused.payload["ok"] is False
            assert refused.payload["error"]

            ws.send_text(Event(
                type=EventType.SESSION_FORK_REQUEST,
                payload={"session_id": sid, "step_index": 1}).to_json())
            resp = Event.from_json(ws.receive_text())
            assert resp.type == EventType.SESSION_FORK_RESPONSE
            assert resp.payload["ok"] is True
            assert resp.payload["source_session_id"] == sid
            assert resp.payload["title"] == "Fork of WS fork"
            assert resp.payload["messages_copied"] == 1
            assert resp.payload["error"] is None
            fork_id = resp.payload["new_session_id"]
            assert fork_id and fork_id != sid
            # The fork exists, ends at the prompt, and holds exactly
            # the checkpoint chain up to the fork point.
            assert _messages(fork_id) == [("user", "please write a file")]
            assert _stored_checkpoint_ids(
                ws_env["tmp_path"] / "checkpoints.db", fork_id) == {
                    "cp-0001", "cp-0002"}
            # The source keeps everything.
            assert len(_messages(sid)) == 2
            assert _stored_checkpoint_ids(
                ws_env["tmp_path"] / "checkpoints.db", sid) == set(_CP_CHAIN)
