"""Wave 2 (blow-up overhaul) — media analysis + store search fixes.

Pins three Pass A chunk-E7 findings:

* E7-03 [S3] ``extract_audio_pcm`` loaded a video's entire PCM
  track into memory as one bytes (32,000 B/s of media: ~346 MB
  for a 3-hour video) and handed it whole to STT. Extraction is
  now windowed: ffmpeg runs per bounded window and the
  transcript stage transcribes chunk by chunk, so peak memory
  is one window no matter how long the video runs.

* E7-12 [S4] ``list_media`` / ``list_artifacts`` interpolated
  the search text into LIKE patterns unescaped, so ``%`` or
  ``_`` matched every row. The query is user text, not a
  pattern: metacharacters are escaped (the knowledge-graph
  S1-N2 shape) in both stores.

* E7-20 [S4] ``analyze_item`` had no per-item lock — only the
  scheduler wrapper deduped — so a startup backfill and an
  arrival-triggered analysis of the same item could run
  concurrently (two vision calls, two embedding writes, last
  writer wins). Concurrent calls for one item now serialise.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import aether_engine.media.analyzer as analyzer_module
from aether_engine.artifacts.store import ArtifactStore
from aether_engine.media.analyzer import MediaAnalyzer
from aether_engine.media.store import MediaStore
from aether_engine.memory import episodic

# With _AUDIO_CHUNK_SECONDS patched to 0.5 s: one window of
# PCM16 mono at 16 kHz is 0.5 * 16000 * 2 bytes.
WINDOW_BYTES = 16000


async def wait_until(predicate, timeout=5.0):
    for _ in range(int(timeout / 0.005)):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached in time")


# ---------------------------------------------------------------------------
# E7-03: windowed audio extraction + chunked transcription
# ---------------------------------------------------------------------------


class RecordingStt:
    """STT stand-in: records every buffer it is handed."""

    def __init__(self):
        self.calls = []
        self.opened = False

    def unavailable_reason(self):
        return None

    async def open(self):
        self.opened = True

    async def close(self):
        pass

    async def transcribe(self, audio, *, sample_rate):
        self.calls.append(bytes(audio))
        return f"words{len(self.calls) - 1}"


def _long_video_run(cmd, **kwargs):
    """ffmpeg stand-in for a 3.5-window video. Windowed calls
    (``-ss``/``-t`` present) return that window's PCM; an
    unwindowed call (the defective shape) returns the whole
    track at once."""
    assert cmd[0] == "ffmpeg"
    if "-ss" in cmd:
        start = float(cmd[cmd.index("-ss") + 1])
        index = round(start / 0.5)
        if index < 3:
            payload = bytes([index + 1]) * WINDOW_BYTES
        elif index == 3:
            payload = b"\x09" * (WINDOW_BYTES // 2)
        else:
            payload = b""
        return SimpleNamespace(returncode=0, stdout=payload, stderr=b"")
    whole = b"\x01" * WINDOW_BYTES * 3 + b"\x09" * (WINDOW_BYTES // 2)
    return SimpleNamespace(returncode=0, stdout=whole, stderr=b"")


class TestWindowedTranscription:
    async def test_long_video_transcribes_in_bounded_chunks(
        self, monkeypatch,
    ):
        monkeypatch.setattr(
            analyzer_module, "_AUDIO_CHUNK_SECONDS", 0.5, raising=False)
        stt = RecordingStt()
        analyzer = MediaAnalyzer(store=MagicMock(), stt_engine=stt)
        item = {"id": "m1", "file_path": "/videos/long.mp4"}
        with patch("subprocess.run", side_effect=_long_video_run):
            text, status, error = await analyzer._transcribe_video(item)
        assert status == "done"
        assert error is None
        # Four window transcriptions, none larger than one window.
        assert [len(call) for call in stt.calls] == [
            WINDOW_BYTES, WINDOW_BYTES, WINDOW_BYTES, WINDOW_BYTES // 2]
        assert text == "words0 words1 words2 words3"

    async def test_no_audio_still_maps_to_no_audio(self):
        stt = RecordingStt()
        analyzer = MediaAnalyzer(store=MagicMock(), stt_engine=stt)
        empty = SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with patch("subprocess.run", return_value=empty):
            text, status, error = await analyzer._transcribe_video(
                {"id": "m2", "file_path": "/videos/silent.mp4"})
        assert (text, status, error) == (None, "no_audio", None)
        assert stt.calls == []

    async def test_ffmpeg_failure_still_maps_to_failed(self):
        stt = RecordingStt()
        analyzer = MediaAnalyzer(store=MagicMock(), stt_engine=stt)
        bad = SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"moov atom not found")
        with patch("subprocess.run", return_value=bad):
            _text, status, error = await analyzer._transcribe_video(
                {"id": "m3", "file_path": "/videos/broken.mp4"})
        assert status == "failed"
        assert "moov atom not found" in error

    async def test_missing_ffmpeg_still_maps_to_unavailable(self):
        stt = RecordingStt()
        analyzer = MediaAnalyzer(store=MagicMock(), stt_engine=stt)
        with patch("subprocess.run", side_effect=FileNotFoundError):
            _text, status, error = await analyzer._transcribe_video(
                {"id": "m4", "file_path": "/videos/any.mp4"})
        assert status == "unavailable"
        assert "ffmpeg is not installed" in error


# ---------------------------------------------------------------------------
# E7-12: LIKE metacharacters in listing searches are literal
# ---------------------------------------------------------------------------


@pytest.fixture
def media_store(tmp_path):
    store = MediaStore(db_path=tmp_path / "media.db")
    store.init_db()
    return store


class TestMediaLikeEscaping:
    def test_underscore_matches_literally(self, media_store):
        media_store.register_media(
            file_path="/lib/a_b.mp4", collection="user", media_type="video")
        media_store.register_media(
            file_path="/lib/aXb.mp4", collection="user", media_type="video")
        media_store.register_media(
            file_path="/lib/plain.mp4", collection="user", media_type="video")
        items, total = media_store.list_media(query="a_b")
        assert total == 1
        assert items[0]["file_path"] == "/lib/a_b.mp4"

    def test_percent_matches_literally(self, media_store):
        media_store.register_media(
            file_path="/lib/a_b.mp4", collection="user", media_type="video")
        media_store.register_media(
            file_path="/lib/plain.mp4", collection="user", media_type="video")
        _items, total = media_store.list_media(query="%")
        assert total == 0

    def test_plain_substring_search_unchanged(self, media_store):
        media_store.register_media(
            file_path="/lib/aXb.mp4", collection="user", media_type="video")
        items, total = media_store.list_media(query="aXb")
        assert total == 1
        assert items[0]["file_path"] == "/lib/aXb.mp4"


@pytest.fixture
def artifact_store(tmp_path):
    return ArtifactStore(db_path=tmp_path / "artifacts.db")


def _record(store, task_id, title, file_path):
    return store.record_artifact(
        session_id="s1", task_id=task_id, kind="doc",
        title=title, file_path=file_path)


class TestArtifactLikeEscaping:
    def test_underscore_matches_literally(self, artifact_store):
        _record(artifact_store, "t1", "a_b plan", "/x/a_b.txt")
        _record(artifact_store, "t2", "aXb plan", "/x/aXb.txt")
        items, total = artifact_store.list_artifacts(search="a_b")
        assert total == 1
        assert items[0]["title"] == "a_b plan"

    def test_percent_matches_literally(self, artifact_store):
        _record(artifact_store, "t1", "a_b plan", "/x/a_b.txt")
        _record(artifact_store, "t2", "plain plan", "/x/plain.txt")
        _record(artifact_store, "t3", "100% done", "/x/done.txt")
        items, total = artifact_store.list_artifacts(search="%")
        assert total == 1
        assert items[0]["title"] == "100% done"


# ---------------------------------------------------------------------------
# E7-20: analyze_item is serialised per item
# ---------------------------------------------------------------------------


class GatedVision:
    """Vision stand-in: every call parks on the gate, so any
    concurrency is observable."""

    def __init__(self):
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self.release = asyncio.Event()

    async def analyze_image(self, path, mime_type=None):
        self.calls += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            await self.release.wait()
            return {"summary": "A thing.", "tags": ["thing"]}
        finally:
            self.concurrent -= 1


class TestAnalyzeItemSerialised:
    async def test_concurrent_calls_for_one_item_do_not_overlap(
        self, tmp_path,
    ):
        store = MediaStore(db_path=tmp_path / "media.db")
        store.init_db()
        img = tmp_path / "pic.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")
        item = store.register_media(
            file_path=str(img), collection="user", media_type="image")
        vision = GatedVision()
        analyzer = MediaAnalyzer(store=store, vision_provider=vision)
        with patch.object(
            episodic, "embed_text", new=AsyncMock(return_value=None),
        ):
            first = asyncio.ensure_future(analyzer.analyze_item(item["id"]))
            second = asyncio.ensure_future(analyzer.analyze_item(item["id"]))
            await wait_until(lambda: vision.calls >= 1)
            await asyncio.sleep(0.2)
            # The second call is parked on the per-item lock, not
            # racing the first through the vision stage.
            assert vision.calls == 1
            vision.release.set()
            result_first, result_second = await asyncio.gather(first, second)
        assert vision.max_concurrent == 1
        assert result_first["status"] == "analyzed"
        assert result_second["status"] == "analyzed"
