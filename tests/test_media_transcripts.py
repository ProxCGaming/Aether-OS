"""Tests for E8-4.4: video transcript stage for media analysis.

The transcript stage (split out of E8-4.3, board D4) rides the
local STT runner from E8-5.1a (``aether_engine.voice.stt``):

1. ACCEPTANCE: a video becomes searchable — library search AND
   the agent media_recall path — by a phrase that is only SPOKEN
   in it: absent from the filename, the vision summary, the tags,
   and every keyframe description.
2. The transcript is stored on the media entry with an honest
   status (done / unavailable / failed / no_audio).
3. Backfill: videos analysed before this slice gain transcripts
   through the analyzer's backfill — resumable and idempotent.
4. Missing deps (STT runtime or ffmpeg) degrade honestly: the
   item still reaches ``analyzed`` with the transcript marked
   unavailable — never fabricated text, never a failed analysis.
5. Images are never transcribed; media_recall stays READ_ONLY
   (plan-mode parity from E8-4.3).
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
import zlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aether_engine.media.analyzer import MediaAnalyzer
from aether_engine.media.store import MediaStore
from aether_engine.memory import episodic

# A phrase that appears ONLY in the fake transcript below — never
# in the fake vision output, the tags, or the video's filename.
SPOKEN_PHRASE = "photosynthesis converts sunlight into chemical energy"
SPOKEN_TOKEN = "photosynthesis"


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
    store = MediaStore(db_path=tmp_path / "test_media.db")
    store.init_db()
    return store


class FakeSttEngine:
    """SttEngine protocol fake (E8-5.1a surface)."""

    def __init__(self, transcript: str = "", unavailable: str | None = None,
                 error: Exception | None = None):
        self._transcript = transcript
        self._unavailable = unavailable
        self._error = error
        self.open_calls = 0
        self.close_calls = 0
        self.transcribe_calls: list[tuple[bytes, int]] = []

    def unavailable_reason(self) -> str | None:
        return self._unavailable

    async def open(self) -> None:
        self.open_calls += 1

    async def close(self) -> None:
        self.close_calls += 1

    async def transcribe(self, audio: bytes, *, sample_rate: int) -> str:
        self.transcribe_calls.append((audio, sample_rate))
        if self._error is not None:
            raise self._error
        return self._transcript


@pytest.fixture
def fake_stt():
    return FakeSttEngine(
        transcript=f"So today we explain how {SPOKEN_PHRASE} inside leaves.")


@pytest.fixture
def mock_vision_provider():
    provider = MagicMock()
    provider.analyze_image = AsyncMock(return_value={
        "summary": "A photo of a red bicycle parked by a brick wall.",
        "tags": ["bicycle", "red", "outdoor"],
    })
    provider.analyze_video_frames = AsyncMock(return_value={
        "summary": "A presenter talking in a bright garden studio.",
        "keyframe_summaries": [
            {"timestamp": 0.0, "description": "A presenter beside a whiteboard"},
            {"timestamp": 5.0, "description": "Close-up of green leaves in sunlight"},
        ],
        "tags": ["lecture", "garden", "presenter"],
    })
    return provider


_FFPROBE_JSON = json.dumps({
    "format": {"duration": "45.0", "format_name": "mov,mp4", "size": "2048000"},
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 1280,
         "height": 720, "r_frame_rate": "24/1"},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
})

FAKE_PCM = b"\x01\x00" * 8000


def _ffmpeg_dispatch(cmd, **kwargs):
    """subprocess.run stand-in: ffprobe JSON, keyframe ffmpeg ok,
    audio-extraction ffmpeg returns raw PCM on stdout."""
    if cmd[0] == "ffprobe":
        return MagicMock(returncode=0, stdout=_FFPROBE_JSON, stderr="")
    if cmd[0] == "ffmpeg" and "s16le" in cmd:
        return MagicMock(returncode=0, stdout=FAKE_PCM, stderr=b"")
    if cmd[0] == "ffmpeg":
        return MagicMock(returncode=0, stdout="", stderr="")
    raise AssertionError(f"unexpected subprocess call: {cmd}")


def _register_video(temp_store, tmp_path, name="garden_lecture.mp4"):
    video_path = tmp_path / name
    video_path.write_bytes(b"dummy video bytes")
    return temp_store.register_media(
        file_path=str(video_path),
        collection="user",
        media_type="video",
        session_id="sess-lecture",
        message_id="msg-lecture",
    )


def _patch_embedding():
    return patch(
        "aether_engine.memory.episodic.litellm.aembedding",
        new=AsyncMock(side_effect=_fake_aembedding))


# ======================================================================
# 1. ACCEPTANCE — spoken-only phrase becomes searchable everywhere
# ======================================================================

@pytest.mark.asyncio
async def test_spoken_only_phrase_becomes_searchable(
    temp_store, tmp_path, mock_vision_provider, fake_stt
):
    from aether_engine.tools.media_recall import (
        format_media_results,
        search_media_entries,
    )

    item = _register_video(temp_store, tmp_path)
    # Sanity: the phrase is genuinely spoken-only before analysis.
    assert SPOKEN_TOKEN not in item["file_path"]

    analyzer = MediaAnalyzer(
        store=temp_store, vision_provider=mock_vision_provider,
        stt_engine=fake_stt)
    with (
        patch("subprocess.run", side_effect=_ffmpeg_dispatch),
        _patch_embedding(),
    ):
        result = await analyzer.analyze_item(item["id"])

    assert result["status"] == "analyzed"
    # The vision side must NOT contain the phrase (spoken-only).
    assert SPOKEN_TOKEN not in result["summary_text"]
    assert all(SPOKEN_TOKEN not in t for t in result["tags"])
    keyframe_text = " ".join(
        k["description"] for k in result["probe_metadata"]["keyframe_summaries"])
    assert SPOKEN_TOKEN not in keyframe_text

    # The transcript is stored on the entry, marked done.
    assert SPOKEN_PHRASE in result["transcript_text"]
    assert result["transcript_status"] == "done"
    stored = temp_store.get_media(item["id"])
    assert SPOKEN_PHRASE in stored["transcript_text"]
    assert stored["transcript_status"] == "done"

    # The STT engine was fed extracted PCM at the STT sample rate.
    assert fake_stt.transcribe_calls == [(FAKE_PCM, 16000)]

    # Library search (the MEDIA_LIST query path) finds it.
    listed, total = temp_store.list_media(query=SPOKEN_TOKEN)
    assert total == 1 and listed[0]["id"] == item["id"]

    # Store hybrid search finds it lexically (no query vector)...
    hits = temp_store.search_media(query=SPOKEN_TOKEN, query_vec=None, limit=5)
    assert [h["id"] for h in hits] == [item["id"]]

    # ...and semantically, because the transcript joined the embedding.
    with _patch_embedding():
        outcome = await search_media_entries(
            SPOKEN_TOKEN, store=temp_store)
    assert [r["id"] for r in outcome["results"]] == [item["id"]]
    assert outcome["results"][0]["session_id"] == "sess-lecture"
    assert outcome["results"][0]["message_id"] == "msg-lecture"
    rendered = format_media_results(SPOKEN_TOKEN, outcome)
    assert "garden_lecture.mp4" in rendered


# ======================================================================
# 2. Backfill — pre-slice videos gain transcripts, resumable + idempotent
# ======================================================================

def _pre_analyzed_video(temp_store, tmp_path, name):
    """A video analysed before E8-4.4: analyzed, no transcript."""
    item = _register_video(temp_store, tmp_path, name=name)
    temp_store.update_analysis(
        item["id"],
        status="analyzed",
        summary_text="A presenter talking in a bright garden studio.",
        tags=["lecture", "garden"],
        probe_metadata={"origin": "user_upload", "duration": 45.0,
                        "codec": "h264", "keyframe_summaries": []},
        analyzed_at=1700000000000,
    )
    stored = temp_store.get_media(item["id"])
    assert stored["transcript_status"] is None
    return stored


@pytest.mark.asyncio
async def test_backfill_transcribes_previously_analyzed_videos(
    temp_store, tmp_path, mock_vision_provider, fake_stt
):
    old = _pre_analyzed_video(temp_store, tmp_path, "old_lecture.mp4")

    analyzer = MediaAnalyzer(
        store=temp_store, vision_provider=mock_vision_provider,
        stt_engine=fake_stt)
    with (
        patch("subprocess.run", side_effect=_ffmpeg_dispatch),
        _patch_embedding(),
    ):
        done = await analyzer.backfill_transcripts()

    assert done == 1
    stored = temp_store.get_media(old["id"])
    assert SPOKEN_PHRASE in stored["transcript_text"]
    assert stored["transcript_status"] == "done"
    # The original analysis results are untouched.
    assert stored["status"] == "analyzed"
    assert stored["summary_text"].startswith("A presenter")
    # The refreshed embedding now carries the transcript.
    assert temp_store.get_embedding(old["id"]) is not None

    # Idempotent: a second pass transcribes nothing again.
    with patch("subprocess.run", side_effect=_ffmpeg_dispatch):
        assert await analyzer.backfill_transcripts() == 0
    assert len(fake_stt.transcribe_calls) == 1


@pytest.mark.asyncio
async def test_backfill_transcripts_is_resumable_by_limit(
    temp_store, tmp_path, mock_vision_provider, fake_stt
):
    first = _pre_analyzed_video(temp_store, tmp_path, "first.mp4")
    second = _pre_analyzed_video(temp_store, tmp_path, "second.mp4")

    analyzer = MediaAnalyzer(
        store=temp_store, vision_provider=mock_vision_provider,
        stt_engine=fake_stt)
    with (
        patch("subprocess.run", side_effect=_ffmpeg_dispatch),
        _patch_embedding(),
    ):
        assert await analyzer.backfill_transcripts(limit=1) == 1
        done_ids = {
            temp_store.get_media(first["id"])["transcript_status"],
            temp_store.get_media(second["id"])["transcript_status"],
        }
        assert done_ids == {"done", None}  # exactly one progressed
        assert await analyzer.backfill_transcripts(limit=1) == 1
        assert await analyzer.backfill_transcripts() == 0
    assert len(fake_stt.transcribe_calls) == 2


@pytest.mark.asyncio
async def test_analyzer_backfill_also_fills_transcripts(
    temp_store, tmp_path, mock_vision_provider, fake_stt
):
    """The established backfill entry point covers transcripts too."""
    old = _pre_analyzed_video(temp_store, tmp_path, "legacy.mp4")
    analyzer = MediaAnalyzer(
        store=temp_store, vision_provider=mock_vision_provider,
        stt_engine=fake_stt)
    with (
        patch("subprocess.run", side_effect=_ffmpeg_dispatch),
        _patch_embedding(),
    ):
        await analyzer.backfill()
    stored = temp_store.get_media(old["id"])
    assert stored["transcript_status"] == "done"
    assert SPOKEN_PHRASE in stored["transcript_text"]


# ======================================================================
# 3. Honest degradation — missing deps / failures never sink analysis
# ======================================================================

@pytest.mark.asyncio
async def test_missing_stt_runtime_marks_transcript_unavailable(
    temp_store, tmp_path, mock_vision_provider
):
    unavailable_stt = FakeSttEngine(
        unavailable="faster-whisper is not installed (local STT)")
    item = _register_video(temp_store, tmp_path)
    analyzer = MediaAnalyzer(
        store=temp_store, vision_provider=mock_vision_provider,
        stt_engine=unavailable_stt)
    with (
        patch("subprocess.run", side_effect=_ffmpeg_dispatch),
        _patch_embedding(),
    ):
        result = await analyzer.analyze_item(item["id"])

    assert result["status"] == "analyzed"  # analysis itself is fine
    assert result["transcript_status"] == "unavailable"
    assert not result["transcript_text"]  # nothing fabricated
    assert "faster-whisper" in result["probe_metadata"]["transcript_error"]
    assert unavailable_stt.transcribe_calls == []


@pytest.mark.asyncio
async def test_missing_ffmpeg_marks_transcript_unavailable(
    temp_store, tmp_path, mock_vision_provider, fake_stt
):
    def _no_ffmpeg(cmd, **kwargs):
        if cmd[0] == "ffmpeg" and "s16le" in cmd:
            raise FileNotFoundError("ffmpeg")
        return _ffmpeg_dispatch(cmd, **kwargs)

    item = _register_video(temp_store, tmp_path)
    analyzer = MediaAnalyzer(
        store=temp_store, vision_provider=mock_vision_provider,
        stt_engine=fake_stt)
    with (
        patch("subprocess.run", side_effect=_no_ffmpeg),
        _patch_embedding(),
    ):
        result = await analyzer.analyze_item(item["id"])

    assert result["status"] == "analyzed"
    assert result["transcript_status"] == "unavailable"
    assert "ffmpeg" in result["probe_metadata"]["transcript_error"]
    assert fake_stt.transcribe_calls == []


@pytest.mark.asyncio
async def test_stt_failure_marks_transcript_failed_not_analysis(
    temp_store, tmp_path, mock_vision_provider
):
    failing_stt = FakeSttEngine(error=RuntimeError("model exploded"))
    item = _register_video(temp_store, tmp_path)
    analyzer = MediaAnalyzer(
        store=temp_store, vision_provider=mock_vision_provider,
        stt_engine=failing_stt)
    with (
        patch("subprocess.run", side_effect=_ffmpeg_dispatch),
        _patch_embedding(),
    ):
        result = await analyzer.analyze_item(item["id"])

    assert result["status"] == "analyzed"
    assert result["summary_text"].startswith("A presenter")
    assert result["transcript_status"] == "failed"
    assert "model exploded" in result["probe_metadata"]["transcript_error"]
    assert not result["transcript_text"]


@pytest.mark.asyncio
async def test_video_without_audio_stream_marks_no_audio(
    temp_store, tmp_path, mock_vision_provider, fake_stt
):
    def _silent(cmd, **kwargs):
        if cmd[0] == "ffmpeg" and "s16le" in cmd:
            return MagicMock(returncode=0, stdout=b"", stderr=b"")
        return _ffmpeg_dispatch(cmd, **kwargs)

    item = _register_video(temp_store, tmp_path)
    analyzer = MediaAnalyzer(
        store=temp_store, vision_provider=mock_vision_provider,
        stt_engine=fake_stt)
    with (
        patch("subprocess.run", side_effect=_silent),
        _patch_embedding(),
    ):
        result = await analyzer.analyze_item(item["id"])

    assert result["status"] == "analyzed"
    assert result["transcript_status"] == "no_audio"
    assert not result["transcript_text"]
    assert fake_stt.transcribe_calls == []


@pytest.mark.asyncio
async def test_silent_video_transcript_done_with_empty_text(
    temp_store, tmp_path, mock_vision_provider
):
    silent_stt = FakeSttEngine(transcript="")
    item = _register_video(temp_store, tmp_path)
    analyzer = MediaAnalyzer(
        store=temp_store, vision_provider=mock_vision_provider,
        stt_engine=silent_stt)
    with (
        patch("subprocess.run", side_effect=_ffmpeg_dispatch),
        _patch_embedding(),
    ):
        result = await analyzer.analyze_item(item["id"])
        # An empty-but-done transcript is terminal for backfill too.
        assert await analyzer.backfill_transcripts() == 0

    assert result["transcript_status"] == "done"
    assert result["transcript_text"] == ""


# ======================================================================
# 4. Scope fences — images untouched, tool stays read-only
# ======================================================================

@pytest.mark.asyncio
async def test_images_are_never_transcribed(
    temp_store, tmp_path, mock_vision_provider, fake_stt
):
    img = tmp_path / "IMG_001.png"
    img.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00"
        b"\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
    item = temp_store.register_media(
        file_path=str(img), collection="user", media_type="image")
    analyzer = MediaAnalyzer(
        store=temp_store, vision_provider=mock_vision_provider,
        stt_engine=fake_stt)
    with _patch_embedding():
        result = await analyzer.analyze_item(item["id"])

    assert result["status"] == "analyzed"
    assert fake_stt.transcribe_calls == []
    assert result["transcript_status"] is None


def test_media_recall_tool_stays_read_only():
    from aether_engine.tools.media_recall import create_media_recall_tool
    from aether_engine.tools.registry import ToolRisk

    assert create_media_recall_tool().risk == ToolRisk.READ_ONLY


# ======================================================================
# 5. Store migration — pre-4.4 databases gain the transcript columns
# ======================================================================

def test_store_migrates_pre_transcript_schema(tmp_path):
    db_path = tmp_path / "old_media.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE media_items (
            id TEXT PRIMARY KEY, session_id TEXT, message_id TEXT,
            collection TEXT NOT NULL, media_type TEXT NOT NULL,
            file_path TEXT NOT NULL, file_size INTEGER NOT NULL,
            mime_type TEXT, status TEXT NOT NULL DEFAULT 'pending',
            probe_metadata_json TEXT, summary_text TEXT,
            tags_json TEXT DEFAULT '[]', user_notes TEXT DEFAULT '',
            created_at INTEGER NOT NULL, analyzed_at INTEGER
        )""")
    conn.execute("""
        CREATE TABLE media_embeddings (
            media_id TEXT PRIMARY KEY, embedding BLOB NOT NULL)""")
    conn.commit()
    conn.close()

    store = MediaStore(db_path=db_path)
    store.init_db()
    item = store.register_media(
        file_path=str(tmp_path / "x.mp4"), collection="user",
        media_type="video")
    assert item["transcript_text"] is None
    assert item["transcript_status"] is None

    updated = store.update_analysis(
        item["id"], transcript_text="hello world",
        transcript_status="done")
    assert updated["transcript_text"] == "hello world"
    assert updated["transcript_status"] == "done"


# ======================================================================
# 6. Async scheduling — transcript backfill is schedulable fire-and-forget
# ======================================================================

@pytest.mark.asyncio
async def test_schedule_transcript_backfill_runs_in_background(
    temp_store, tmp_path, mock_vision_provider, fake_stt, monkeypatch
):
    import aether_engine.media.analyzer as analyzer_mod

    old = _pre_analyzed_video(temp_store, tmp_path, "scheduled.mp4")
    stub_analyzer = MediaAnalyzer(
        store=temp_store, vision_provider=mock_vision_provider,
        stt_engine=fake_stt)
    monkeypatch.setattr(analyzer_mod, "get_media_analyzer", lambda: stub_analyzer)

    with (
        patch("subprocess.run", side_effect=_ffmpeg_dispatch),
        _patch_embedding(),
    ):
        analyzer_mod.schedule_transcript_backfill()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if temp_store.get_media(old["id"])["transcript_status"] == "done":
                break

    assert temp_store.get_media(old["id"])["transcript_status"] == "done"
