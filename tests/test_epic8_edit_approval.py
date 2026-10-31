"""Epic 8, E8-1.3: edit-on-approval (arguments only).

Behaviour pinned (grill Q4 + PRD §E8-1.3, Refinement Note 3):

1. The approval resume value may be ``{"approved": True,
   "edited_args": {...}}`` (it rides the existing LangGraph
   ``interrupt()`` resume at ``nodes/pause.py``). The pause node swaps
   the pending call's args for the edited ones and marks the call
   ``edited`` — arguments only, never a tool swap.
2. Edited args re-run the FULL policy pipeline in
   ``ToolRegistry.execute`` (arg/path-aware policy → pre-flight →
   hooks): an edit that lands on a policy-denied path is blocked even
   though the user approved, and never executes.
3. A benign edit executes with the edited values; the persisted tool
   message carries the edited args and the ``edited`` marker that the
   TOOL_ACTIVITY payload forwards to the tool card's pill.
4. The websocket layer audits ``APPROVAL_ARGUMENTS_EDITED`` with the
   changed field NAMES only — values never reach the audit payload.
5. The unedited approval path (bare boolean resume) is unchanged.

The full-graph tests mirror tests/test_epic1_parity.py: scripted stub
providers, a tmp checkpoint DB, no network, no sandbox worker (the
probe tool is READ_ONLY, so it executes in-process).
"""
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from aether_common.contracts import EventType, TaskState
from aether_engine.config import UserConfig
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.graph import compile_graph
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.nodes.pause import (
    changed_arg_fields,
    dummy_pause_node,
)
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.routes.websocket import _audit_approval_arguments_edited
from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk

TOOL_NAME = "edit_probe"
TOOL_RESULT = "probe-result-42"
FINAL_TEXT = "Specialist final answer."

ORIGINAL_ARGS = {"path": "/workspace/ok/file.txt", "note": "original"}
EDITED_ARGS = {"path": "/workspace/ok/file.txt", "note": "user-edited"}


# ---------------------------------------------------------------------------
# changed_arg_fields — the audit/pill diff primitive (names only)
# ---------------------------------------------------------------------------

def test_changed_arg_fields_detects_value_change():
    assert changed_arg_fields({"a": 1, "b": 2}, {"a": 1, "b": 3}) == ["b"]


def test_changed_arg_fields_detects_added_and_removed_keys():
    assert changed_arg_fields({"a": 1}, {"a": 1, "b": 2}) == ["b"]
    assert changed_arg_fields({"a": 1, "b": 2}, {"a": 1}) == ["b"]


def test_changed_arg_fields_empty_for_identical_args():
    assert changed_arg_fields({"a": 1, "b": [1, 2]}, {"a": 1, "b": [1, 2]}) == []


def test_changed_arg_fields_is_sorted_and_names_only():
    changed = changed_arg_fields(
        {"z": "secret-z", "m": 1}, {"z": "secret-z2", "m": 2, "a": True})
    assert changed == ["a", "m", "z"]
    assert "secret-z2" not in changed


# ---------------------------------------------------------------------------
# Pause node — the resume value shapes
# ---------------------------------------------------------------------------

def _pause_state(args=None):
    return {
        "pending_tool_call": {
            "name": TOOL_NAME,
            "args": dict(args or ORIGINAL_ARGS),
            "call_id": "call-edit-1",
        }
    }


@patch("aether_engine.langgraph.nodes.pause.interrupt")
def test_pause_applies_edited_args_and_marks_edited(mock_interrupt):
    mock_interrupt.return_value = {"approved": True, "edited_args": dict(EDITED_ARGS)}
    result = dummy_pause_node(_pause_state(), {"configurable": {}})
    pending = result["pending_tool_call"]
    assert pending["approved"] is True
    assert pending["args"] == EDITED_ARGS
    assert pending["edited"] is True
    # Arguments only: the tool itself can never be swapped by an edit.
    assert pending["name"] == TOOL_NAME


@patch("aether_engine.langgraph.nodes.pause.interrupt")
def test_pause_identical_edits_do_not_mark_edited(mock_interrupt):
    mock_interrupt.return_value = {"approved": True, "edited_args": dict(ORIGINAL_ARGS)}
    result = dummy_pause_node(_pause_state(), {"configurable": {}})
    pending = result["pending_tool_call"]
    assert pending["approved"] is True
    assert pending["args"] == ORIGINAL_ARGS
    assert not pending.get("edited")


@patch("aether_engine.langgraph.nodes.pause.interrupt")
def test_pause_bool_approval_unchanged(mock_interrupt):
    mock_interrupt.return_value = True
    result = dummy_pause_node(_pause_state(), {"configurable": {}})
    pending = result["pending_tool_call"]
    assert pending["approved"] is True
    assert pending["args"] == ORIGINAL_ARGS
    assert not pending.get("edited")


@patch("aether_engine.langgraph.nodes.pause.interrupt")
def test_pause_dict_rejection_still_rejects(mock_interrupt):
    mock_interrupt.return_value = {"approved": False, "edited_args": dict(EDITED_ARGS)}
    result = dummy_pause_node(_pause_state(), {"configurable": {}})
    assert result["pending_tool_call"] is None
    msg = result["messages"][0]
    assert msg["role"] == "tool"
    assert "rejected" in msg["content"].lower()


# ---------------------------------------------------------------------------
# Audit helper — changed field names only, never values
# ---------------------------------------------------------------------------

class _RecordingAuditLogger:
    def __init__(self):
        self.events = []

    def log_event(self, name, payload):
        self.events.append((name, payload))


def test_audit_event_names_changed_fields_not_values():
    audit = _RecordingAuditLogger()
    edited = {"path": "/workspace/ok/file.txt", "note": "brand-new-secret-value"}
    changed = _audit_approval_arguments_edited(
        audit, "thread-1", TOOL_NAME, dict(ORIGINAL_ARGS), edited)
    assert changed == ["note"]
    assert len(audit.events) == 1
    name, payload = audit.events[0]
    assert name == "APPROVAL_ARGUMENTS_EDITED"
    assert payload["changed_fields"] == ["note"]
    assert payload["tool_name"] == TOOL_NAME
    # The edited VALUE must not appear anywhere in the audit payload.
    assert "brand-new-secret-value" not in str(payload)
    assert "args" not in payload


def test_audit_event_skipped_when_nothing_changed():
    audit = _RecordingAuditLogger()
    changed = _audit_approval_arguments_edited(
        audit, "thread-1", TOOL_NAME, dict(ORIGINAL_ARGS), dict(ORIGINAL_ARGS))
    assert changed == []
    assert audit.events == []


# ---------------------------------------------------------------------------
# Full graph: run → approval pause → resume with edited args
# ---------------------------------------------------------------------------

class ScriptedGraphProvider:
    """Supervisor routes to planner; planner's first call requests the
    probe tool, later calls return final text (parity-suite pattern)."""

    def __init__(self, tool_args):
        self.calls = []
        self._planner_calls = 0
        self._tool_args = tool_args

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({"node": node_name,
                           "messages": [dict(m) for m in messages]})
        if node_name == "planner":
            self._planner_calls += 1
            if self._planner_calls == 1:
                yield StreamChunk(
                    tool_calls=[ToolCall(name=TOOL_NAME,
                                         args=dict(self._tool_args),
                                         call_id="call-edit-1")],
                    finish_reason="tool_calls",
                )
            else:
                yield StreamChunk(text=FINAL_TEXT, finish_reason="stop")
            return
        yield StreamChunk(
            text='{"next": "planner", "reason": "edit probe routing", "briefing": ""}',
            finish_reason="stop",
        )


def _probe_registry(executed):
    registry = ToolRegistry()

    def _fn(**kwargs):
        executed.append(kwargs)
        return TOOL_RESULT

    registry.register(Tool(
        name=TOOL_NAME,
        description="Epic 8 edit-on-approval probe tool",
        execute_fn=_fn,
        risk=ToolRisk.READ_ONLY,
    ))
    return registry


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "edit-probe/artifact.txt")
    return tmp_path


async def _collect(agen):
    return [event async for event in agen]


async def _run_until_pause(provider, registry, user_config, session_id, tool_args):
    events = await _collect(executor_module.run_langgraph_task(
        "please run the edit probe",
        provider,
        tools=registry,
        task_id=f"task-{session_id}",
        request_id=f"req-{session_id}",
        session_id=session_id,
        user_config=user_config,
    ))
    types = [ev.type for ev in events]
    assert EventType.TASK_INTERRUPTED in types
    approval = next(ev for ev in events
                    if ev.type == EventType.TOOL_APPROVAL_REQUEST)
    assert approval.payload["args"] == tool_args
    return events


async def _final_tool_message(session_id, db_path):
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        graph = compile_graph(checkpointer=saver)
        state = await graph.aget_state(
            {"configurable": {"thread_id": session_id}})
    tool_msgs = [m for m in state.values.get("messages", [])
                 if m.get("role") == "tool" and m.get("name") == TOOL_NAME]
    assert tool_msgs, "expected a persisted tool message"
    return tool_msgs[-1]


@pytest.mark.asyncio
async def test_benign_edit_executes_with_edited_values(graph_env):
    executed = []
    registry = _probe_registry(executed)
    provider = ScriptedGraphProvider(ORIGINAL_ARGS)
    user_config = UserConfig(tool_policies={TOOL_NAME: "Require Approval"})
    session_id = "e8-edit-benign"

    await _run_until_pause(provider, registry, user_config, session_id,
                           ORIGINAL_ARGS)
    assert executed == [], "tool must not execute before approval"

    resume_events = await _collect(executor_module.resume_langgraph_task(
        session_id, True, provider, tools=registry,
        user_config=user_config, edited_args=dict(EDITED_ARGS)))

    types = [ev.type for ev in resume_events]
    assert EventType.TASK_COMPLETED in types
    completed = next(ev for ev in resume_events
                     if ev.type == EventType.TASK_COMPLETED)
    assert completed.payload["state"] == TaskState.SUCCEEDED.value
    assert executed == [EDITED_ARGS], "the EDITED args must be what executes"

    tool_msg = await _final_tool_message(session_id,
                                         graph_env / "checkpoints.db")
    assert TOOL_RESULT in tool_msg["content"]
    assert tool_msg["args"] == EDITED_ARGS
    assert tool_msg.get("edited") is True

    activity = [ev for ev in resume_events
                if ev.type == EventType.TOOL_ACTIVITY
                and ev.payload.get("tool_name") == TOOL_NAME
                and ev.payload.get("status") == "completed"]
    assert activity, "expected a completed TOOL_ACTIVITY for the edited call"
    assert activity[-1].payload["edited"] is True
    assert activity[-1].payload["tool_args"] == EDITED_ARGS


@pytest.mark.asyncio
async def test_policy_breaking_edit_is_blocked(graph_env, tmp_path):
    """An edit that moves the target path under a Deny path rule is
    blocked by the re-run policy pipeline — approval does not help."""
    executed = []
    registry = _probe_registry(executed)
    original = {"path": str(tmp_path / "ok" / "file.txt"), "note": "original"}
    breaking = {"path": str(tmp_path / "vault" / "file.txt"), "note": "original"}
    provider = ScriptedGraphProvider(original)
    user_config = UserConfig(
        tool_policies={TOOL_NAME: "Require Approval"},
        path_rules=[{"prefix": str(tmp_path / "vault"), "verdict": "Deny"}],
    )
    session_id = "e8-edit-blocked"

    await _run_until_pause(provider, registry, user_config, session_id,
                           original)

    resume_events = await _collect(executor_module.resume_langgraph_task(
        session_id, True, provider, tools=registry,
        user_config=user_config, edited_args=breaking))

    types = [ev.type for ev in resume_events]
    assert EventType.TASK_COMPLETED in types, "task settles after the block"
    assert executed == [], "a policy-denied edit must never execute"

    tool_msg = await _final_tool_message(session_id,
                                         graph_env / "checkpoints.db")
    assert "blocked by user policy" in tool_msg["content"]


@pytest.mark.asyncio
async def test_unedited_resume_path_unchanged(graph_env):
    executed = []
    registry = _probe_registry(executed)
    provider = ScriptedGraphProvider(ORIGINAL_ARGS)
    user_config = UserConfig(tool_policies={TOOL_NAME: "Require Approval"})
    session_id = "e8-edit-unedited"

    await _run_until_pause(provider, registry, user_config, session_id,
                           ORIGINAL_ARGS)

    resume_events = await _collect(executor_module.resume_langgraph_task(
        session_id, True, provider, tools=registry, user_config=user_config))

    assert EventType.TASK_COMPLETED in [ev.type for ev in resume_events]
    assert executed == [ORIGINAL_ARGS]

    tool_msg = await _final_tool_message(session_id,
                                         graph_env / "checkpoints.db")
    assert TOOL_RESULT in tool_msg["content"]
    assert tool_msg["args"] == ORIGINAL_ARGS
    assert not tool_msg.get("edited")
