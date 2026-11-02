"""PF-3 (Epic 8 programme follow-up, xref N08): steered-continuation
media attributes to the turn's originating message.

N08 threaded the originating user message's id into graph state
(``state["message_id"]``) so media a task generates is registered
against the message that prompted it, and the Library's
"jump to conversation" lands on that exact message. The steer path
was left out: a ``Steer`` carries its own ``message_id`` (the queue,
the executor's re-enqueue hand-off and the STEERING_APPLIED event all
preserve it), but when the executor applies a steer it starts the new
segment with only ``{"messages": ...}`` — graph state's ``message_id``
still names the ORIGINAL turn's message. Media generated during the
steered continuation is therefore registered against the original
message, so provenance and jump-to-conversation point at the wrong
place in the conversation.

Pinned here:
  * media generated after a mid-task steer (the steer itself carrying
    an image the model does see, per PF-1) is registered with the
    STEER message's id — not the original turn's;
  * the same holds for a steer applied on the approval-resume path;
  * an unsteered task's generated media keeps the originating
    message's id (unchanged behaviour).

All providers are scripted stubs (house pattern): no network.
"""
import asyncio
import base64
import copy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from aether_common.contracts import EventType
from aether_engine.artifacts import store as artifacts_store_mod
from aether_engine.artifacts.store import ArtifactStore
from aether_engine.config import UserConfig
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.steering import (
    STEERING_QUEUE,
    Steer,
    enqueue_steer,
    steer_state_update,
)
from aether_engine.media import analyzer as analyzer_mod
from aether_engine.media import store as media_store_mod
from aether_engine.media.store import MediaStore
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.tools.registry import Tool, ToolRisk, build_tool_registry

# 1x1 transparent PNG (same fixture bytes as the PF-1 suite).
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwAD"
    "hgGAWjR9awAAAABJRU5ErkJggg=="
)
PNG_BYTES = base64.b64decode(PNG_B64)

STEER_TEXT = "STEER-SENTINEL: render it like this screenshot instead"
PROBE_NAME = "pf3_probe"


def _materialised_shot(session_key: str) -> Path:
    """Write the fixture PNG where production puts steer attachments
    (the sanctioned attachments tree; HOME is conftest-sandboxed)."""
    shot_dir = Path.home() / ".aether" / "attachments" / session_key
    shot_dir.mkdir(parents=True, exist_ok=True)
    shot = shot_dir / "shot.png"
    shot.write_bytes(PNG_BYTES)
    return shot


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    """Isolate a full executor run: tmp checkpoint DB, tmp media +
    artifact stores, no real memory writes, in-process worker."""
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "test/artifact.txt")

    media_store = MediaStore(db_path=tmp_path / "media.db")
    media_store.init_db()
    monkeypatch.setattr(media_store_mod, "GLOBAL_MEDIA_STORE", media_store)
    art_store = ArtifactStore(db_path=tmp_path / "artifacts.db")
    monkeypatch.setattr(
        artifacts_store_mod, "GLOBAL_ARTIFACT_STORE", art_store)
    scheduled: list[str] = []
    monkeypatch.setattr(
        analyzer_mod, "schedule_media_analysis",
        lambda media_id: scheduled.append(media_id))

    # The subprocess sandbox needs Windows Job Objects (pywin32),
    # absent on this Linux VM; dispatching to run_worker in-process
    # exercises the identical worker code path, minus isolation.
    from aether_engine.workers import sandbox
    from aether_worker.__main__ import run_worker

    async def _in_process_worker(tool_name, args, task_class="standard",
                                 audit_logger=None, task_classes=None):
        return await run_worker({"tool_name": tool_name, "args": args})

    monkeypatch.setattr(sandbox, "execute_tool_in_worker", _in_process_worker)
    STEERING_QUEUE.clear()
    yield {
        "tmp_path": tmp_path,
        "media_store": media_store,
        "scheduled": scheduled,
    }
    STEERING_QUEUE.clear()


# ---------------------------------------------------------------------------
# Unit pins: the state update an applied steer batch produces
# ---------------------------------------------------------------------------

def test_steer_state_update_moves_provenance_to_last_steer_id():
    update = steer_state_update([
        Steer(text="first", message_id="m-first"),
        Steer(text="second", message_id="m-second"),
    ])
    assert update["message_id"] == "m-second"
    assert update["messages"] == [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]


def test_steer_state_update_without_ids_leaves_provenance_untouched():
    update = steer_state_update([Steer(text="anon", message_id=None)])
    assert "message_id" not in update
    assert update["messages"] == [{"role": "user", "content": "anon"}]


async def _collect(agen):
    return [event async for event in agen]


def _types(events):
    return [ev.type for ev in events]


def _generated_items(media_store):
    items, total = media_store.list_media(collection="generated")
    assert total == 1, f"expected exactly one generated item, got {items!r}"
    return items


def _routing_chunk():
    return StreamChunk(
        text='{"next": "planner", "reason": "test routing", "briefing": ""}',
        finish_reason="stop",
    )


def _write_chunk(target: Path, call_id: str):
    return StreamChunk(
        tool_calls=[ToolCall(
            name="write_file",
            args={"path": str(target), "content": "rendered by the task"},
            call_id=call_id,
        )],
        finish_reason="tool_calls",
    )


# ---------------------------------------------------------------------------
# (a) Mid-task steer: media generated in the steered continuation
# ---------------------------------------------------------------------------

class SteeredRenderProvider:
    """Planner's first call is gated so the test can steer mid-task
    (with an image attached). Its next call — the steered
    continuation — writes an image file; later calls answer text.
    Every call's messages are recorded (PF-1 pattern)."""

    def __init__(self, target: Path):
        self.target = target
        self.planner_calls = 0
        self.calls: list[dict] = []
        self.first_planner_started = asyncio.Event()
        self.release_first_planner = asyncio.Event()

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({
            "node": node_name,
            "messages": copy.deepcopy(messages),
        })
        if node_name == "planner":
            self.planner_calls += 1
            if self.planner_calls == 1:
                self.first_planner_started.set()
                await self.release_first_planner.wait()
                yield StreamChunk(
                    text="First version ready.", finish_reason="stop")
                return
            if self.planner_calls == 2:
                yield _write_chunk(self.target, "call-pf3-write")
                return
            yield StreamChunk(
                text="Rendered the steered image.", finish_reason="stop")
            return
        yield _routing_chunk()


@pytest.mark.asyncio
async def test_media_after_mid_task_steer_carries_steer_message_id(graph_env):
    tmp_path = graph_env["tmp_path"]
    session_id = "pf3-mid-task"
    shot = _materialised_shot(session_id)
    target = tmp_path / "steered.png"
    provider = SteeredRenderProvider(target)

    task = asyncio.create_task(_collect(executor_module.run_langgraph_task(
        "draw a diagram",
        provider,
        tools=build_tool_registry(),
        task_id="task-pf3-mid",
        request_id="req-pf3-mid",
        session_id=session_id,
        message_id="origin-msg-1",
        user_config=UserConfig(approval_mode="write"),
        workspace_roots=[str(tmp_path)],
    )))
    await asyncio.wait_for(provider.first_planner_started.wait(), timeout=10)
    enqueue_steer(session_id, STEER_TEXT, "steer-msg-1", [
        {"name": "shot.png", "mime_type": "image/png", "path": str(shot)},
    ])
    provider.release_first_planner.set()
    events = await asyncio.wait_for(task, timeout=60)

    assert EventType.TASK_COMPLETED in _types(events)
    assert EventType.TASK_FAILED not in _types(events)
    applied = [ev for ev in events if ev.type == EventType.STEERING_APPLIED]
    assert len(applied) == 1
    assert applied[0].payload["message_id"] == "steer-msg-1"

    # The steered continuation really did see the steer's image
    # (PF-1 delivery), so the render below is the steer's product.
    planner_calls = [c for c in provider.calls if c["node"] == "planner"]
    assert len(planner_calls) >= 2
    steer_seen = False
    for message in planner_calls[1]["messages"]:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        texts = [p.get("text", "") for p in content
                 if isinstance(p, dict) and p.get("type") == "text"]
        images = [p for p in content
                  if isinstance(p, dict) and p.get("type") == "image_url"]
        if STEER_TEXT in texts and images:
            steer_seen = True
            assert images[0]["image_url"]["url"] == \
                f"data:image/png;base64,{PNG_B64}"
    assert steer_seen, "post-steer planner call never saw the steer image"

    # THE PF-3 assertion: the generated media is registered against
    # the STEER message — the message that actually prompted it.
    items = _generated_items(graph_env["media_store"])
    item = items[0]
    assert item["file_path"] == str(target)
    assert item["session_id"] == session_id
    assert item["message_id"] == "steer-msg-1", (
        f"generated media attributed to {item['message_id']!r} — the "
        "original turn — instead of the steer message 'steer-msg-1'")
    assert graph_env["scheduled"] == [item["id"]]


# ---------------------------------------------------------------------------
# (b) Approval-resume steer: media generated after the resume-applied
# steer carries the steer message's id
# ---------------------------------------------------------------------------

class ResumeRenderProvider:
    """Planner first requests a read-only probe tool (forced to pause
    by an explicit Require Approval policy). The resumed segment may
    re-delegate to the planner several times before the executor
    applies the queued steer (steers ride a fresh segment after the
    resume settles), so the script is steer-aware rather than
    call-counted: it answers plain text until a call's messages
    contain the steer text — that call writes the image file — and
    answers final text afterwards."""

    def __init__(self, target: Path):
        self.target = target
        self.planner_calls = 0
        self.wrote = False

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "planner":
            self.planner_calls += 1
            if self.planner_calls == 1:
                yield StreamChunk(
                    tool_calls=[ToolCall(
                        name=PROBE_NAME, args={"q": "x"},
                        call_id="call-pf3-probe")],
                    finish_reason="tool_calls",
                )
                return
            if not self.wrote and STEER_TEXT in str(messages):
                self.wrote = True
                yield _write_chunk(self.target, "call-pf3-write-resume")
                return
            if self.wrote:
                yield StreamChunk(
                    text="Rendered the steered image.", finish_reason="stop")
                return
            yield StreamChunk(text="Probe done.", finish_reason="stop")
            return
        yield _routing_chunk()


@pytest.mark.asyncio
async def test_media_after_resume_steer_carries_steer_message_id(graph_env):
    tmp_path = graph_env["tmp_path"]
    session_id = "pf3-resume"
    target = tmp_path / "steered-resume.png"
    executed: list[dict] = []
    registry = build_tool_registry()

    def _probe(**kwargs):
        executed.append(kwargs)
        return "probe-result"

    registry.register(Tool(
        name=PROBE_NAME, description="PF-3 probe tool",
        execute_fn=_probe, risk=ToolRisk.READ_ONLY))
    provider = ResumeRenderProvider(target)
    user_config = UserConfig(
        approval_mode="write",
        tool_policies={PROBE_NAME: "Require Approval"})

    events = await _collect(executor_module.run_langgraph_task(
        "run the probe, then draw",
        provider,
        tools=registry,
        task_id="task-pf3-resume",
        request_id="req-pf3-resume",
        session_id=session_id,
        message_id="origin-msg-2",
        user_config=user_config,
        workspace_roots=[str(tmp_path)],
    ))
    assert EventType.TASK_INTERRUPTED in _types(events)
    assert executed == [], "probe must not execute before approval"

    enqueue_steer(session_id, STEER_TEXT, "steer-msg-2")

    resume_events = await _collect(executor_module.resume_langgraph_task(
        session_id, True, provider, tools=registry,
        workspace_roots=[str(tmp_path)]))

    assert EventType.TASK_COMPLETED in _types(resume_events)
    assert EventType.TASK_FAILED not in _types(resume_events)
    applied = [ev for ev in resume_events
               if ev.type == EventType.STEERING_APPLIED]
    assert len(applied) == 1
    assert applied[0].payload["message_id"] == "steer-msg-2"
    assert executed == [{"q": "x"}]

    items = _generated_items(graph_env["media_store"])
    item = items[0]
    assert item["file_path"] == str(target)
    assert item["session_id"] == session_id
    assert item["message_id"] == "steer-msg-2", (
        f"generated media attributed to {item['message_id']!r} — the "
        "original turn — instead of the steer message 'steer-msg-2'")


# ---------------------------------------------------------------------------
# (c) Control: unsteered generation keeps the originating message id
# ---------------------------------------------------------------------------

class UnsteeredRenderProvider:
    """Planner writes the image on its first call, then answers."""

    def __init__(self, target: Path):
        self.target = target
        self.planner_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "planner":
            self.planner_calls += 1
            if self.planner_calls == 1:
                yield _write_chunk(self.target, "call-pf3-write-control")
                return
            yield StreamChunk(text="Rendered.", finish_reason="stop")
            return
        yield _routing_chunk()


@pytest.mark.asyncio
async def test_unsteered_media_keeps_originating_message_id(graph_env):
    tmp_path = graph_env["tmp_path"]
    session_id = "pf3-control"
    target = tmp_path / "plain.png"
    provider = UnsteeredRenderProvider(target)

    events = await _collect(executor_module.run_langgraph_task(
        "draw a diagram",
        provider,
        tools=build_tool_registry(),
        task_id="task-pf3-control",
        request_id="req-pf3-control",
        session_id=session_id,
        message_id="origin-msg-3",
        user_config=UserConfig(approval_mode="write"),
        workspace_roots=[str(tmp_path)],
    ))
    assert EventType.TASK_COMPLETED in _types(events)

    items = _generated_items(graph_env["media_store"])
    assert items[0]["file_path"] == str(target)
    assert items[0]["message_id"] == "origin-msg-3"
