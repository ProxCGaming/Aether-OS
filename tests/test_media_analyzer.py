"""Tests for E8-4.3: Media analysis pipeline (analyzer, probe, keyframes, embedding, backfill).

TDD test suite verifying:
1. Video probe extracts duration, codec, resolution, and format.
2. Adaptive keyframe sampling enforces the hard cap of 24 frames.
3. Photo analysis flow with stubbed vision provider (summary + tags).
4. Metadata embedding generation and semantic search hit in MediaStore.
5. Analysis failure leaves the entry usable and honestly marked ('failed').
6. Backfill of pre-existing media from 'pending' to 'analyzed'.
7. Degraded embedding (circuit breaker open) leaves analysis completed and entry usable.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
import zlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aether_common.contracts import EventType
from aether_engine.media.analyzer import (
    MediaAnalyzer,
    adaptive_keyframe_timestamps,
    probe_video,
)
from aether_engine.media.store import MediaStore
from aether_engine.memory import episodic


def _deterministic_vec(text: str, dims: int = 64) -> list[float]:
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
def temp_store(tmp_path):
    db_path = tmp_path / "test_media.db"
    store = MediaStore(db_path=db_path)
    store.init_db()
    return store


@pytest.fixture
def sample_image(tmp_path):
    img = tmp_path / "IMG_001.png"
    # Minimal 1x1 PNG
    img.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00"
        b"\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return img


@pytest.fixture
def mock_vision_provider():
    provider = MagicMock()
    # Mock vision response returning description and tags
    async def _mock_call(messages, **kwargs):
        return {
            "summary": "A photo of a red bicycle parked by a brick wall.",
            "tags": ["bicycle", "red", "outdoor", "transportation"],
        }
    provider.analyze_image = AsyncMock(side_effect=_mock_call)
    provider.analyze_video_frames = AsyncMock(
        return_value={
            "summary": "A video presentation showing database schema architecture.",
            "keyframe_summaries": [
                {"timestamp": 0.0, "description": "Title slide: database design"},
                {"timestamp": 5.0, "description": "Entity relationship diagram showing tables"},
            ],
            "tags": ["video", "database", "schema", "architecture"],
        }
    )
    return provider


# ======================================================================
# 1. Video Probe & Adaptive Keyframe Sampling Unit Tests
# ======================================================================

def test_probe_video_extracts_metadata(tmp_path):
    fake_video = tmp_path / "sample.mp4"
    fake_video.write_bytes(b"dummy mp4 data")

    fake_ffprobe_output = {
        "format": {
            "duration": "14.500000",
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "size": "1048576",
            "bit_rate": "578524",
        },
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "30/1",
                "avg_frame_rate": "30/1",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
            },
        ],
    }

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(fake_ffprobe_output),
            stderr="",
        )
        probe = probe_video(fake_video)

    assert probe["duration"] == 14.5
    assert probe["codec"] == "h264"
    assert probe["width"] == 1920
    assert probe["height"] == 1080
    assert probe["format_name"] == "mov,mp4,m4a,3gp,3g2,mj2"
    assert probe["fps"] == 30.0


def test_probe_video_handles_missing_file_or_ffprobe_error(tmp_path):
    missing_file = tmp_path / "non_existent.mp4"
    probe = probe_video(missing_file)
    assert probe["duration"] == 0.0
    assert probe["codec"] == "unknown"
    assert "error" in probe


def test_adaptive_keyframe_timestamps_density_and_24_cap():
    # <= 30 seconds: 1 frame every 2s
    ts_short = adaptive_keyframe_timestamps(duration=20.0, cap=24)
    assert len(ts_short) <= 24
    assert ts_short == [0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0]

    # <= 180 seconds (3m): 1 frame every 5s, capped at 24
    ts_mid = adaptive_keyframe_timestamps(duration=120.0, cap=24)
    assert len(ts_mid) <= 24
    assert all(t >= 0.0 for t in ts_mid)
    assert len(ts_mid) == 24  # 120 / 5 = 24

    # > 180 seconds: capped at 24 frames
    ts_long = adaptive_keyframe_timestamps(duration=600.0, cap=24)
    assert len(ts_long) == 24
    assert len(ts_long) <= 24
    assert ts_long[0] == 0.0
    assert ts_long[-1] <= 600.0

    # Extremely long video (e.g. 2 hours): strictly never exceeds 24 frames
    ts_huge = adaptive_keyframe_timestamps(duration=7200.0, cap=24)
    assert len(ts_huge) == 24


# ======================================================================
# 2. Image Analysis Pipeline & Semantic Search
# ======================================================================

@pytest.mark.asyncio
async def test_photo_analysis_flow_with_stubbed_vision_and_embedding(
    temp_store, sample_image, mock_vision_provider
):
    # Register image in MediaStore with status 'pending'
    item = temp_store.register_media(
        file_path=str(sample_image),
        collection="user",
        media_type="image",
        session_id="session-123",
        message_id="msg-456",
        probe_metadata={"origin": "user_upload", "name": "IMG_001.png"},
    )
    assert item["status"] == "pending"

    events_emitted = []

    def _event_listener(event):
        events_emitted.append(event)

    analyzer = MediaAnalyzer(
        store=temp_store,
        vision_provider=mock_vision_provider,
        on_event=_event_listener,
    )

    with patch("aether_engine.memory.episodic.litellm.aembedding", new=AsyncMock(side_effect=_fake_aembedding)):
        result = await analyzer.analyze_item(item["id"])

    assert result["status"] == "analyzed"
    assert "red bicycle" in result["summary_text"].lower()
    assert "bicycle" in result["tags"]
    assert result["analyzed_at"] is not None

    # Check store row is updated
    updated_in_store = temp_store.get_media(item["id"])
    assert updated_in_store["status"] == "analyzed"
    assert "red bicycle" in updated_in_store["summary_text"].lower()

    # Check events emitted
    event_types = [e.type for e in events_emitted]
    assert EventType.MEDIA_ANALYSIS_PROGRESS in event_types
    assert EventType.MEDIA_ANALYZED in event_types

    # Check embedding is stored in media_embeddings
    emb = temp_store.get_embedding(item["id"])
    assert emb is not None
    assert len(emb) == 64

    # Search library for "red bicycle"
    with patch("aether_engine.memory.episodic.litellm.aembedding", new=AsyncMock(side_effect=_fake_aembedding)):
        query_vec = await episodic.embed_text("red bicycle", context="test")
        matches = temp_store.search_media(
            query="red bicycle",
            query_vec=query_vec,
            limit=5,
        )

    assert len(matches) > 0
    assert matches[0]["id"] == item["id"]
    assert matches[0]["session_id"] == "session-123"
    assert matches[0]["message_id"] == "msg-456"


# ======================================================================
# 3. Video Analysis Flow
# ======================================================================

@pytest.mark.asyncio
async def test_video_analysis_extracts_probe_and_sampled_summary(
    temp_store, tmp_path, mock_vision_provider
):
    video_path = tmp_path / "diagram_walkthrough.mp4"
    video_path.write_bytes(b"dummy video bytes")

    item = temp_store.register_media(
        file_path=str(video_path),
        collection="generated",
        media_type="video",
        session_id="session-vid-1",
        message_id="msg-vid-1",
    )

    fake_ffprobe_output = {
        "format": {
            "duration": "45.0",
            "format_name": "mov,mp4",
            "size": "2048000",
            "bit_rate": "400000",
        },
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1280,
                "height": 720,
                "r_frame_rate": "24/1",
            }
        ],
    }

    analyzer = MediaAnalyzer(
        store=temp_store,
        vision_provider=mock_vision_provider,
    )

    with (
        patch("subprocess.run") as mock_run,
        patch("aether_engine.memory.episodic.litellm.aembedding", new=AsyncMock(side_effect=_fake_aembedding)),
    ):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(fake_ffprobe_output),
            stderr="",
        )
        result = await analyzer.analyze_item(item["id"])

    assert result["status"] == "analyzed"
    probe = result["probe_metadata"]
    assert probe["duration"] == 45.0
    assert probe["codec"] == "h264"
    assert probe["width"] == 1280
    assert probe["height"] == 720
    assert "database" in result["summary_text"].lower()
    assert "schema" in result["tags"]


# ======================================================================
# 4. Failure Resilience: Entry Remains Usable and Honestly Marked
# ======================================================================

@pytest.mark.asyncio
async def test_analysis_failure_leaves_entry_usable_and_marked_failed(
    temp_store, sample_image
):
    item = temp_store.register_media(
        file_path=str(sample_image),
        collection="user",
        media_type="image",
    )

    failing_provider = MagicMock()
    failing_provider.analyze_image = AsyncMock(side_effect=RuntimeError("Vision model connection timeout"))

    analyzer = MediaAnalyzer(
        store=temp_store,
        vision_provider=failing_provider,
    )

    result = await analyzer.analyze_item(item["id"])

    assert result["status"] == "failed"
    assert "timeout" in str(result.get("probe_metadata", {}).get("error", "")).lower()

    # The entry still exists and is accessible
    item_in_store = temp_store.get_media(item["id"])
    assert item_in_store is not None
    assert item_in_store["status"] == "failed"


# ======================================================================
# 5. Backfill of Pre-Existing Entries
# ======================================================================

@pytest.mark.asyncio
async def test_backfill_processes_all_pending_media(
    temp_store, sample_image, mock_vision_provider
):
    # Register 3 pre-existing items with pending status
    items = [
        temp_store.register_media(
            file_path=str(sample_image),
            collection="user",
            media_type="image",
            session_id=f"sess-{i}",
        )
        for i in range(3)
    ]
    for it in items:
        assert it["status"] == "pending"

    analyzer = MediaAnalyzer(
        store=temp_store,
        vision_provider=mock_vision_provider,
    )

    with patch("aether_engine.memory.episodic.litellm.aembedding", new=AsyncMock(side_effect=_fake_aembedding)):
        backfilled_count = await analyzer.backfill()

    assert backfilled_count == 3
    for it in items:
        stored = temp_store.get_media(it["id"])
        assert stored["status"] == "analyzed"
        assert temp_store.get_embedding(it["id"]) is not None


# ======================================================================
# 6. Degraded Embedding Circuit Breaker
# ======================================================================

@pytest.mark.asyncio
async def test_degraded_embedding_leaves_item_analyzed(
    temp_store, sample_image, mock_vision_provider
):
    item = temp_store.register_media(
        file_path=str(sample_image),
        collection="user",
        media_type="image",
    )

    # Trip the circuit breaker so embedding returns None
    episodic._embed_unavailable_until = time.monotonic() + 100.0

    analyzer = MediaAnalyzer(
        store=temp_store,
        vision_provider=mock_vision_provider,
    )

    result = await analyzer.analyze_item(item["id"])

    # Item analysis still succeeds (status='analyzed') even if embedding failed/degraded
    assert result["status"] == "analyzed"
    assert "red bicycle" in result["summary_text"].lower()
    # Embedding is absent, entry remains healthy
    assert temp_store.get_embedding(item["id"]) is None


# ======================================================================
# 7. Added coverage: keyframe persistence, scheduling seam, store API
# ======================================================================

@pytest.mark.asyncio
async def test_video_keyframe_summaries_persisted_in_probe_metadata(
    temp_store, tmp_path, mock_vision_provider
):
    video_path = tmp_path / "walkthrough.mp4"
    video_path.write_bytes(b"dummy video bytes")
    item = temp_store.register_media(
        file_path=str(video_path),
        collection="user",
        media_type="video",
    )
    fake_ffprobe_output = {
        "format": {"duration": "45.0", "format_name": "mov,mp4"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1280,
             "height": 720, "r_frame_rate": "24/1"}
        ],
    }
    analyzer = MediaAnalyzer(store=temp_store, vision_provider=mock_vision_provider)
    with (
        patch("subprocess.run") as mock_run,
        patch("aether_engine.memory.episodic.litellm.aembedding", new=AsyncMock(side_effect=_fake_aembedding)),
    ):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(fake_ffprobe_output), stderr="")
        await analyzer.analyze_item(item["id"])

    stored = temp_store.get_media(item["id"])
    keyframes = stored["probe_metadata"]["keyframe_summaries"]
    assert len(keyframes) == 2
    assert keyframes[0]["description"].startswith("Title slide")
    # Registration metadata survives alongside the probe results
    assert stored["probe_metadata"]["origin"] == "user_upload"


@pytest.mark.asyncio
async def test_schedule_media_analysis_drives_item_to_analyzed(
    temp_store, sample_image, mock_vision_provider, monkeypatch
):
    import aether_engine.media.analyzer as analyzer_mod

    item = temp_store.register_media(
        file_path=str(sample_image),
        collection="user",
        media_type="image",
    )
    stub_analyzer = MediaAnalyzer(
        store=temp_store, vision_provider=mock_vision_provider)
    monkeypatch.setattr(analyzer_mod, "get_media_analyzer", lambda: stub_analyzer)

    with patch("aether_engine.memory.episodic.litellm.aembedding", new=AsyncMock(side_effect=_fake_aembedding)):
        analyzer_mod.schedule_media_analysis(item["id"])
        for _ in range(100):
            await asyncio.sleep(0.05)
            if temp_store.get_media(item["id"])["status"] == "analyzed":
                break

    assert temp_store.get_media(item["id"])["status"] == "analyzed"


@pytest.mark.asyncio
async def test_analyze_unknown_item_returns_none(temp_store, mock_vision_provider):
    analyzer = MediaAnalyzer(store=temp_store, vision_provider=mock_vision_provider)
    assert await analyzer.analyze_item("no-such-id") is None


def test_store_embedding_roundtrip_and_lexical_only_search(temp_store, sample_image):
    item = temp_store.register_media(
        file_path=str(sample_image),
        collection="user",
        media_type="image",
        status="analyzed",
        summary_text="A red bicycle by a wall.",
        tags=["bicycle"],
    )
    assert temp_store.get_embedding(item["id"]) is None
    temp_store.store_embedding(item["id"], [0.1, 0.2, 0.3])
    assert temp_store.get_embedding(item["id"]) == [0.1, 0.2, 0.3]

    # Without a query vector the store answers lexically.
    hits = temp_store.search_media(query="bicycle", query_vec=None, limit=5)
    assert [h["id"] for h in hits] == [item["id"]]
    # And a query with no lexical or semantic overlap finds nothing.
    assert temp_store.search_media(query="helicopter", query_vec=None, limit=5) == []
