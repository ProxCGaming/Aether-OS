"""Wave 6 (D-1 option B) — pre-resume audit + manual resume.

D-1 (DECIDED 17 Nov 20:19 IST): "Resume, with a pre-resume audit —
before resuming, the engine must check what the task already did
and resume from the checkpoint so nothing overlaps"; the in-flight
step classifies DONE / NOT DONE / UNCERTAIN, an UNCERTAIN step
"stops and asks the owner with specifics, never auto-reruns past
it", and "any tool that will actually run re-gates its approval
fresh".

Covered here:
- the audit's classification matrix over fabricated checkpoint
  slices (the sweep suite's monkeypatch pattern) — DONE evidence,
  NOT DONE (read-only / write-that-left-no-trace / between steps),
  UNCERTAIN (file exists with no completion record, shell, unknown
  tool, uncommitted result), and every refusal gate;
- the recent-tasks audit spine (an interrupted task with no
  episode and no tap entry is listed from the audit trail alone);
- the websocket handlers over a fake connection with the route's
  seams patched on aether_engine.routes.websocket (the house patch
  doctrine): refusal propagation, the confirm_uncertain gate, the
  pending-approval belt-and-braces refusal, provider failure;
- two real-graph integrations: death mid-tool → sweep → audit →
  resume to completion, and approval re-gating on the resumed run.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aether_common.contracts import Event, EventType
from aether_engine import task_ledger
from aether_engine.audit import AuditLogger
from aether_engine.langgraph import resume_audit
from aether_engine.routes import websocket as ws_module
from aether_engine.routes import ws_task_resume


@pytest.fixture(autouse=True)
def _clear_resuming():
    ws_task_resume._RESUMING.clear()
    task_ledger.clear_tap()
    yield
    ws_task_resume._RESUMING.clear()
    task_ledger.clear_tap()


# ── Audit classification over fabricated slices ──────────────────────

def _records(last_state="INTERRUPTED"):
    transitions = []
    if last_state is not None:
        transitions.append(
            {"from_state": "RUNNING", "to_state": last_state,
             "timestamp": 1700000000.0, "source": "audit_log"})
    return {"transitions": transitions, "approvals": [], "events": []}


def _slice(**overrides):
    base = {
        "session_id": "sess-1",
        "prompt": "do the thing",
        "phase": "executing",
        "active_specialist": "planner",
        "delegations": [
            {"agent": "planner", "reason": "plan it", "briefing": "",
             "denied": False, "timestamp": 1700000000.0,
             "source": "checkpoint_state"},
        ],
        "tool_calls": [],
        "files_touched": [],
        "pending_write_channels": [],
        "pending_tool_result": False,
        "next_nodes": ["planner"],
        "in_flight": [],
    }
    base.update(overrides)
    return base


def _in_flight(pending_tool_call=None, tool_calls=None, **overrides):
    entry = {
        "node": "planner",
        "next_nodes": ["execute_tool"],
        "pending_tool_call": pending_tool_call,
        "tool_calls": tool_calls or [],
        "pending_write_channels": [],
        "pending_tool_result": False,
    }
    entry.update(overrides)
    return entry


async def _audit(monkeypatch, slice_, *, last_state="INTERRUPTED", tap=None):
    async def fake_slice(task_id, *, session_id=None, checkpoint_db=None):
        return slice_, False

    monkeypatch.setattr(resume_audit, "checkpoint_slice_for_task", fake_slice)
    monkeypatch.setattr(
        resume_audit, "read_audit_records",
        lambda task_id, *, log_path=None: _records(last_state))
    monkeypatch.setattr(resume_audit, "tap_snapshot", lambda task_id: tap)
    return await resume_audit.build_resume_audit("task-1")


@pytest.mark.asyncio
async def test_audit_ready_when_only_completed_steps(monkeypatch):
    slice_ = _slice(
        next_nodes=[],
        tool_calls=[{"tool_name": "read_file", "tool_args": {"path": "/a"},
                     "status": "completed", "node": None, "edited": False,
                     "timestamp": None, "source": "checkpoint_state"}])
    audit = await _audit(monkeypatch, slice_)
    assert audit["verdict"] == "ready"
    assert audit["in_flight"] is None
    assert [s["kind"] for s in audit["completed_steps"]] == [
        "delegation", "tool_call"]
    assert audit["coverage"]["notes"]  # the settle-without-rerun note


@pytest.mark.asyncio
async def test_audit_between_steps_is_not_done(monkeypatch):
    audit = await _audit(monkeypatch, _slice())
    assert audit["verdict"] == "ready"
    assert audit["in_flight"]["kind"] == "node"
    assert audit["in_flight"]["node"] == "planner"
    assert audit["in_flight"]["classification"] == "not_done"


@pytest.mark.asyncio
async def test_audit_in_flight_read_only_tool_is_not_done(monkeypatch):
    entry = _in_flight(pending_tool_call={
        "name": "read_file", "args": {"path": "/ws/x.txt"},
        "call_id": "call-1"})
    audit = await _audit(monkeypatch, _slice(in_flight=[entry]))
    assert audit["verdict"] == "ready"
    assert audit["in_flight"]["tool_name"] == "read_file"
    assert audit["in_flight"]["classification"] == "not_done"


@pytest.mark.asyncio
async def test_audit_write_with_file_on_disk_is_uncertain(monkeypatch, tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("partial", encoding="utf-8")
    entry = _in_flight(pending_tool_call={
        "name": "write_file",
        "args": {"path": str(target), "content": "partial"},
        "call_id": "call-1"})
    audit = await _audit(monkeypatch, _slice(in_flight=[entry]))
    assert audit["verdict"] == "uncertain"
    assert audit["in_flight"]["classification"] == "uncertain"
    # D-1's own example: "a file exists with no completion record".
    assert "no completion record" in audit["in_flight"]["reason"]


@pytest.mark.asyncio
async def test_audit_write_without_file_is_not_done(monkeypatch, tmp_path):
    entry = _in_flight(pending_tool_call={
        "name": "write_file",
        "args": {"path": str(tmp_path / "absent.txt"), "content": "x"},
        "call_id": "call-1"})
    audit = await _audit(monkeypatch, _slice(in_flight=[entry]))
    assert audit["verdict"] == "ready"
    assert audit["in_flight"]["classification"] == "not_done"


@pytest.mark.asyncio
async def test_audit_in_flight_shell_is_uncertain(monkeypatch):
    audit = await _audit(monkeypatch, _slice(tool_calls=[
        {"tool_name": "execute_shell", "tool_args": {"command": "make it"},
         "status": "pending", "node": None, "edited": False,
         "timestamp": None, "source": "checkpoint_state"}]))
    assert audit["verdict"] == "uncertain"
    assert audit["in_flight"]["tool_name"] == "execute_shell"


@pytest.mark.asyncio
async def test_audit_delete_file_classification(monkeypatch, tmp_path):
    gone = tmp_path / "gone.txt"
    entry = _in_flight(pending_tool_call={
        "name": "delete_file", "args": {"path": str(gone)},
        "call_id": "call-1"})
    audit = await _audit(monkeypatch, _slice(in_flight=[entry]))
    assert audit["in_flight"]["classification"] == "uncertain"

    present = tmp_path / "present.txt"
    present.write_text("x", encoding="utf-8")
    entry = _in_flight(pending_tool_call={
        "name": "delete_file", "args": {"path": str(present)},
        "call_id": "call-1"})
    audit = await _audit(monkeypatch, _slice(in_flight=[entry]))
    assert audit["in_flight"]["classification"] == "not_done"


@pytest.mark.asyncio
async def test_audit_unknown_tool_is_uncertain(monkeypatch):
    entry = _in_flight(pending_tool_call={
        "name": "mcp_some_server_do_something", "args": {},
        "call_id": "call-1"})
    audit = await _audit(monkeypatch, _slice(in_flight=[entry]))
    assert audit["verdict"] == "uncertain"
    assert "cannot verify" in audit["in_flight"]["reason"]


@pytest.mark.asyncio
async def test_audit_uncommitted_result_is_uncertain(monkeypatch):
    entry = _in_flight(
        pending_tool_call={"name": "read_file", "args": {"path": "/x"},
                           "call_id": "call-1"},
        pending_tool_result=True)
    audit = await _audit(monkeypatch, _slice(in_flight=[entry]))
    assert audit["verdict"] == "uncertain"
    assert "uncommitted result" in audit["in_flight"]["reason"]


@pytest.mark.asyncio
async def test_audit_refuses_pending_approval(monkeypatch):
    slice_ = _slice(pending_approvals=[
        {"tool_name": "write_file", "timestamp": None, "pending": True,
         "source": "checkpoint_state"}])
    audit = await _audit(monkeypatch, slice_)
    assert audit["verdict"] == "refused"
    assert "approval" in audit["refusal_reason"]


@pytest.mark.asyncio
async def test_audit_refuses_without_checkpoint(monkeypatch):
    audit = await _audit(monkeypatch, None)
    assert audit["verdict"] == "refused"
    assert "no checkpointed state" in audit["refusal_reason"]


@pytest.mark.asyncio
async def test_audit_refuses_when_not_interrupted(monkeypatch):
    audit = await _audit(monkeypatch, _slice(), last_state="SUCCEEDED")
    assert audit["verdict"] == "refused"
    assert "SUCCEEDED" in audit["refusal_reason"]
    audit = await _audit(monkeypatch, _slice(), last_state=None)
    assert audit["verdict"] == "refused"


@pytest.mark.asyncio
async def test_audit_refuses_when_tap_says_active(monkeypatch):
    audit = await _audit(
        monkeypatch, _slice(), tap={"state": "PENDING_APPROVAL"})
    assert audit["verdict"] == "refused"
    assert "already active" in audit["refusal_reason"]


# ── recent_tasks audit spine ─────────────────────────────────────────

class _EmptyBackend:
    def list_episodes(self, limit=50):
        return []


def test_recent_tasks_lists_interrupted_from_audit_spine(
        tmp_path, monkeypatch):
    from aether_engine.cost.ledger import UsageLedger

    task_ledger.clear_tap()
    log = tmp_path / "audit.log"
    lines = [
        {"type": "TASK_TRANSITION", "ts": 1700000000.0,
         "task_id": "task-dead", "from_state": "PENDING",
         "to_state": "RUNNING", "payload": {}},
        {"type": "TASK_TRANSITION", "ts": 1700000500.0,
         "task_id": "task-dead", "from_state": "RUNNING",
         "to_state": "INTERRUPTED",
         "payload": {"reason": "engine_restart"}},
        {"type": "TASK_TRANSITION", "ts": 1700000600.0,
         "task_id": "task-done", "from_state": "RUNNING",
         "to_state": "SUCCEEDED", "payload": {}},
    ]
    log.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(
        "aether_engine.audit.DEFAULT_AUDIT_LOG_PATH", log)
    rows = task_ledger.recent_tasks(
        20, backend=_EmptyBackend(),
        ledger=UsageLedger(tmp_path / "usage.db"))
    by_id = {row["task_id"]: row for row in rows}
    assert by_id["task-dead"]["state"] == "INTERRUPTED"
    assert by_id["task-dead"]["sources"] == ["audit_log"]
    assert "task-done" not in by_id


# ── Websocket handlers over a fake connection ────────────────────────

class _FakeWs:
    def __init__(self):
        self.frames = []

    async def send_text(self, raw):
        self.frames.append(json.loads(raw))


class _FakeAuditLogger:
    def __init__(self):
        self.events = []

    def log_event(self, action, details=None):
        self.events.append((action, details or {}))


def _fake_conn():
    conn = SimpleNamespace(
        ws=_FakeWs(),
        secret_store=object(),
        audit_logger=_FakeAuditLogger(),
        task_runner=None,
        task_session_id=None,
        execute_task_generator=None,
    )

    async def _pump(gen, p_name, p_model, req_id, session_id=None):
        conn.pump_events = [ev async for ev in gen]

    conn.execute_task_generator = _pump
    conn.pump_events = None
    return conn


def _ready_audit(**overrides):
    audit = {
        "task_id": "task-1", "session_id": "sess-1", "prompt": "p",
        "state": "INTERRUPTED", "verdict": "ready",
        "refusal_reason": None, "completed_steps": [],
        "in_flight": {"kind": "node", "tool_name": None, "tool_args": {},
                      "node": "planner", "classification": "not_done",
                      "reason": "between steps"},
        "files": [], "pending_approvals": [],
        "coverage": {"checkpoint": True, "audit_transitions": 2,
                     "checkpoint_scan_capped": False, "notes": []},
    }
    audit.update(overrides)
    return audit


def _patch_route_seams(monkeypatch, *, audit, run_cfg=None, pending=None,
                       provider_error=False):
    monkeypatch.setattr(
        ws_task_resume, "build_resume_audit",
        AsyncMock(return_value=audit))
    monkeypatch.setattr(
        ws_module, "get_persisted_run_config",
        AsyncMock(return_value=run_cfg or {}))
    monkeypatch.setattr(
        ws_module, "get_pending_approvals",
        AsyncMock(return_value=pending or []))
    if provider_error:
        def _boom(*a, **k):
            raise RuntimeError("no provider")
        monkeypatch.setattr(ws_module, "_create_provider_instance", _boom)
    else:
        monkeypatch.setattr(
            ws_module, "_create_provider_instance",
            lambda *a, **k: object())
    monkeypatch.setattr(
        ws_module, "build_tool_registry", lambda **k: object())
    monkeypatch.setattr(
        "aether_engine.mcp.registry.GLOBAL_MCP_REGISTRY.get_mcp_tools",
        lambda **k: [])
    calls = {}

    async def _fake_resume(**kwargs):
        calls.update(kwargs)
        yield Event(type=EventType.TASK_COMPLETED,
                    payload={"task_id": "task-1"})

    monkeypatch.setattr(ws_module, "continue_langgraph_task", _fake_resume)
    return calls


def _msg(payload, request_id="req-1"):
    return Event(type=EventType.TASK_RESUME_REQUEST,
                 request_id=request_id, payload=payload)


@pytest.mark.asyncio
async def test_audit_handler_requires_task_id():
    conn = _fake_conn()
    msg = Event(type=EventType.TASK_RESUME_AUDIT_REQUEST,
                request_id="r", payload={})
    await ws_task_resume.handle_task_resume_audit_request(conn, msg)
    frame = conn.ws.frames[-1]
    assert frame["type"] == "TASK_RESUME_AUDIT_RESPONSE"
    assert frame["payload"]["success"] is False


@pytest.mark.asyncio
async def test_resume_handler_propagates_refusal(monkeypatch):
    audit = _ready_audit(
        verdict="refused", refusal_reason="no checkpointed state")
    calls = _patch_route_seams(monkeypatch, audit=audit)
    conn = _fake_conn()
    await ws_task_resume.handle_task_resume_request(
        conn, _msg({"task_id": "task-1"}))
    frame = conn.ws.frames[-1]
    assert frame["type"] == "TASK_RESUME_RESPONSE"
    assert frame["payload"]["success"] is False
    assert frame["payload"]["error"] == "no checkpointed state"
    assert calls == {}  # the executor was never invoked


@pytest.mark.asyncio
async def test_resume_handler_uncertain_gate(monkeypatch):
    audit = _ready_audit(
        verdict="uncertain",
        in_flight={"kind": "tool_call", "tool_name": "execute_shell",
                   "tool_args": {}, "node": "planner",
                   "classification": "uncertain",
                   "reason": "a shell command's effects cannot be verified"})
    calls = _patch_route_seams(monkeypatch, audit=audit)
    conn = _fake_conn()
    await ws_task_resume.handle_task_resume_request(
        conn, _msg({"task_id": "task-1"}))
    frame = conn.ws.frames[-1]
    assert frame["payload"]["success"] is False
    assert frame["payload"]["requires_confirmation"] is True
    assert calls == {}

    # With the owner's explicit confirmation the resume proceeds.
    await ws_task_resume.handle_task_resume_request(
        conn, _msg({"task_id": "task-1", "confirm_uncertain": True}))
    frame = conn.ws.frames[-1]
    assert frame["payload"]["success"] is True
    await conn.task_runner  # let the pump settle (it drives the generator)
    assert calls["thread_id"] == "sess-1"
    assert conn.pump_events[-1].type == EventType.TASK_COMPLETED
    logged = dict(conn.audit_logger.events)["TASK_RESUME_REQUESTED"]
    assert logged["confirmed_uncertain"] is True


@pytest.mark.asyncio
async def test_resume_handler_refuses_pending_approval_thread(monkeypatch):
    audit = _ready_audit()
    calls = _patch_route_seams(
        monkeypatch, audit=audit,
        pending=[{"thread_id": "sess-1", "tool_name": "write_file"}])
    conn = _fake_conn()
    await ws_task_resume.handle_task_resume_request(
        conn, _msg({"task_id": "task-1"}))
    frame = conn.ws.frames[-1]
    assert frame["payload"]["success"] is False
    assert "approval" in frame["payload"]["error"]
    assert calls == {}


@pytest.mark.asyncio
async def test_resume_handler_provider_failure_is_honest(monkeypatch):
    audit = _ready_audit()
    _patch_route_seams(monkeypatch, audit=audit, provider_error=True)
    conn = _fake_conn()
    await ws_task_resume.handle_task_resume_request(
        conn, _msg({"task_id": "task-1"}))
    frame = conn.ws.frames[-1]
    assert frame["payload"]["success"] is False
    assert "provider" in frame["payload"]["error"]


@pytest.mark.asyncio
async def test_resume_handler_ready_path(monkeypatch):
    audit = _ready_audit()
    run_cfg = {"session_id": "sess-1", "provider_name": "taskprov",
               "model": "task-model", "forced_agent": "planner",
               "agent_models": {"planner": "taskprov:task-model"},
               "workspace_roots": ["/ws"]}
    calls = _patch_route_seams(monkeypatch, audit=audit, run_cfg=run_cfg)
    conn = _fake_conn()
    await ws_task_resume.handle_task_resume_request(
        conn, _msg({"task_id": "task-1"}))
    frame = conn.ws.frames[-1]
    assert frame["payload"]["success"] is True
    assert frame["payload"]["session_id"] == "sess-1"
    await conn.task_runner
    # The task's own snapshot configuration drives the resume.
    assert calls["thread_id"] == "sess-1"
    assert calls["session_id"] == "sess-1"
    assert calls["forced_agent"] == "planner"
    assert calls["workspace_roots"] == ["/ws"]
    await asyncio.sleep(0)
    assert "task-1" not in ws_task_resume._RESUMING


# ── Real-graph integrations ──────────────────────────────────────────
#
# Death is simulated the way it actually happens to the checkpoint:
# the run's tool blocks mid-execution and the awaiting task is
# cancelled — the on-disk checkpoint keeps the last committed
# superstep, exactly what a killed process leaves behind. The
# startup sweep then marks the task INTERRUPTED, and the resume
# goes through the real handler + real pump + real executor
# continuation.

from functools import partial

from aether_engine.config import UserConfig
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.orphan_sweep import sweep_orphaned_tasks
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.routes import ws_task_exec
from aether_engine.state import engine_state
from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk


class _DeathProvider:
    """Supervisor delegates to planner; the planner's first call
    requests read_file (which blocks — the "death"), its second the
    gated probe tool, later calls answer."""

    def __init__(self):
        self.provider_name = "taskprov"
        self.model = "task-model"
        self._planner_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None,
                          **kwargs):
        if node_name == "planner":
            self._planner_calls += 1
            if self._planner_calls == 1:
                yield StreamChunk(
                    tool_calls=[ToolCall(
                        name="read_file",
                        args={"path": self.read_path},
                        call_id="call-death-1")],
                    finish_reason="tool_calls")
            elif self._planner_calls == 2:
                yield StreamChunk(
                    tool_calls=[ToolCall(
                        name="gated_probe", args={"q": "x"},
                        call_id="call-death-2")],
                    finish_reason="tool_calls")
            else:
                yield StreamChunk(
                    text="Specialist final answer.", finish_reason="stop")
            return
        yield StreamChunk(
            text='{"next": "planner", "reason": "death probe", '
                 '"briefing": ""}',
            finish_reason="stop")


@pytest.fixture
def death_env(tmp_path, monkeypatch):
    db = tmp_path / "checkpoints.db"
    monkeypatch.setattr(executor_module, "DEFAULT_CHECKPOINT_DB", db)
    monkeypatch.setattr(
        "aether_engine.langgraph.checkpointer.DEFAULT_CHECKPOINT_DB", db)
    monkeypatch.setattr(
        "aether_engine.audit.DEFAULT_AUDIT_LOG_PATH",
        tmp_path / "audit.log")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "probe/plan.txt")
    log = tmp_path / "audit.log"
    audit_logger = AuditLogger(log_path=log)
    monkeypatch.setattr(engine_state, "audit_logger", audit_logger)

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    target = ws_dir / "input.txt"
    target.write_text("hello", encoding="utf-8")

    entered = asyncio.Event()
    calls = {"read_file": 0, "gated_probe": 0}

    async def _read(**kwargs):
        calls["read_file"] += 1
        if calls["read_file"] == 1:
            entered.set()
            await asyncio.Event().wait()  # blocks until cancelled
        return "file-contents"

    def _gated(**kwargs):
        calls["gated_probe"] += 1
        return "gated-result"

    registry = ToolRegistry()
    registry.register(Tool(
        name="read_file", description="probe read",
        execute_fn=_read, risk=ToolRisk.READ_ONLY))
    registry.register(Tool(
        name="gated_probe", description="probe gated",
        execute_fn=_gated, risk=ToolRisk.READ_ONLY))
    return SimpleNamespace(
        tmp_path=tmp_path, db=db, log=log, audit_logger=audit_logger,
        ws_dir=ws_dir, target=target, entered=entered, calls=calls,
        registry=registry)


async def _kill_mid_tool(env, provider, *, user_config):
    """Run a task whose read_file blocks; cancel = process death."""
    session_id = "sess-death"
    task_id = "task-death"
    gen = executor_module.run_langgraph_task(
        "read the file, then probe", provider, tools=env.registry,
        task_id=task_id, request_id="req-death",
        session_id=session_id, user_config=user_config,
        workspace_roots=[str(env.ws_dir)])
    run_task = asyncio.create_task(_drain(gen))
    await asyncio.wait_for(env.entered.wait(), timeout=30)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    env.audit_logger.log_task_transition(
        task_id, TaskState.PENDING, TaskState.RUNNING, {})
    result = await sweep_orphaned_tasks(
        audit_logger=env.audit_logger, audit_log_path=env.log,
        checkpoint_db=env.db)
    assert result["swept"] == [task_id]
    return task_id, session_id


async def _drain(gen):
    events = []
    async for ev in gen:
        events.append(ev)
    return events


from aether_common.contracts import TaskState


def _real_conn():
    conn = _fake_conn()
    conn.active_task_id = None
    conn.background_tasks = set()

    def _spawn(coro):
        task = asyncio.create_task(coro)
        conn.background_tasks.add(task)
        task.add_done_callback(conn.background_tasks.discard)
        return task

    conn.spawn_background = _spawn
    conn.execute_task_generator = partial(
        ws_task_exec.execute_task_generator, conn)
    return conn


def _patch_real_resume_seams(monkeypatch, env, provider):
    monkeypatch.setattr(
        ws_module, "_create_provider_instance",
        lambda *a, **k: provider)
    monkeypatch.setattr(
        ws_module, "build_tool_registry",
        lambda **k: env.registry)
    monkeypatch.setattr(
        "aether_engine.mcp.registry.GLOBAL_MCP_REGISTRY.get_mcp_tools",
        lambda **k: [])


@pytest.mark.asyncio
async def test_real_death_audit_and_resume_to_completion(
        death_env, monkeypatch):
    env = death_env
    provider = _DeathProvider()
    provider.read_path = str(env.target)
    task_id, _session_id = await _kill_mid_tool(
        env, provider,
        user_config=UserConfig(
            tool_policies={"gated_probe": "Always Allow"}))

    # The audit sees the real checkpoint: the delegation is DONE
    # evidence; the blocked read_file is the in-flight step and —
    # read-only, no result — NOT DONE.
    audit = await resume_audit.build_resume_audit(
        task_id, audit_path=env.log, checkpoint_db=env.db)
    assert audit["verdict"] == "ready", audit
    assert audit["in_flight"]["tool_name"] == "read_file"
    assert audit["in_flight"]["classification"] == "not_done"
    assert any(s["kind"] == "delegation"
               for s in audit["completed_steps"])

    # Resume through the real handler/pump/executor. The audit +
    # checkpoint inside the handler resolve through the executor's
    # patched checkpoint DB (the handler passes no explicit paths —
    # production defaults, which the fixture pointed at tmp).
    _patch_real_resume_seams(monkeypatch, env, provider)
    conn = _real_conn()
    msg = Event(type=EventType.TASK_RESUME_REQUEST, request_id="r1",
                payload={"task_id": task_id})
    await ws_task_resume.handle_task_resume_request(conn, msg)
    frame = conn.ws.frames[-1]
    assert frame["type"] == "TASK_RESUME_RESPONSE"
    assert frame["payload"]["success"] is True, frame
    await conn.task_runner

    # The blocked call re-ran and completed; the task settled.
    assert env.calls["read_file"] == 2
    transitions = task_ledger.read_audit_records(
        task_id, log_path=env.log)["transitions"]
    assert transitions[-1]["to_state"] == "SUCCEEDED"
    # And a second resume is refused: the task is no longer
    # interrupted.
    audit_after = await resume_audit.build_resume_audit(
        task_id, audit_path=env.log, checkpoint_db=env.db)
    assert audit_after["verdict"] == "refused"


@pytest.mark.asyncio
async def test_real_resume_regates_approvals(death_env, monkeypatch):
    env = death_env
    provider = _DeathProvider()
    provider.read_path = str(env.target)
    # gated_probe requires approval; read_file does not.
    user_config = UserConfig(
        tool_policies={"gated_probe": "Require Approval"})
    task_id, _session_id = await _kill_mid_tool(
        env, provider, user_config=user_config)

    _patch_real_resume_seams(monkeypatch, env, provider)
    conn = _real_conn()
    events = []
    # Keep the resumed run's events: wrap the resume generator at
    # the websocket seam (the pump forwards frames to the socket,
    # so the generator is where the event stream is observable).
    real_resume = ws_module.continue_langgraph_task

    async def _tapped_resume(**kwargs):
        async for ev in real_resume(**kwargs):
            events.append(ev)
            yield ev

    monkeypatch.setattr(ws_module, "continue_langgraph_task", _tapped_resume)
    msg = Event(type=EventType.TASK_RESUME_REQUEST, request_id="r1",
                payload={"task_id": task_id})
    await ws_task_resume.handle_task_resume_request(conn, msg)
    assert conn.ws.frames[-1]["payload"]["success"] is True
    await conn.task_runner

    # The resumed run completed read_file, then STOPPED at the
    # approval gate for gated_probe: it asked fresh instead of
    # executing on any inherited say-so.
    types = [ev.type for ev in events]
    assert EventType.TOOL_APPROVAL_REQUEST in types
    assert EventType.TASK_INTERRUPTED in types
    assert EventType.TASK_COMPLETED not in types
    assert env.calls["gated_probe"] == 0
