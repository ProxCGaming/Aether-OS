"""Master build S3 — Task Ledger read model (Mission Control core).

The ledger is a READ MODEL over the five existing writers, not a
sixth store (plan R7): checkpointed graph state, the executor's event
stream (run AND resume paths, via the bounded in-memory tap),
episodic memory, the audit trail, and the usage ledger joined by
task_id + agent_name.

These tests build a task's worth of records across the REAL writers
— a real UsageLedger, a real AuditLogger, a real episodic row read
back through the memory facade, real executor runs (scripted
providers, the Epic 1 parity pattern) whose checkpoints the ledger
then reads — and prove the joined summary answers who did what, on
which models, at what cost, with what approvals, files, tests and
errors, and what the outcome was. An unknown task must answer an
honest not-found, never fabricated data.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from aether_common.contracts import Event, EventType, TaskState
from aether_engine import task_ledger
from aether_engine.audit import AuditLogger
from aether_engine.config import UserConfig
from aether_engine.cost.ledger import UsageLedger
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk

# A uuid-shaped id, as production mints: the audit trail's secret
# scrubber redacts any string containing "sk-..." (an API-key shape),
# which a synthetic "task-..." id would trip — uuid hex cannot.
TASK_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


@pytest.fixture(autouse=True)
def _clean_tap():
    task_ledger.clear_tap()
    yield
    task_ledger.clear_tap()


# ── Real-writer fixtures ─────────────────────────────────────────────

@pytest.fixture
def usage(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.db")
    ledger.record_call(
        model_name="gemini-3.5-flash", provider_name="google_gemini",
        input_tokens=1000, output_tokens=200, cost_usd=0.004,
        session_id="sess-s3", task_id=TASK_ID, agent_name="coder")
    ledger.record_call(
        model_name="gemini-3.5-flash", provider_name="google_gemini",
        input_tokens=500, output_tokens=100, cost_usd=0.002,
        session_id="sess-s3", task_id=TASK_ID, agent_name="coder")
    ledger.record_call(
        model_name="local-model", provider_name="ollama",
        input_tokens=300, output_tokens=50, cost_usd=0.0, is_local=True,
        session_id="sess-s3", task_id=TASK_ID, agent_name="researcher")
    return ledger


@pytest.fixture
def audit_log(tmp_path):
    path = tmp_path / "logs" / "audit.log"
    audit = AuditLogger(log_path=path)
    audit.log_task_transition(TASK_ID, TaskState.PENDING, TaskState.RUNNING, {})
    audit.log_event("APPROVAL_REQUESTED", {
        "tool_name": "write_file", "task_id": TASK_ID,
        "args": {"path": "src/app.py"}})
    audit.log_task_transition(TASK_ID, TaskState.RUNNING, TaskState.SUCCEEDED, {})
    return path


def _store_real_episode(task_id=TASK_ID):
    """Write the episode through the episodic store's own writer and
    read it back via the facade's native backend (the production read
    path the ledger uses)."""
    episodic_module.init_episodic_db()
    episodic_module._write_episode_row(
        task_id, "build the thing", "Built the thing successfully",
        "task,completed", "The thing was built and verified.",
        "[TOOL] write_file -> completed", 1_763_000_000)
    from aether_engine.memory.facade import NativeMemoryBackend

    return NativeMemoryBackend()


def _tap_run_path_events(task_id=TASK_ID):
    """The event sequence the RUN path emits for this task (delegation,
    tool activity incl. a file write and a test run, an approval ask,
    terminal completion) — fed through the tap exactly as the
    executor's emit funnel delivers them."""
    def ev(type_, **payload):
        return Event(type=type_, request_id="req-1",
                     payload={"task_id": task_id, "session_id": "sess-s3", **payload})

    task_ledger.record_executor_event(task_id, ev(EventType.TASK_CREATED, state="RUNNING"))
    task_ledger.record_executor_event(task_id, ev(
        EventType.NODE_ACTIVITY, from_node="supervisor", to_node="coder",
        action="delegation", reason="needs code", briefing="build it",
        output_payload={"decision": {"next": "coder", "reason": "needs code"}},
        status="completed", timestamp=1_763_000_010))
    task_ledger.record_executor_event(task_id, ev(
        EventType.TOOL_APPROVAL_REQUEST, tool_name="write_file",
        timestamp=1_763_000_020))
    task_ledger.record_executor_event(task_id, ev(
        EventType.TOOL_ACTIVITY, tool_name="write_file",
        tool_args={"path": "src/app.py"}, status="pending", node="coder",
        timestamp=1_763_000_030))
    task_ledger.record_executor_event(task_id, ev(
        EventType.TOOL_ACTIVITY, tool_name="write_file",
        tool_args={"path": "src/app.py"}, status="completed", node="coder",
        timestamp=1_763_000_031))
    task_ledger.record_executor_event(task_id, ev(
        EventType.TOOL_ACTIVITY, tool_name="execute_shell",
        tool_args={"command": "python -m pytest tests/ -q"},
        status="completed", node="coder", timestamp=1_763_000_040))
    task_ledger.record_executor_event(task_id, ev(
        EventType.TASK_COMPLETED, state="SUCCEEDED", latency_ms=1234.0))


# ── The joined summary across the real writers ───────────────────────

@pytest.mark.asyncio
async def test_summary_joins_all_five_writers(usage, audit_log, tmp_path):
    backend = _store_real_episode()
    _tap_run_path_events()

    summary = await task_ledger.task_summary(
        TASK_ID, session_id="sess-s3", backend=backend, ledger=usage,
        audit_path=audit_log, checkpoint_db=tmp_path / "no-checkpoints.db")

    assert summary["found"] is True
    assert summary["session_id"] == "sess-s3"
    assert summary["prompt"] == "build the thing"
    assert summary["state"] == "SUCCEEDED"
    assert summary["latency_ms"] == 1234.0

    # Who did what — the delegation, from the tapped executor stream.
    (delegation,) = summary["delegations"]
    assert delegation["agent"] == "coder"
    assert delegation["reason"] == "needs code"
    assert delegation["denied"] is False

    # Tool calls, with the pending/completed pair merged into one.
    by_name = {c["tool_name"]: c for c in summary["tool_calls"]}
    assert by_name["write_file"]["status"] == "completed"
    assert by_name["execute_shell"]["status"] == "completed"
    assert summary["files_touched"] == ["src/app.py"]
    (test_call,) = summary["tests"]
    assert test_call["tool_name"] == "execute_shell"

    # The approval ask is one record, sourced from both the audit
    # trail and the executor tap.
    (approval,) = summary["approvals"]
    assert approval["tool_name"] == "write_file"
    assert "audit_log" in approval["sources"]
    assert "executor_event" in approval["sources"]

    # Models + cost, joined from the usage ledger by task_id + agent.
    assert summary["costs"]["total_cost_usd"] == pytest.approx(0.006)
    models = {(m["agent"], m["model"]): m for m in summary["models"]}
    assert models[("coder", "gemini-3.5-flash")]["calls"] == 2
    assert models[("coder", "gemini-3.5-flash")]["cost_usd"] == pytest.approx(0.006)
    assert models[("researcher", "local-model")]["calls"] == 1

    # Outcome, from the episode record; transitions, from the audit log.
    assert summary["outcome"]["summary"] == "Built the thing successfully"
    assert summary["outcome"]["response_excerpt"] == "The thing was built and verified."
    assert [t["to_state"] for t in summary["transitions"]] == ["RUNNING", "SUCCEEDED"]
    assert summary["errors"] == []
    assert summary["coverage"]["episode"] is True
    assert summary["coverage"]["usage_calls"] == 3
    assert summary["coverage"]["live_tap"] is True
    assert summary["coverage"]["checkpoint"] is False  # named, not hidden

    briefing = task_ledger.task_briefing(summary)
    assert "coder" in briefing and "write_file" in briefing
    assert "$0.0060" in briefing
    assert len(briefing) <= task_ledger.BRIEFING_MAX_CHARS


@pytest.mark.asyncio
async def test_unknown_task_is_an_honest_not_found(tmp_path):
    summary = await task_ledger.task_summary(
        "task-that-never-existed", backend=_EmptyBackend(),
        ledger=UsageLedger(tmp_path / "usage.db"),
        audit_path=tmp_path / "no-audit.log",
        checkpoint_db=tmp_path / "no-checkpoints.db")

    assert summary["found"] is False
    assert summary["state"] is None
    assert summary["delegations"] == [] and summary["tool_calls"] == []
    assert summary["models"] == [] and summary["approvals"] == []
    assert summary["errors"] == [] and summary["files_touched"] == []
    assert summary["coverage"]["usage_calls"] == 0
    assert "no record found" in task_ledger.task_briefing(summary)


class _EmptyBackend:
    def list_episodes(self, limit=50):
        return []


# ── Checkpoint leg: state slice reconstruction ───────────────────────

def test_state_slice_reconstructs_delegations_tools_files_errors():
    from aether_engine.task_ledger import _state_slice

    state = {
        "task_id": TASK_ID, "session_id": "sess-s3",
        "original_prompt": "build the thing", "current_phase": "acting",
        "plan": "Plan: build it", "active_specialist": "coder",
        "delegation_log": [{
            "agent": "supervisor", "timestamp": 1_763_000_005,
            "decision": {"next": "coder", "reason": "needs code",
                         "briefing": "build it"}}],
        "messages": [
            {"role": "user", "content": "build the thing"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "src/app.py"})}}]},
            {"role": "tool", "name": "write_file",
             "args": {"path": "src/app.py"}, "content": "wrote 120 bytes"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c2", "type": "function", "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "src/nope.py"})}}]},
            {"role": "tool", "name": "read_file",
             "args": {"path": "src/nope.py"}, "content": "Error: not found"},
        ],
        "blackboard": {"artifacts": ["out/report.md"]},
        "artifact_paths": {"summary": "/tmp/summary.md"},
        "structured_outputs": {"coder": {"error": "schema validation failed"}},
        "run_config": {"provider_name": "google_gemini", "model": "gemini-3.5-flash"},
    }

    slice_ = _state_slice(state)

    (delegation,) = slice_["delegations"]
    assert delegation["agent"] == "coder" and delegation["denied"] is False
    by_name = {c["tool_name"]: c for c in slice_["tool_calls"]}
    assert by_name["write_file"]["status"] == "completed"
    assert by_name["read_file"]["status"] == "failed"
    assert slice_["files_touched"] == ["out/report.md", "/tmp/summary.md"]
    (error,) = slice_["errors"]
    assert error["agent"] == "coder" and "schema" in error["message"]
    assert slice_["blackboard_briefing"]


# ── Both executor paths feed the tap (real graph runs) ───────────────

TOOL_NAME = "s3_probe"
TOOL_ARGS = {"q": "x"}


class _ScriptedProvider:
    """Parity pattern: supervisor delegates to planner; planner's
    first call requests the probe tool, later calls answer."""

    def __init__(self):
        self._planner_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "planner":
            self._planner_calls += 1
            if self._planner_calls == 1:
                yield StreamChunk(
                    tool_calls=[ToolCall(name=TOOL_NAME, args=dict(TOOL_ARGS),
                                         call_id="call-s3-1")],
                    finish_reason="tool_calls")
            else:
                yield StreamChunk(text="Specialist final answer.",
                                  finish_reason="stop")
            return
        yield StreamChunk(
            text='{"next": "planner", "reason": "s3 routing", "briefing": ""}',
            finish_reason="stop")


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "s3/artifact.txt")
    registry = ToolRegistry()
    executed = []

    def _fn(**kwargs):
        executed.append(kwargs)
        return "probe-result-42"

    registry.register(Tool(
        name=TOOL_NAME, description="S3 probe tool",
        execute_fn=_fn, risk=ToolRisk.READ_ONLY))
    return tmp_path, registry, executed


@pytest.mark.asyncio
async def test_both_executor_paths_feed_the_ledger(graph_env):
    tmp_path, registry, executed = graph_env
    provider = _ScriptedProvider()
    session_id = "s3-pause-resume"
    task_id = f"task-{session_id}"
    user_config = UserConfig(tool_policies={TOOL_NAME: "Require Approval"})

    # RUN path: delegates, asks approval, pauses.
    events = [ev async for ev in executor_module.run_langgraph_task(
        "please run the s3 probe", provider, tools=registry,
        task_id=task_id, request_id=f"req-{session_id}",
        session_id=session_id, user_config=user_config)]
    assert EventType.TASK_INTERRUPTED in [ev.type for ev in events]
    assert executed == []

    tap = task_ledger.tap_snapshot(task_id)
    assert tap is not None and tap["state"] == "PENDING_APPROVAL"
    assert [d["agent"] for d in tap["delegations"]] == ["planner"]
    assert [a["tool_name"] for a in tap["approvals"]] == [TOOL_NAME]

    # RESUME path: the approved tool executes and the task completes —
    # its events arrive through the resume segment funnel tap.
    resume_events = [ev async for ev in executor_module.resume_langgraph_task(
        session_id, True, provider, tools=registry)]
    assert EventType.TASK_COMPLETED in [ev.type for ev in resume_events]
    assert executed == [TOOL_ARGS]

    tap = task_ledger.tap_snapshot(task_id)
    assert tap["state"] == "SUCCEEDED"
    tool_records = [c for c in tap["tool_calls"] if c["tool_name"] == TOOL_NAME]
    assert tool_records and tool_records[-1]["status"] == "completed"

    # And the checkpoint leg reconstructs the same task after the fact.
    summary = await task_ledger.task_summary(
        task_id, session_id=session_id, backend=_EmptyBackend(),
        ledger=UsageLedger(tmp_path / "usage.db"),
        audit_path=tmp_path / "no-audit.log",
        checkpoint_db=tmp_path / "checkpoints.db")
    assert summary["found"] is True
    assert summary["coverage"]["checkpoint"] is True
    # The checkpoint's delegation_log also holds the resume-time
    # re-decision and the finish decision; the read model reports the
    # actual delegations — every one of them to the planner here, the
    # first being the run path's.
    assert summary["delegations"]
    assert summary["delegations"][0]["agent"] == "planner"
    assert {d["agent"] for d in summary["delegations"]} == {"planner"}
    by_name = {c["tool_name"]: c for c in summary["tool_calls"]}
    assert by_name[TOOL_NAME]["status"] == "completed"
    assert summary["state"] == "SUCCEEDED"


# ── Recent listing + WS surface ──────────────────────────────────────

def test_recent_tasks_merges_episode_spine_and_tap(usage, tmp_path):
    backend = _store_real_episode()
    _tap_run_path_events("task-s3-live-only")
    tasks = task_ledger.recent_tasks(
        10, backend=backend, ledger=UsageLedger(tmp_path / "u2.db"))
    by_id = {t["task_id"]: t for t in tasks}
    assert by_id[TASK_ID]["summary"] == "Built the thing successfully"
    assert by_id[TASK_ID]["state"] == "SUCCEEDED"
    assert by_id["task-s3-live-only"]["state"] == "SUCCEEDED"
    assert "executor_event" in by_id["task-s3-live-only"]["sources"]
    assert tasks[0]["timestamp"] >= tasks[-1]["timestamp"]


class _FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


@pytest.mark.asyncio
async def test_ws_handlers_answer_query_and_list(monkeypatch):
    from aether_engine.routes import websocket, ws_connection

    async def fake_summary(task_id, **kwargs):
        return {"task_id": task_id, "found": True, "state": "SUCCEEDED"}

    monkeypatch.setattr(task_ledger, "task_summary", fake_summary)
    monkeypatch.setattr(
        task_ledger, "recent_tasks",
        lambda limit=20, **kw: [{"task_id": TASK_ID}])

    ws = _FakeSocket()
    conn = ws_connection.WsConnection(ws, object(), object())
    await websocket._DISPATCH[EventType.TASK_LEDGER_QUERY_REQUEST](
        conn, Event(type=EventType.TASK_LEDGER_QUERY_REQUEST,
                    request_id="req-q", payload={"task_id": TASK_ID}))
    await websocket._DISPATCH[EventType.TASK_LEDGER_LIST_REQUEST](
        conn, Event(type=EventType.TASK_LEDGER_LIST_REQUEST,
                    request_id="req-l", payload={"limit": 5}))

    assert ws.sent[0]["type"] == EventType.TASK_LEDGER_QUERY_RESPONSE.value
    assert ws.sent[0]["payload"]["found"] is True
    assert ws.sent[1]["type"] == EventType.TASK_LEDGER_LIST_RESPONSE.value
    assert ws.sent[1]["payload"]["count"] == 1

    # A query with no task_id answers honestly instead of raising.
    await websocket._DISPATCH[EventType.TASK_LEDGER_QUERY_REQUEST](
        conn, Event(type=EventType.TASK_LEDGER_QUERY_REQUEST,
                    request_id="req-bad", payload={}))
    assert ws.sent[2]["payload"]["found"] is False
    assert ws.sent[2]["payload"]["error"] == "task_id is required"


def test_tap_is_bounded():
    for i in range(task_ledger.EVENT_TAP_TASK_CAP + 5):
        task_ledger.record_executor_event(
            f"task-flood-{i}",
            Event(type=EventType.TASK_COMPLETED, request_id="r",
                  payload={"task_id": f"task-flood-{i}", "state": "SUCCEEDED"}))
    assert len(task_ledger.tap_task_ids()) == task_ledger.EVENT_TAP_TASK_CAP
    assert task_ledger.tap_snapshot("task-flood-0") is None
