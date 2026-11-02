"""N07 + N08 (Epic 8 Astra review): Media Library wiring.

N07 — the production analyzer was constructed with ``on_event=None``,
so analysis progress/results never reached any client and the Library
UI could not reflect analysis state; and Library search never used
the backend recall path, so transcript/semantic matches never
surfaced in the UI.

N08 — generated media was registered with ``message_id=None``, so the
Library's "jump to conversation" could not identify the exact message
that produced the media item.

Pins:
  * the production construction path (``get_media_analyzer``) fans
    analysis events out to subscribers;
  * the media-search contract events exist and the backend recall the
    websocket handler serves returns transcript-only matches;
  * a generated-media registration carries the producing (originating
    user) message id threaded through the executor config.
"""
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aether_common.contracts import EventType
from aether_engine.artifacts import store as artifacts_store
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.nodes import execute_tool as execute_tool_module
from aether_engine.media import analyzer as analyzer_mod
from aether_engine.media import store as media_store_mod
from aether_engine.media.store import MediaStore
from aether_engine.memory import episodic
from aether_engine.tools.media_recall import search_media_entries
from aether_engine.tools.registry import ToolRegistry, ToolRisk

# 1x1 transparent PNG.
PNG_BYTES = bytes(
    bytearray.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000d49444154789c62600100000500010d0a2db40000"
        "000049454e44ae426082"
    )
)


def _fake_embedding_for(text: str, dim: int = 8) -> list[float]:
    seed = zlib.crc32(text.encode("utf-8"))
    vals = []
    x = seed
    for _ in range(dim):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        vals.append((x % 1000) / 1000.0 - 0.5)
    return vals


async def _fake_aembedding(model, input, **kwargs):  # litellm kwarg name
    texts = input if isinstance(input, list) else [input]
    return {"data": [{"embedding": _fake_embedding_for(t)} for t in texts]}


@pytest.fixture(autouse=True)
def _reset_embed_cooldown():
    episodic._embed_unavailable_until = 0.0
    yield
    episodic._embed_unavailable_until = 0.0


# ---------------------------------------------------------------------------
# N07a — production analyzer delivers analysis events to subscribers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_production_analyzer_delivers_events_to_subscribers(tmp_path, monkeypatch):
    store = MediaStore(db_path=tmp_path / "media.db")
    store.init_db()
    monkeypatch.setattr(analyzer_mod, "get_media_store", lambda: store)
    monkeypatch.setattr(analyzer_mod, "_default_analyzer", None)

    received: list = []
    unsubscribe = analyzer_mod.subscribe_media_events(received.append)
    try:
        analyzer = analyzer_mod.get_media_analyzer()
        # The production construction path must wire an event callback —
        # on_event=None is the N07 defect.
        assert analyzer._on_event is not None

        vision = MagicMock()
        vision.analyze_image = AsyncMock(
            return_value={"summary": "A red bicycle by a wall.", "tags": ["bicycle"]}
        )
        analyzer._vision_provider = vision

        png = tmp_path / "shot.png"
        png.write_bytes(PNG_BYTES)
        item = store.register_media(
            file_path=str(png), collection="user", media_type="image",
            session_id="s1", message_id="m1",
        )
        with patch(
            "aether_engine.memory.episodic.litellm.aembedding",
            new=AsyncMock(side_effect=_fake_aembedding),
        ):
            await analyzer.analyze_item(item["id"])

        types = [getattr(e.type, "value", e.type) for e in received]
        assert EventType.MEDIA_ANALYSIS_PROGRESS.value in types
        analyzed = [
            e for e in received
            if getattr(e.type, "value", e.type) == EventType.MEDIA_ANALYZED.value
        ]
        assert analyzed, f"no MEDIA_ANALYZED event reached the subscriber: {types}"
        assert analyzed[-1].payload["id"] == item["id"]
        assert analyzed[-1].payload["status"] == "analyzed"
    finally:
        unsubscribe()
        monkeypatch.setattr(analyzer_mod, "_default_analyzer", None)


# ---------------------------------------------------------------------------
# N07b — media search contract + backend recall serves transcript matches
# ---------------------------------------------------------------------------

def test_media_search_contract_events_exist():
    assert EventType.MEDIA_SEARCH_REQUEST.value == "MEDIA_SEARCH_REQUEST"
    assert EventType.MEDIA_SEARCH_RESPONSE.value == "MEDIA_SEARCH_RESPONSE"


@pytest.mark.asyncio
async def test_backend_recall_returns_transcript_only_match(tmp_path):
    store = MediaStore(db_path=tmp_path / "media.db")
    store.init_db()
    clip = tmp_path / "call.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    item = store.register_media(
        file_path=str(clip), collection="user", media_type="video",
        session_id="s1", message_id="m1",
        summary_text="A screen recording of a dashboard.",
        tags=["dashboard"],
    )
    # The phrase lives ONLY in the transcript — no metadata field has it.
    store.update_analysis(
        item["id"],
        transcript_text="the quarterly earnings call numbers look strong",
        transcript_status="done",
    )
    with patch(
        "aether_engine.memory.episodic.litellm.aembedding",
        new=AsyncMock(side_effect=_fake_aembedding),
    ):
        out = await search_media_entries(
            "quarterly earnings", limit=5, collection="all", store=store,
        )
    ids = [r["id"] for r in out["results"]]
    assert item["id"] in ids


# ---------------------------------------------------------------------------
# N08 — generated media carries the producing message id
# ---------------------------------------------------------------------------

class _FakeRegistry:
    """Minimal registry double (test_epic1_prewalk pattern)."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def get_tool(self, name):
        return SimpleNamespace(risk=ToolRisk.WRITE)

    async def execute(self, name, args, **kwargs):
        self.calls.append((name, dict(args)))
        return "done"


@pytest.mark.asyncio
async def test_generated_media_records_producing_message_id(tmp_path, monkeypatch):
    media_store = MediaStore(db_path=tmp_path / "media.db")
    media_store.init_db()
    artifact_store = artifacts_store.ArtifactStore(db_path=tmp_path / "artifacts.db")
    monkeypatch.setattr(media_store_mod, "get_media_store", lambda: media_store)
    monkeypatch.setattr(artifacts_store, "get_artifact_store", lambda: artifact_store)
    monkeypatch.setattr(analyzer_mod, "schedule_media_analysis", lambda mid: None)

    png = tmp_path / "diagram.png"
    png.write_bytes(PNG_BYTES)
    registry = _FakeRegistry()
    state = {
        "pending_tool_call": {
            "name": "write_file",
            "args": {"path": str(png)},
            "call_id": "call-1",
        },
        "active_specialist": "coder",
        "messages": [],
        "session_id": "sess-9",
        "task_id": "task-9",
    }
    config = {
        "configurable": {
            "tool_registry": registry,
            "session_id": "sess-9",
            # The originating user message id, threaded by the executor.
            "message_id": "user-msg-42",
        }
    }
    await execute_tool_module.execute_tool_node(state, config)

    items, total = media_store.list_media()
    assert total == 1
    assert items[0]["collection"] == "generated"
    assert items[0]["message_id"] == "user-msg-42"


@pytest.mark.asyncio
async def test_executor_threads_message_id_into_graph_config(tmp_path, monkeypatch):
    """run_langgraph_task(message_id=...) must land in the graph
    config's configurable dict — the channel execute_tool reads."""
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")

    captured: dict = {}

    class _Snapshot:
        def __init__(self):
            self.values: dict = {}
            self.config: dict = {}

    class _FakeGraph:
        async def aget_state(self, config):
            captured.setdefault("config", config)
            return _Snapshot()

        async def astream(self, *args, **kwargs):
            if False:  # pragma: no cover — makes this an async generator
                yield None

    monkeypatch.setattr(
        executor_module, "compile_graph", lambda checkpointer=None: _FakeGraph())

    provider = MagicMock()
    async for _event in executor_module.run_langgraph_task(
        "draw a diagram",
        provider,
        tools=ToolRegistry(),
        task_id="task-sess-9",
        request_id="req-sess-9",
        session_id="sess-9",
        message_id="user-msg-7",
    ):
        pass
    assert captured.get("config"), "executor never fetched graph state"
    configurable = captured["config"].get("configurable", {})
    assert configurable.get("message_id") == "user-msg-7"
