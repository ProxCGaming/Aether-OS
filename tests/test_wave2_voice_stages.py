"""Wave 2 (blow-up overhaul) — voice stage + RTC transport fixes.

Pins four Pass A chunk-E7 / lane findings:

* E7-08 [S3] ``SileroVad._infer`` truncated or zero-padded every
  frame to exactly 512 samples and ran it as one inference, so
  the production ~20 ms frames (320 samples) were judged on
  audio up to 37.5% synthetic silence, and the context window
  was taken from the padded audio. The stage now accumulates
  real samples and only ever feeds the model true 512-sample
  windows of the actual stream.

* E7-13 [S4] Kokoro TTS synthesis ran in a ``to_thread`` producer
  feeding an unbounded queue: cancelling the consumer future
  could not stop the thread, which kept synthesising the
  interrupted sentence into a queue nobody drained, and its
  ``call_soon_threadsafe`` could raise on a closed loop. The
  queue is now bounded and the producer stops when the
  consumer leaves.

* E7-19 [S4] ``FasterWhisperStt.transcribe`` awaited the worker
  thread with no timeout: a wedged faster-whisper call hung
  the turn forever. The await is now bounded and raises
  ``TimeoutError``.

* N-32 [S3] ``WebRtcVoiceTransport._on_track`` overwrote
  ``self._pump_task`` unconditionally: the first track's pump
  leaked past ``close()``, and its end-of-stream sentinel could
  end ``frames()`` while the second track was still live. A
  replacement track now cancels the previous pump, and only
  the live pump may end the stream.
"""
import asyncio
import threading
from types import SimpleNamespace

import pytest

import aether_engine.voice.stt as stt_module
import aether_engine.voice.tts as tts_module
from aether_engine.voice.rtc import WebRtcVoiceTransport
from aether_engine.voice.stt import FasterWhisperStt
from aether_engine.voice.tts import KokoroTts
from aether_engine.voice.vad import SILERO_FRAME_SAMPLES, SileroVad

np = pytest.importorskip("numpy")


# ---------------------------------------------------------------------------
# E7-08: Silero is fed only true 512-sample windows of real audio
# ---------------------------------------------------------------------------


class RecordingSession:
    """ONNX session stand-in: records every input it is fed."""

    def __init__(self):
        self.inputs = []

    def run(self, _outputs, feed):
        self.inputs.append(feed["input"].copy())
        return np.array([[0.9]], dtype=np.float32), feed["state"]


def _vad_with_session():
    vad = SileroVad()
    vad._np = np
    vad._session = RecordingSession()
    vad._state = np.zeros((2, 1, 128), dtype=np.float32)
    vad._context = None
    return vad


def _pcm(samples) -> bytes:
    return np.asarray(samples, dtype=np.int16).tobytes()


class TestSileroWindowing:
    def test_partial_frames_accumulate_without_padding(self):
        vad = _vad_with_session()
        session = vad._session
        stream = np.arange(1280, dtype=np.int16)  # identifiable samples

        # One 20 ms frame (320 samples): no full window exists
        # yet, so the model must not run at all — and certainly
        # not on a zero-padded frame.
        prob = vad._infer(_pcm(stream[:320]), 16000)
        assert session.inputs == []
        assert prob == 0.0

        # The second frame completes the first true window:
        # exactly one inference, prefixed with a zeroed context,
        # over the stream's real first 512 samples.
        vad._infer(_pcm(stream[320:640]), 16000)
        assert len(session.inputs) == 1
        fed = session.inputs[0]
        assert fed.shape == (1, 64 + SILERO_FRAME_SAMPLES)
        np.testing.assert_array_equal(fed[0, :64], np.zeros(64))
        np.testing.assert_array_equal(
            fed[0, 64:],
            stream[:512].astype(np.float32) / 32768.0)

        # Two more frames complete a second window: its context
        # is the tail of the previous REAL window (never padded
        # audio), and its samples are the stream's next 512.
        vad._infer(_pcm(stream[640:960]), 16000)
        prob = vad._infer(_pcm(stream[960:1280]), 16000)
        assert len(session.inputs) == 2
        fed = session.inputs[1]
        np.testing.assert_array_equal(
            fed[0, :64],
            stream[448:512].astype(np.float32) / 32768.0)
        np.testing.assert_array_equal(
            fed[0, 64:],
            stream[512:1024].astype(np.float32) / 32768.0)
        assert prob == pytest.approx(0.9)

    def test_oversized_frame_runs_every_window_and_keeps_the_tail(self):
        vad = _vad_with_session()
        session = vad._session
        stream = np.arange(1024 + 320, dtype=np.int16)

        vad._infer(_pcm(stream[:1024]), 16000)
        assert len(session.inputs) == 2
        # No samples were dropped: the retained 320-sample tail
        # plus 192 fresh samples complete the third window.
        vad._infer(_pcm(stream[1024:]), 16000)
        vad._infer(_pcm(np.arange(192, dtype=np.int16)), 16000)
        assert len(session.inputs) == 3
        fed = session.inputs[2]
        np.testing.assert_array_equal(
            fed[0, 64:64 + 320],
            stream[1024:].astype(np.float32) / 32768.0)


# ---------------------------------------------------------------------------
# E7-13: TTS producer is bounded and stops with its consumer
# ---------------------------------------------------------------------------


class CountingPipeline:
    """Fake Kokoro pipeline: counts the chunks the producer
    pulls from the synthesis generator."""

    def __init__(self, chunks=50, error=None):
        self.chunks = chunks
        self.error = error
        self.produced = 0

    def __call__(self, text, voice=None, speed=1.0):
        def gen():
            if self.error is not None:
                raise self.error
            for _ in range(self.chunks):
                self.produced += 1
                yield None, None, [0.0, 0.05]

        return gen()


def _tts_with(pipeline) -> KokoroTts:
    tts = KokoroTts()
    # FF3-01 made the engine hold one pipeline PER LANGUAGE;
    # seed the dict the way open() + a first synthesize would.
    tts._pipelines = {"a": pipeline}
    return tts


class TestTtsProducerBounds:
    async def test_producer_stops_when_consumer_leaves(self):
        pipe = CountingPipeline(chunks=50)
        tts = _tts_with(pipe)
        gen = tts.synthesize("hello", voice="af_heart")
        first = await gen.__anext__()
        assert first
        await gen.aclose()
        await asyncio.sleep(0.5)
        bound = getattr(tts_module, "_TTS_QUEUE_MAXSIZE", 8)
        # What the consumer took + a full queue + one in flight —
        # never the whole interrupted sentence.
        assert pipe.produced <= 1 + bound + 1
        assert pipe.produced < 50

    async def test_full_consumption_is_unchanged(self):
        pipe = CountingPipeline(chunks=3)
        tts = _tts_with(pipe)
        chunks = [c async for c in tts.synthesize("hi", voice="af_heart")]
        assert len(chunks) == 3
        assert all(chunks)

    async def test_synthesis_error_still_reaches_the_consumer(self):
        pipe = CountingPipeline(error=RuntimeError("boom"))
        tts = _tts_with(pipe)
        with pytest.raises(RuntimeError, match="boom"):
            async for _ in tts.synthesize("hi", voice="af_heart"):
                pass


# ---------------------------------------------------------------------------
# E7-19: STT transcription await is bounded
# ---------------------------------------------------------------------------


class BlockingModel:
    """faster-whisper stand-in whose transcribe wedges until
    released (a corrupt model / driver stall)."""

    def __init__(self):
        self.release = threading.Event()

    def transcribe(self, wav, **kwargs):
        self.release.wait(timeout=30)
        return [SimpleNamespace(text="never")], None


class FastModel:
    def transcribe(self, wav, **kwargs):
        return [SimpleNamespace(text=" hello ")], None


class TestSttTranscribeBound:
    async def test_wedged_transcription_times_out(self, monkeypatch):
        monkeypatch.setattr(
            stt_module, "_TRANSCRIBE_TIMEOUT_S", 0.3, raising=False)
        model = BlockingModel()
        stt = FasterWhisperStt(model="small")
        stt._model = model
        task = asyncio.ensure_future(
            stt.transcribe(b"\x00\x00" * 100, sample_rate=16000))
        try:
            await asyncio.sleep(1.5)
            assert task.done(), "transcribe() hung past its bound"
            assert isinstance(task.exception(), TimeoutError)
        finally:
            model.release.set()

    async def test_fast_transcription_is_unchanged(self):
        stt = FasterWhisperStt(model="small")
        stt._model = FastModel()
        assert await stt.transcribe(b"\x00\x00" * 100, sample_rate=16000) \
            == "hello"


# ---------------------------------------------------------------------------
# N-32: a replacement track retires the previous pump
# ---------------------------------------------------------------------------


def _av_frame(samples=320, rate=48000):
    import av

    frame = av.AudioFrame.from_ndarray(
        np.zeros((1, samples), dtype=np.int16), format="s16",
        layout="mono")
    frame.sample_rate = rate
    return frame


class ScriptedTrack:
    """mode "block": one frame, then recv() hangs (a live track).
    mode "end": one frame, then recv() raises (the track is over)."""

    def __init__(self, mode):
        self.kind = "audio"
        self.mode = mode
        self._hang = asyncio.Event()

    async def recv(self):
        if self.mode == "block":
            self.mode = "blocked"
            return _av_frame()
        if self.mode == "end":
            self.mode = "ended"
            return _av_frame()
        if self.mode == "ended":
            raise RuntimeError("track ended")
        await self._hang.wait()
        return _av_frame()


class TestSecondTrackRetiresFirstPump:
    async def test_replacement_cancels_previous_pump(self):
        transport = WebRtcVoiceTransport()
        transport._pc = object()  # frames() only checks presence
        transport._on_track(ScriptedTrack(mode="block"))
        first_task = transport._pump_task
        assert first_task is not None

        transport._on_track(ScriptedTrack(mode="end"))
        assert transport._pump_task is not first_task

        # The stream serves the live track and ends when IT ends.
        got = [item async for item in transport.frames()]
        assert got and all(isinstance(item, bytes) for item in got)
        # The replaced pump is retired, not leaked past its track.
        assert first_task.done()

        await transport.close()
        assert first_task.cancelled() or first_task.done()
