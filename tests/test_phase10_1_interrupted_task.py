"""Phase 10.1, A2: an interrupted task must emit a distinct interrupted outcome.

Regression test for the audit finding in critical/03 #1: on the approval-pause
(interrupt) path the executor fell through to TASK_COMPLETED / TASK_FAILED
(or, once ``elapsed_ms`` was pre-initialised, silently ended with no terminal
event at all). An interrupted task must yield TASK_INTERRUPTED with state
PENDING_APPROVAL -- never TASK_FAILED, never TASK_COMPLETED -- so the
approval-pause -> kill-engine -> restart -> resume flow can pick the thread
back up. The pause itself must stay: TOOL_APPROVAL_REQUEST is still emitted
first.
"""
import pytest
from types import SimpleNamespace

import aether_engine.langgraph.executor as executor_mod
from aether_common.contracts import Event, EventType, TaskState
from aether_engine.langgraph.executor import (
    resume_langgraph_task,
    run_langgraph_task,
)
from aether_engine.providers.litellm_provider import LiteLLMProvider


class _FakeInterrupt:
    """Stand-in for a LangGraph interrupt: only ``.value`` is read."""

    def __init__(self, value):
        self.value = value


def _approval_interrupt():
    return {
        "__interrupt__": [
            _FakeInterrupt(
                {
                    "type": "approval_request",
                    "tool_name": "write_file",
                    "args": {"path": "/tmp/x.txt"},
                    "task_class": "standard",
                }
            )
        ]
    }


class _InterruptingGraph:
    """Fake compiled graph whose astream pauses for approval, then ends."""

    async def astream(self, *args, **kwargs):
        yield _approval_interrupt()

    async def aget_state(self, config):
        return SimpleNamespace(
            values={
                "task_id": "task-interrupted",
                "original_prompt": "write a hello world script",
                "session_id": "test-session",
            }
        )


class _CompletingGraph:
    """Fake compiled graph that runs to completion without interrupting."""

    async def astream(self, *args, **kwargs):
        # No updates: the stream simply ends.
        if False:
            yield

    async def aget_state(self, config):
        return SimpleNamespace(values={})


def _provider():
    return LiteLLMProvider(api_key="test", model="mock", provider_name="mock")


@pytest.fixture(autouse=True)
def _isolate_checkpoints(tmp_path, monkeypatch):
    """Keep the executor off the real ~/.aether/checkpoints.db."""
    monkeypatch.setattr(
        executor_mod, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db"
    )


async def _collect(gen):
    return [ev async for ev in gen]


async def test_run_interrupted_task_emits_task_interrupted(monkeypatch):
    """Interrupt on the run path -> TASK_INTERRUPTED, never FAILED/COMPLETED."""
    monkeypatch.setattr(
        executor_mod, "compile_graph", lambda checkpointer=None: _InterruptingGraph()
    )

    events = await _collect(
        run_langgraph_task(
            "write a hello world script",
            _provider(),
            task_id="task-interrupted",
            request_id="req-1",
            session_id="test-session",
        )
    )
    types = [ev.type for ev in events]

    # The approval pause itself must still happen.
    assert EventType.TOOL_APPROVAL_REQUEST in types
    # The distinct interrupted outcome.
    assert types.count(EventType.TASK_INTERRUPTED) == 1
    # And it must never be reported as a failure or a success.
    assert EventType.TASK_FAILED not in types
    assert EventType.TASK_COMPLETED not in types

    interrupted = next(ev for ev in events if ev.type == EventType.TASK_INTERRUPTED)
    assert interrupted.payload["task_id"] == "task-interrupted"
    assert interrupted.payload["state"] == TaskState.PENDING_APPROVAL.value
    assert interrupted.payload["approval_key"] == "test-session"
    assert interrupted.payload["latency_ms"] >= 0
    # The event must survive the wire envelope (new enum member in contracts).
    assert Event.from_json(interrupted.to_json()).type == EventType.TASK_INTERRUPTED


async def test_resume_interrupted_task_emits_task_interrupted(monkeypatch):
    """Interrupt again on the resume path -> TASK_INTERRUPTED, not FAILED."""
    monkeypatch.setattr(
        executor_mod, "compile_graph", lambda checkpointer=None: _InterruptingGraph()
    )

    events = await _collect(
        resume_langgraph_task(
            thread_id="test-session",
            approved=True,
            provider=_provider(),
        )
    )
    types = [ev.type for ev in events]

    assert EventType.TOOL_APPROVAL_REQUEST in types
    assert types.count(EventType.TASK_INTERRUPTED) == 1
    assert EventType.TASK_FAILED not in types
    assert EventType.TASK_COMPLETED not in types

    interrupted = next(ev for ev in events if ev.type == EventType.TASK_INTERRUPTED)
    assert interrupted.payload["task_id"] == "task-interrupted"
    assert interrupted.payload["state"] == TaskState.PENDING_APPROVAL.value
    assert interrupted.payload["approval_key"] == "test-session"


async def test_run_completed_task_still_emits_task_completed(monkeypatch):
    """Normal completion is untouched: TASK_COMPLETED with a bound latency."""
    monkeypatch.setattr(
        executor_mod, "compile_graph", lambda checkpointer=None: _CompletingGraph()
    )

    events = await _collect(
        run_langgraph_task(
            "write a hello world script",
            _provider(),
            task_id="task-completed",
            request_id="req-2",
            session_id="test-session",
        )
    )
    types = [ev.type for ev in events]

    assert types.count(EventType.TASK_COMPLETED) == 1
    assert EventType.TASK_FAILED not in types
    assert EventType.TASK_INTERRUPTED not in types
    completed = next(ev for ev in events if ev.type == EventType.TASK_COMPLETED)
    assert completed.payload["latency_ms"] >= 0
