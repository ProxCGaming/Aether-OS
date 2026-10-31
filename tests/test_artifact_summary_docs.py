"""Tests for E8-2.4 (Canvas D): summary documents + standalone HTML preview.

Verifies:
- Auto-saving the task's final summary as an artifact document (kind = 'report').
- Direct text storage in ArtifactStore for summary documents.
- Exact final-summary text preservation.
- Grouping under its task ID.
- Persistence across session reloads and store re-initialization.
- Creation on both normal task completion and resumed task completion.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aether_common.contracts import Event, EventType
from aether_engine.artifacts.store import ArtifactStore
from aether_engine.config import UserConfig
from aether_engine.langgraph.executor import resume_langgraph_task, run_langgraph_task
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.tools.registry import build_tool_registry


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "test_artifacts.db"


@pytest.fixture
def store(temp_db: Path) -> ArtifactStore:
    return ArtifactStore(db_path=temp_db)


# ==============================================================================
# 1. ArtifactStore direct text storage & persistence tests
# ==============================================================================

def test_record_summary_artifact_direct_text_storage(store: ArtifactStore) -> None:
    """Verifies that record_summary_artifact stores kind='report' with direct text."""
    summary_text = "# Execution Summary\n\n- Step 1: Created config\n- Step 2: Tests passed"
    session_id = "session_test_1"
    task_id = "task_test_1"

    art = store.record_summary_artifact(
        session_id=session_id,
        task_id=task_id,
        summary=summary_text,
    )

    assert art["id"] is not None
    assert art["session_id"] == session_id
    assert art["task_id"] == task_id
    assert art["kind"] == "report"
    assert art["title"] == "Task Summary"
    assert art["file_path"] is None
    assert art["content"] == summary_text
    assert art["metadata"]["type"] == "summary_document"

    # Fetch it back by id
    fetched = store.get_artifact(art["id"])
    assert fetched is not None
    assert fetched["id"] == art["id"]
    assert fetched["kind"] == "report"
    assert fetched["content"] == summary_text
    assert fetched["file_path"] is None


def test_summary_document_persisted_across_reloads(temp_db: Path) -> None:
    """Verifies that a summary document persists across reload and re-initialization."""
    store1 = ArtifactStore(db_path=temp_db)
    summary_text = "Final deliverable summary text."
    art = store1.record_summary_artifact(
        session_id="session_persist",
        task_id="task_persist_1",
        summary=summary_text,
    )

    # Simulate fresh store instance (reopen / reload)
    store2 = ArtifactStore(db_path=temp_db)
    fetched = store2.get_artifact(art["id"])
    assert fetched is not None
    assert fetched["content"] == summary_text
    assert fetched["kind"] == "report"
    assert fetched["task_id"] == "task_persist_1"

    # Listing by session returns it grouped under task
    items, total = store2.list_artifacts(session_id="session_persist")
    assert total == 1
    assert items[0]["id"] == art["id"]
    assert items[0]["task_id"] == "task_persist_1"
    assert items[0]["content"] == summary_text


def test_record_summary_artifact_updates_existing_for_task(store: ArtifactStore) -> None:
    """Calling record_summary_artifact again for the same session + task updates the existing record."""
    art1 = store.record_summary_artifact(
        session_id="sess_update",
        task_id="task_update",
        summary="Initial partial summary",
    )
    art2 = store.record_summary_artifact(
        session_id="sess_update",
        task_id="task_update",
        summary="Updated final summary",
    )

    assert art1["id"] == art2["id"]
    items, total = store.list_artifacts(session_id="sess_update")
    assert total == 1
    assert items[0]["content"] == "Updated final summary"


# ==============================================================================
# 2. End-to-end task completion creates summary artifact
# ==============================================================================

class SimpleSummaryProvider:
    """Stub provider that returns a fixed final assistant response."""

    def __init__(self, response_text: str):
        self.response_text = response_text

    async def call_stream(self, messages: Any, tools: Any = None, node_name: Any = None, **kwargs: Any) -> Any:
        yield StreamChunk(text=self.response_text)
        yield StreamChunk(finish_reason="stop")


@pytest.mark.asyncio
async def test_task_completion_creates_summary_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that a completed graph task auto-records a summary document artifact."""
    db_path = tmp_path / "artifacts.db"
    test_store = ArtifactStore(db_path=db_path)
    monkeypatch.setattr("aether_engine.artifacts.store.GLOBAL_ARTIFACT_STORE", test_store)

    session_id = "session_e2e_summary"
    task_id = "task_e2e_summary"
    expected_summary = "All tasks completed successfully. Refined 3 components and fixed 1 bug."

    provider = SimpleSummaryProvider(expected_summary)
    events: list[Event] = []

    async for ev in run_langgraph_task(
        prompt="Summarize the work done",
        provider=provider,
        tools=build_tool_registry(),
        task_id=task_id,
        request_id=f"req-{task_id}",
        session_id=session_id,
        user_config=UserConfig(approval_mode="never"),
        workspace_roots=[str(tmp_path)],
    ):
        events.append(ev)

    # 1. TASK_COMPLETED event emitted
    completed_events = [e for e in events if e.type == EventType.TASK_COMPLETED]
    assert len(completed_events) == 1
    assert completed_events[0].payload["task_id"] == task_id
    assert completed_events[0].payload["response"] == expected_summary

    # 2. ARTIFACT_RECORDED event emitted for the summary document
    artifact_recorded_events = [e for e in events if e.type == EventType.ARTIFACT_RECORDED]
    assert len(artifact_recorded_events) >= 1
    summary_ev = next(e for e in artifact_recorded_events if e.payload.get("kind") == "report")
    assert summary_ev.payload["task_id"] == task_id
    assert summary_ev.payload["session_id"] == session_id
    assert summary_ev.payload["title"] == "Task Summary"

    # 3. Artifact is in the ArtifactStore with the exact final-summary text
    art = test_store.get_artifact(summary_ev.payload["artifact_id"])
    assert art is not None
    assert art["kind"] == "report"
    assert art["task_id"] == task_id
    assert art["session_id"] == session_id
    assert art["content"] == expected_summary
    assert art["file_path"] is None


class InterruptingThenCompletingProvider:
    """First call asks for tool approval (write_file); second call yields final summary."""

    def __init__(self, target_path: str, final_summary: str):
        self._calls = 0
        self._target_path = target_path
        self._final_summary = final_summary

    async def call_stream(self, messages: Any, tools: Any = None, node_name: Any = None, **kwargs: Any) -> Any:
        if node_name == "planner":
            self._calls += 1
            if self._calls == 1:
                yield StreamChunk(
                    tool_calls=[ToolCall(
                        name="write_file",
                        args={"path": self._target_path, "content": "test content"},
                        call_id="call_write_1",
                    )],
                )
            else:
                yield StreamChunk(text=self._final_summary)
                yield StreamChunk(finish_reason="stop")
        else:
            yield StreamChunk(text="OK")
            yield StreamChunk(finish_reason="stop")


@pytest.mark.asyncio
async def test_resumed_task_completion_creates_summary_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that a resumed graph task also auto-records a summary document artifact on completion."""
    db_path = tmp_path / "artifacts.db"
    test_store = ArtifactStore(db_path=db_path)
    monkeypatch.setattr("aether_engine.artifacts.store.GLOBAL_ARTIFACT_STORE", test_store)

    session_id = "session_resume_summary"
    task_id = "task_resume_summary"
    final_text = "Resumed task finished writing file and validating results."
    target_file = tmp_path / "output.txt"

    provider = InterruptingThenCompletingProvider(str(target_file), final_text)

    # 1. Run initial task which interrupts on approval_mode="always"
    events_initial: list[Event] = []
    async for ev in run_langgraph_task(
        prompt="Write a file",
        provider=provider,
        tools=build_tool_registry(),
        task_id=task_id,
        request_id=f"req-{task_id}",
        session_id=session_id,
        user_config=UserConfig(approval_mode="always"),
        workspace_roots=[str(tmp_path)],
    ):
        events_initial.append(ev)

    interrupted = [e for e in events_initial if e.type == EventType.TASK_INTERRUPTED]
    assert len(interrupted) == 1
    approval_key = interrupted[0].payload["approval_key"]

    # 2. Resume task with approved=True
    events_resumed: list[Event] = []
    async for ev in resume_langgraph_task(
        thread_id=approval_key,
        approved=True,
        provider=provider,
        tools=build_tool_registry(),
        workspace_roots=[str(tmp_path)],
    ):
        events_resumed.append(ev)

    completed = [e for e in events_resumed if e.type == EventType.TASK_COMPLETED]
    assert len(completed) == 1

    # Check summary artifact recorded for resumed task
    arts, total = test_store.list_artifacts(session_id=session_id, kind="report")
    assert total == 1
    assert arts[0]["task_id"] == task_id
    assert arts[0]["content"] == final_text
    assert arts[0]["kind"] == "report"
