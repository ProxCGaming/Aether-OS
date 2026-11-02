"""M16 (Epic 8 Astra review): graph tasks discard image attachments at
the executor boundary.

The websocket forwards ``attachments`` to ``run_langgraph_task`` (and
to ``run_fast_chat``), but the graph executor swallowed them in unused
kwargs and built a TEXT-ONLY user message, so a screenshot attached to
a graph task reached the assigned specialist as bare text. Grill Q20
requires the same attachment flow for fast chat and graph tasks: the
specialist's own model receives the image in the established
multimodal content-block shape fast chat already uses
(``[{"type": "text", ...}, {"type": "image_url", "image_url": {"url":
"data:..."}}]`` — the shape the provider layer converts for calls).

Pinned here:
  * an image attachment (file path AND inline data URL) reaches the
    specialist's model call as an image_url part alongside the prompt
    text part;
  * the attachment survives the executor's fresh-thread history
    seeding (the seeded-history branch replaces the message list) and
    a checkpoint round-trip (fresh checkpointer on the same DB);
  * a text-only task is byte-identical to before (plain string
    content, no parts list);
  * an attachment whose bytes are missing or corrupt degrades to an
    honest note inside the message — no crash, no silent drop, no
    bogus image part.

All providers are scripted stubs (house pattern): no network.
"""
import base64
import copy
from unittest.mock import AsyncMock

import pytest

from aether_common.contracts import EventType
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.nodes import researcher as researcher_module
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.memory import session_store
from aether_engine.providers.base import StreamChunk
from aether_engine.tools.registry import ToolRegistry

# 1x1 transparent PNG (same fixture bytes as test_vision_attachment_flow).
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwAD"
    "hgGAWjR9awAAAABJRU5ErkJggg=="
)
PNG_BYTES = base64.b64decode(PNG_B64)
PROMPT = "What does this screenshot show?"


# ---------------------------------------------------------------------------
# Stub provider: routes the supervisor to the researcher, records every
# call (node + messages) so tests can inspect what the specialist's own
# model was actually given.
# ---------------------------------------------------------------------------

class ImageRoutingProvider:
    def __init__(self):
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({
            "node": node_name,
            "messages": copy.deepcopy(messages),
        })
        if node_name == "researcher":
            yield StreamChunk(text="I can see the screenshot.", finish_reason="stop")
            return
        yield StreamChunk(
            text='{"next": "researcher", "reason": "needs eyes", "briefing": ""}',
            finish_reason="stop",
        )


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    """Isolate a full executor run (parity/f09 pattern): tmp checkpoint
    DB, no real memory writes on completion, no artifact files."""
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(
        researcher_module, "save_artifact", lambda *a, **k: "research/out.txt")
    return tmp_path


async def _collect(agen):
    return [event async for event in agen]


async def _run(provider, session_id, attachments=None, prompt=PROMPT):
    kwargs = {}
    if attachments is not None:
        kwargs["attachments"] = attachments
    return await _collect(executor_module.run_langgraph_task(
        prompt,
        provider,
        tools=ToolRegistry(),
        task_id=f"task-{session_id}",
        request_id=f"req-{session_id}",
        session_id=session_id,
        **kwargs,
    ))


def _specialist_user_messages(provider, node="researcher"):
    calls = [c for c in provider.calls if c["node"] == node]
    assert calls, f"specialist {node!r} was never called"
    return [m for m in calls[0]["messages"] if m.get("role") == "user"]


def _image_parts(message):
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [p for p in content
            if isinstance(p, dict) and p.get("type") == "image_url"]


def _text_parts(message):
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"]


# ---------------------------------------------------------------------------
# Delivery: the specialist's model call carries the image
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_graph_task_delivers_file_image_to_specialist(graph_env, tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG_BYTES)
    provider = ImageRoutingProvider()

    events = await _run(provider, "m16-file", attachments=[
        {"name": "shot.png", "mime_type": "image/png", "path": str(shot)},
    ])

    assert EventType.TASK_COMPLETED in [ev.type for ev in events]
    user_msgs = _specialist_user_messages(provider)
    images = _image_parts(user_msgs[-1])
    assert len(images) == 1, f"expected one image part, got {user_msgs[-1]!r}"
    assert images[0]["image_url"]["url"] == f"data:image/png;base64,{PNG_B64}"
    assert PROMPT in _text_parts(user_msgs[-1])


@pytest.mark.asyncio
async def test_graph_task_delivers_data_url_image_to_specialist(graph_env):
    provider = ImageRoutingProvider()
    data_url = f"data:image/png;base64,{PNG_B64}"

    events = await _run(provider, "m16-data", attachments=[
        {"name": "snip.png", "mime_type": "image/png", "data": data_url},
    ])

    assert EventType.TASK_COMPLETED in [ev.type for ev in events]
    user_msgs = _specialist_user_messages(provider)
    images = _image_parts(user_msgs[-1])
    assert len(images) == 1, f"expected one image part, got {user_msgs[-1]!r}"
    assert images[0]["image_url"]["url"] == data_url
    assert PROMPT in _text_parts(user_msgs[-1])


# ---------------------------------------------------------------------------
# Survival: checkpoint round-trip and fresh-thread history seeding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_graph_task_attachment_survives_checkpoint_round_trip(graph_env, tmp_path):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from aether_engine.langgraph.graph import compile_graph

    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG_BYTES)
    provider = ImageRoutingProvider()
    sid = "m16-checkpoint"

    events = await _run(provider, sid, attachments=[
        {"name": "shot.png", "mime_type": "image/png", "path": str(shot)},
    ])
    assert EventType.TASK_COMPLETED in [ev.type for ev in events]

    db_path = graph_env / "checkpoints.db"
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        graph = compile_graph(checkpointer=saver)
        state = await graph.aget_state({"configurable": {"thread_id": sid}})

    messages = state.values.get("messages", [])
    user_msgs = [m for m in messages
                 if isinstance(m, dict) and m.get("role") == "user"]
    assert user_msgs, f"no user message persisted, got {messages!r}"
    images = _image_parts(user_msgs[0])
    assert len(images) == 1, (
        f"image part did not survive the checkpoint round-trip: {user_msgs[0]!r}")
    assert images[0]["image_url"]["url"] == f"data:image/png;base64,{PNG_B64}"


@pytest.mark.asyncio
async def test_graph_task_attachment_survives_history_seeding(
        graph_env, tmp_path, monkeypatch):
    """Fresh checkpoint thread + existing session history: the executor
    seeds the run from the session store and appends the current
    message — the current turn's image must still arrive as a part."""
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", None)
    session_store.init_db()
    sid = session_store.create_session("M16 seeded")
    session_store.add_message(sid, "user", "Earlier question about the diagram")
    session_store.add_message(sid, "assistant", "Earlier answer.")

    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG_BYTES)
    provider = ImageRoutingProvider()

    events = await _run(provider, sid, attachments=[
        {"name": "shot.png", "mime_type": "image/png", "path": str(shot)},
    ])

    assert EventType.TASK_COMPLETED in [ev.type for ev in events]
    user_msgs = _specialist_user_messages(provider)
    assert len(user_msgs) >= 2, f"seeded history missing: {user_msgs!r}"
    assert user_msgs[0]["content"] == "Earlier question about the diagram"
    images = _image_parts(user_msgs[-1])
    assert len(images) == 1, (
        f"current-turn image lost on the seeding path: {user_msgs[-1]!r}")
    assert images[0]["image_url"]["url"] == f"data:image/png;base64,{PNG_B64}"


@pytest.mark.asyncio
async def test_graph_task_attachment_survives_followup_turn(graph_env, tmp_path):
    """Second task on the same thread: the first turn's image is part
    of the checkpointed history the specialist is re-shown — it must
    still be a structured image part, not a stringified repr."""
    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG_BYTES)
    sid = "m16-turn2"

    first = ImageRoutingProvider()
    events = await _run(first, sid, attachments=[
        {"name": "shot.png", "mime_type": "image/png", "path": str(shot)},
    ])
    assert EventType.TASK_COMPLETED in [ev.type for ev in events]

    second = ImageRoutingProvider()
    events = await _run(second, sid, prompt="And what about now?")
    assert EventType.TASK_COMPLETED in [ev.type for ev in events]

    user_msgs = _specialist_user_messages(second)
    assert len(user_msgs) >= 2, f"history missing on follow-up: {user_msgs!r}"
    images = _image_parts(user_msgs[0])
    assert len(images) == 1, (
        f"first-turn image mangled on the follow-up turn: {user_msgs[0]!r}")
    assert images[0]["image_url"]["url"] == f"data:image/png;base64,{PNG_B64}"
    assert user_msgs[-1]["content"] == "And what about now?"


# ---------------------------------------------------------------------------
# Negative space: text-only unchanged; broken attachments degrade honestly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_graph_task_text_only_content_unchanged(graph_env):
    provider = ImageRoutingProvider()

    events = await _run(provider, "m16-text")

    assert EventType.TASK_COMPLETED in [ev.type for ev in events]
    user_msgs = _specialist_user_messages(provider)
    assert user_msgs[-1]["content"] == PROMPT


@pytest.mark.asyncio
async def test_graph_task_missing_attachment_bytes_degrade_to_note(
        graph_env, tmp_path):
    provider = ImageRoutingProvider()

    events = await _run(provider, "m16-missing", attachments=[
        {"name": "gone.png", "mime_type": "image/png",
         "path": str(tmp_path / "does-not-exist.png")},
    ])

    assert EventType.TASK_COMPLETED in [ev.type for ev in events]
    user_msgs = _specialist_user_messages(provider)
    last = user_msgs[-1]
    assert _image_parts(last) == [], f"bogus image part emitted: {last!r}"
    notes = " ".join(_text_parts(last))
    assert "gone.png" in notes and "could not be loaded" in notes, (
        f"missing attachment must degrade to an honest note, got {last!r}")


@pytest.mark.asyncio
async def test_graph_task_corrupt_data_url_degrades_to_note(graph_env):
    provider = ImageRoutingProvider()

    events = await _run(provider, "m16-corrupt", attachments=[
        {"name": "bad.png", "mime_type": "image/png",
         "data": "data:image/png;base64,!!!not-base64!!!"},
    ])

    assert EventType.TASK_COMPLETED in [ev.type for ev in events]
    user_msgs = _specialist_user_messages(provider)
    last = user_msgs[-1]
    assert _image_parts(last) == [], f"corrupt image part emitted: {last!r}"
    notes = " ".join(_text_parts(last))
    assert "bad.png" in notes and "could not be loaded" in notes, (
        f"corrupt attachment must degrade to an honest note, got {last!r}")
