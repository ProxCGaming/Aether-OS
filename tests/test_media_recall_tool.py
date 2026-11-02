"""Tests for E8-4.3: The agent `media_recall` tool.

TDD test suite verifying:
1. Tool definition, name ('media_recall'), and risk (ToolRisk.READ_ONLY).
2. Registration in build_tool_registry() and read_only_names().
3. Agent calling media_recall receives matching media entries with session/message references.
4. Recall filtering by collection (all, user, generated).
5. Honest degradation when embedding endpoint is down (lexical recall).
6. Empty store / no matches returns honest message without error.
"""
from __future__ import annotations

import math
import re
from unittest.mock import AsyncMock, patch

import pytest

from aether_engine.media.store import MediaStore, set_global_media_store
from aether_engine.memory import episodic
from aether_engine.tools.media_recall import (
    create_media_recall_tool,
    search_media_entries,
)
from aether_engine.tools.registry import ToolRisk, build_tool_registry


def _deterministic_vec(text: str, dims: int = 64) -> list[float]:
    import zlib
    v = [0.0] * dims
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        v[zlib.crc32(tok.encode()) % dims] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


async def _fake_aembedding(model=None, input=None, **_kwargs):
    resp = type("Resp", (), {})()
    resp.data = [{"embedding": _deterministic_vec(t)} for t in (input or [])]
    return resp


@pytest.fixture(autouse=True)
def _reset_embedding_circuit_breaker():
    episodic._embed_unavailable_until = 0.0
    yield
    episodic._embed_unavailable_until = 0.0


@pytest.fixture
def populated_store(tmp_path):
    db_path = tmp_path / "test_recall_media.db"
    store = MediaStore(db_path=db_path)
    store.init_db()
    set_global_media_store(store)

    # Item 1: photo of red bicycle (user upload)
    m1 = store.register_media(
        file_path=str(tmp_path / "IMG_001.png"),
        collection="user",
        media_type="image",
        session_id="session-user-photo",
        message_id="msg-photo-001",
        status="analyzed",
        summary_text="A photograph of a bright red bicycle leaning against a stone wall.",
        tags=["bicycle", "red", "outdoor", "stone wall"],
        probe_metadata={"origin": "user_upload"},
    )
    store.store_embedding(m1["id"], _deterministic_vec("A photograph of a bright red bicycle leaning against a stone wall. Tags: bicycle, red, outdoor, stone wall"))

    # Item 2: generated diagram of database schema
    m2 = store.register_media(
        file_path=str(tmp_path / "schema_diagram.png"),
        collection="generated",
        media_type="image",
        session_id="session-agent-task",
        message_id="msg-agent-002",
        status="analyzed",
        summary_text="An entity relationship diagram of the database schema showing tables and foreign keys.",
        tags=["database", "schema", "architecture", "er-diagram"],
        probe_metadata={"origin": "agent_generated", "tool": "generate_diagram"},
    )
    store.store_embedding(m2["id"], _deterministic_vec("An entity relationship diagram of the database schema showing tables and foreign keys. Tags: database, schema, architecture, er-diagram"))

    # Item 3: video clip of user demo
    m3 = store.register_media(
        file_path=str(tmp_path / "app_demo.mp4"),
        collection="user",
        media_type="video",
        session_id="session-video-demo",
        message_id="msg-video-003",
        status="analyzed",
        summary_text="Screen capture video demonstrating the settings configuration panel and provider toggles.",
        tags=["demo", "settings", "video", "tutorial"],
        probe_metadata={"duration": 18.0, "codec": "h264"},
    )
    store.store_embedding(m3["id"], _deterministic_vec("Screen capture video demonstrating the settings configuration panel and provider toggles. Tags: demo, settings, video, tutorial"))

    return store


# ======================================================================
# 1. Tool Metadata and Registry Registration
# ======================================================================

def test_media_recall_tool_definition():
    tool = create_media_recall_tool()
    assert tool.name == "media_recall"
    assert tool.risk == ToolRisk.READ_ONLY
    assert "query" in tool.input_schema["properties"]
    assert "limit" in tool.input_schema["properties"]
    assert "collection" in tool.input_schema["properties"]


def test_media_recall_tool_registered_in_registry():
    registry = build_tool_registry()
    assert "media_recall" in registry.read_only_names()
    tool = registry.get_tool("media_recall")
    assert tool.risk == ToolRisk.READ_ONLY


# ======================================================================
# 2. Agent Recall with Session/Message References
# ======================================================================

@pytest.mark.asyncio
async def test_media_recall_finds_item_with_session_message_reference(populated_store):
    with patch("aether_engine.memory.episodic.litellm.aembedding", new=AsyncMock(side_effect=_fake_aembedding)):
        outcome = await search_media_entries(
            query="red bicycle",
            store=populated_store,
        )

    assert outcome["results"]
    top = outcome["results"][0]
    assert "IMG_001.png" in top["file_path"]
    assert top["session_id"] == "session-user-photo"
    assert top["message_id"] == "msg-photo-001"
    assert "red bicycle" in top["summary_text"].lower()


@pytest.mark.asyncio
async def test_agent_media_recall_database_schema_diagram(populated_store):
    registry = build_tool_registry()
    with patch("aether_engine.memory.episodic.litellm.aembedding", new=AsyncMock(side_effect=_fake_aembedding)):
        result = await registry.execute(
            "media_recall",
            {"query": "diagram of database schema"},
        )

    assert isinstance(result, str)
    assert "schema_diagram.png" in result
    assert "session-agent-task" in result
    assert "msg-agent-002" in result
    assert "database" in result.lower()


# ======================================================================
# 3. Collection Filtering
# ======================================================================

@pytest.mark.asyncio
async def test_media_recall_collection_filtering(populated_store):
    with patch("aether_engine.memory.episodic.litellm.aembedding", new=AsyncMock(side_effect=_fake_aembedding)):
        # Only generated
        gen_outcome = await search_media_entries(
            query="diagram",
            collection="generated",
            store=populated_store,
        )
        assert len(gen_outcome["results"]) == 1
        assert gen_outcome["results"][0]["collection"] == "generated"

        # Searching user collection for generated item yields nothing
        user_outcome = await search_media_entries(
            query="database schema",
            collection="user",
            store=populated_store,
        )
        assert len(user_outcome["results"]) == 0


# ======================================================================
# 4. Degradation and Empty State
# ======================================================================

@pytest.mark.asyncio
async def test_media_recall_degrades_to_lexical_when_endpoint_down(populated_store):
    # Simulate circuit breaker open
    import time
    episodic._embed_unavailable_until = time.monotonic() + 100.0

    outcome = await search_media_entries(
        query="red bicycle",
        store=populated_store,
    )
    assert outcome["degraded"] is True
    assert len(outcome["results"]) > 0
    assert "IMG_001.png" in outcome["results"][0]["file_path"]


@pytest.mark.asyncio
async def test_media_recall_no_matches_returns_honest_message(populated_store):
    with patch("aether_engine.memory.episodic.litellm.aembedding", new=AsyncMock(side_effect=_fake_aembedding)):
        outcome = await search_media_entries(
            query="flying saucer on mars alien spaceship",
            store=populated_store,
        )
        assert outcome["results"] == []

    tool = create_media_recall_tool()
    with patch("aether_engine.memory.episodic.litellm.aembedding", new=AsyncMock(side_effect=_fake_aembedding)):
        output_text = await tool.execute_fn(query="flying saucer on mars alien spaceship")

    assert "No matching media items found" in output_text


@pytest.mark.asyncio
async def test_media_recall_respects_limit(populated_store):
    with patch("aether_engine.memory.episodic.litellm.aembedding", new=AsyncMock(side_effect=_fake_aembedding)):
        outcome = await search_media_entries(
            query="settings video demo",
            limit=1,
            store=populated_store,
        )
    assert len(outcome["results"]) == 1
