"""Wave 2 (blow-up overhaul) — voice pipeline intake fixes.

Pins two Pass A chunk-E7 findings against ``voice/pipeline.py``:

* E7-06 [S3] ``speak_announcement`` waited for a quiet moment with
  no timeout: a user who keeps talking, or output activity that
  never settles, held the caller's task forever. The wait is now
  bounded by ``_ANNOUNCEMENT_QUIET_TIMEOUT_S`` and gives up
  (returns False — the summary still stands in chat) when no
  quiet moment arrives.

* E7-07 [S3] Partial transcription blocked frame intake: the
  frame loop awaited a full re-transcription of the growing
  utterance buffer inline, so while a slow partial ran the
  bounded RTC queue overflowed and silently dropped the user's
  speech. Partials now run as a background task off a snapshot;
  the frame loop never waits on one, requests coalesce, and a
  partial whose utterance has since ended is never published.

All stages are scripted fakes, as in tests/test_voice_pipeline.py.
"""
import asyncio

from test_voice_pipeline import (
    Emitter,
    FakeStt,
    FakeTts,
    FakeVad,
    make_pipeline,
    speech_then_silence,
)

import aether_engine.voice.pipeline as pipeline_module
from aether_common.contracts import EventType


async def wait_until(predicate, timeout=5.0):
    for _ in range(int(timeout / 0.005)):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached in time")


class QueueTransport:
    """frames() pulls from a queue the test feeds frame by frame
    (the test_voice_barge_in shape): idles like a live mic until
    closed."""

    def __init__(self):
        self._queue = asyncio.Queue()
        self.opened = False
        self.closed = False
        self.config = {"iceServers": [{"urls": "stun:fake.local"}]}

    def unavailable_reason(self):
        return None

    async def open(self):
        self.opened = True
        return self.config

    def push_frames(self, frames):
        for data in frames:
            self._queue.put_nowait(data)

    async def frames(self):
        while not self.closed:
            try:
                data = await asyncio.wait_for(self._queue.get(), timeout=0.01)
            except TimeoutError:
                continue
            yield data
            # Yield per frame like a real-time transport, so
            # pipeline background tasks (partials) interleave
            # the way they do when frames arrive 20 ms apart.
            await asyncio.sleep(0)

    async def close(self):
        self.closed = True


def _text_deltas(emitter):
    return "".join(
        e.payload.get("text_delta") or ""
        for e in emitter.of_type(EventType.VOICE_OUTPUT_CHUNK)
    )


# ---------------------------------------------------------------------------
# E7-06: the announcement quiet-wait is bounded
# ---------------------------------------------------------------------------


class GatedTalker:
    """First delta flows, then delivery blocks on the gate: a
    reply turn whose output activity never settles until the
    test releases it."""

    def __init__(self):
        self.prompts = []
        self.gate = asyncio.Event()

    async def __call__(self, text):
        self.prompts.append(text)
        yield "First part. "
        await self.gate.wait()
        yield "Second part."


class TestAnnouncementQuietWaitBound:
    async def test_announcement_gives_up_when_output_never_settles(
        self, monkeypatch,
    ):
        monkeypatch.setattr(
            pipeline_module, "_ANNOUNCEMENT_QUIET_TIMEOUT_S", 0.3,
            raising=False)
        transport = QueueTransport()
        emitter = Emitter()
        tts = FakeTts()
        talker = GatedTalker()
        pipeline = make_pipeline(
            transport, stt=FakeStt(["hi"] * 4), talker=talker,
            tts=tts, emitter=emitter)
        await pipeline.start()
        run_task = asyncio.ensure_future(pipeline.run())
        try:
            transport.push_frames(speech_then_silence())
            # The turn is live and parked mid-reply: its first
            # sentence has been spoken, the rest is gated, so
            # output activity never settles on its own.
            await wait_until(
                lambda: len(talker.prompts) == 1 and len(tts.requests) >= 1)
            spoken = await asyncio.wait_for(
                pipeline.speak_announcement("Your job finished."),
                timeout=5.0)
            assert spoken is False
            assert "Your job finished." not in _text_deltas(emitter)
        finally:
            talker.gate.set()
            await pipeline.stop()
            await asyncio.wait_for(run_task, timeout=5.0)


# ---------------------------------------------------------------------------
# E7-07: partial transcription must not block frame intake
# ---------------------------------------------------------------------------


class BlockingPartialStt:
    """The first transcribe() call (a partial, on the cadence)
    blocks until the test releases it; later calls answer at
    once. On the defective pipeline the first call runs inline
    in the frame loop, so the utterance can never finish while
    it is blocked."""

    def __init__(self):
        self.calls = []
        self.first_call_started = asyncio.Event()
        self.release_first = asyncio.Event()

    def unavailable_reason(self):
        return None

    async def open(self):
        pass

    async def close(self):
        pass

    async def transcribe(self, audio, *, sample_rate):
        self.calls.append(len(audio))
        if len(self.calls) == 1:
            self.first_call_started.set()
            await self.release_first.wait()
            return "stale partial"
        return "the final words"


class RecordingTalker:
    def __init__(self):
        self.prompts = []

    async def __call__(self, text):
        self.prompts.append(text)
        yield "Okay."


class TestPartialTranscriptionOffIntakePath:
    async def test_final_commits_while_partial_still_running(self):
        transport = QueueTransport()
        emitter = Emitter()
        stt = BlockingPartialStt()
        talker = RecordingTalker()
        pipeline = make_pipeline(
            transport, vad=FakeVad({1: 0.9}), stt=stt,
            talker=talker, emitter=emitter)
        await pipeline.start()
        run_task = asyncio.ensure_future(pipeline.run())
        try:
            transport.push_frames(speech_then_silence())
            await wait_until(lambda: stt.first_call_started.is_set())
            # The partial is still blocked, yet the frame loop
            # must keep consuming: the utterance ends and its
            # final transcript commits as a normal turn.
            await wait_until(lambda: talker.prompts == ["the final words"])
            assert not stt.release_first.is_set()
            # Releasing the stale partial must not publish it:
            # its utterance is over.
            stt.release_first.set()
            await asyncio.sleep(0.3)
            partial_texts = [
                e.payload.get("text")
                for e in emitter.of_type(EventType.VOICE_PARTIAL_TRANSCRIPT)
            ]
            assert "stale partial" not in partial_texts
            assert len(stt.calls) == 2
        finally:
            stt.release_first.set()
            await pipeline.stop()
            await asyncio.wait_for(run_task, timeout=5.0)
