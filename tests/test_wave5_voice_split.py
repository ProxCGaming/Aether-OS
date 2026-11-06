"""Wave 5 (blow-up overhaul) — the voice pipeline split.

Annex C row 4: ``voice/pipeline.py`` carried five responsibilities
in one class — the intake path (frames -> VAD -> endpointing) and
the delivery path (transcription, turn routing, announcements,
barge-in bookkeeping) — and E7-07's intake-blocks-on-transcription
flaw was structural. The split moves the two paths into
``aether_engine.voice.intake`` (``VoiceIntake``) and
``aether_engine.voice.delivery`` (``VoiceDelivery``); ``VoicePipeline``
is the facade that owns the stages and wires the halves together.

These suites test the halves directly, at the seam:
* the intake path runs against a recording delivery stub — no STT
  exists on that side of the seam at all;
* the delivery path runs against scripted buffer providers — no
  transport or VAD exists on that side;
* and the boundary guarantee E7-07 was about: with a transcription
  blocked mid-flight, the intake loop keeps consuming frames and
  the final/commit still lands.

All stages are scripted fakes, as in tests/test_voice_pipeline.py.
"""
import asyncio

from test_voice_pipeline import (
    Emitter,
    FakeStt,
    FakeTalker,
    FakeTts,
    FakeVad,
    frame,
    make_pipeline,
    speech_then_silence,
)
from test_wave2_voice_pipeline import BlockingPartialStt, QueueTransport

from aether_common.contracts import EventType
from aether_engine.voice.delivery import VoiceDelivery
from aether_engine.voice.intake import VoiceIntake
from aether_engine.voice.vad import Endpointer


async def wait_until(predicate, timeout=5.0):
    for _ in range(int(timeout / 0.005)):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached in time")


def compose_halves(*, stt, talker, emitter, transport,
                   vad=None, tts=None, **delivery_kwargs):
    """Wire an intake and a delivery exactly as VoicePipeline does,
    and return both halves plus their shared endpointer."""
    endpointer = Endpointer()
    intake = VoiceIntake(
        vad=vad if vad is not None else FakeVad({1: 0.9}),
        transport=transport,
        endpointer=endpointer,
        barge_endpointer=Endpointer(endpointer.config),
        partial_min_audio_ms=250,
        is_stopping=lambda: False,
    )
    delivery = VoiceDelivery(
        session_id="sess-split",
        stt=stt,
        tts=tts if tts is not None else FakeTts(),
        talker=talker,
        emit=emitter,
        tts_voice="af_heart",
        escalation_ack="On it.",
        utterance_snapshot=intake.utterance_snapshot,
        utterance_extends=intake.utterance_extends,
        has_utterance=intake.has_utterance,
        observe_transcript=lambda text: endpointer.observe_transcript(text),
        user_speaking=lambda: intake.user_speaking,
        is_started=lambda: True,
        is_stopping=lambda: False,
        **delivery_kwargs,
    )
    intake.bind_delivery(delivery)
    return intake, delivery, endpointer


# ---------------------------------------------------------------------------
# The facade wires the halves it delegates to
# ---------------------------------------------------------------------------

class TestFacadeComposition:
    def test_pipeline_builds_and_binds_both_halves(self):
        from test_voice_pipeline import FakeTransport

        pipeline = make_pipeline(FakeTransport())
        assert isinstance(pipeline._intake, VoiceIntake)
        assert isinstance(pipeline._delivery, VoiceDelivery)
        assert pipeline._intake._delivery is pipeline._delivery
        # The endpointers the facade exposes are the very objects
        # the intake half feeds.
        assert pipeline._intake._endpointer is pipeline._endpointer
        assert pipeline._intake._barge_endpointer is pipeline._barge_endpointer


# ---------------------------------------------------------------------------
# Intake in isolation: a delivery stub, no transcription anywhere
# ---------------------------------------------------------------------------

class RecordingDelivery:
    """The delivery side of the seam, reduced to a call log."""

    def __init__(self):
        self.output_active = False
        self.begun = 0
        self.finished = []
        self.partial_requests = 0
        self.interrupts = 0

    def begin_utterance(self):
        self.begun += 1

    async def finish_utterance(self, audio):
        self.finished.append(audio)

    def request_partial(self):
        self.partial_requests += 1

    async def interrupt_output(self):
        self.interrupts += 1
        return {}


class TestIntakeInIsolation:
    async def test_endpoint_hands_the_exact_utterance_bytes_over(self):
        transport = QueueTransport()
        stub = RecordingDelivery()
        endpointer = Endpointer()
        intake = VoiceIntake(
            vad=FakeVad({1: 0.9}),
            transport=transport,
            endpointer=endpointer,
            barge_endpointer=Endpointer(endpointer.config),
            partial_min_audio_ms=250,
            is_stopping=lambda: False,
        )
        intake.bind_delivery(stub)

        frames = speech_then_silence(speech_frames=10, silence_frames=30)
        task = asyncio.ensure_future(intake.run())
        try:
            transport.push_frames(frames)
            await wait_until(lambda: len(stub.finished) == 1)
        finally:
            transport.closed = True
            await asyncio.wait_for(task, timeout=5.0)

        # The utterance is the ten speech frames followed by the
        # trailing silence the endpointer consumed before declaring
        # the end — that silence is part of the handed-over audio,
        # exactly as it was when both halves lived in one class.
        assert stub.begun == 1
        (audio,) = stub.finished
        speech = b"".join(frame(1) for _ in range(10))
        assert audio[:len(speech)] == speech
        tail = audio[len(speech):]
        assert tail and set(tail) == {0}
        # The partial cadence fired across the seam (250 ms of
        # buffered speech) — as a request, not a transcription:
        # there is no STT on this side to have run one.
        assert stub.partial_requests >= 1
        # And the buffer was handed over empty.
        assert not intake.has_utterance()


# ---------------------------------------------------------------------------
# The boundary: intake never waits on transcription (E7-07, structural)
# ---------------------------------------------------------------------------

class TestIntakeDoesNotBlockOnTranscription:
    async def test_blocked_partial_never_stalls_the_frame_loop(self):
        stt = BlockingPartialStt()
        emitter = Emitter()
        talker = FakeTalker(deltas=("Done.",))
        transport = QueueTransport()
        intake, delivery, _ = compose_halves(
            stt=stt, talker=talker, emitter=emitter, transport=transport)

        task = asyncio.ensure_future(intake.run())
        try:
            transport.push_frames(speech_then_silence())
            # The final transcription is the SECOND STT call: it
            # commits the turn while partial #1 is still blocked
            # inside the first call.
            await wait_until(lambda: talker.prompts == ["the final words"])
            assert not stt.release_first.is_set()
            assert delivery._partial_task is not None
            assert not delivery._partial_task.done()
            # The intake loop is still live: once the reply has
            # played out, frames pushed now are consumed (a second
            # utterance begins and buffers in the main path) while
            # the transcription remains blocked.
            await wait_until(lambda: not delivery.output_active)
            transport.push_frames([frame(1)] * 5)
            await wait_until(lambda: len(intake.utterance_snapshot()) > 0)
            assert not stt.release_first.is_set()
        finally:
            stt.release_first.set()
            transport.closed = True
            await asyncio.wait_for(task, timeout=5.0)
            delivery.cancel_pending()

        finals = [
            e for e in emitter.of_type(EventType.VOICE_PARTIAL_TRANSCRIPT)
            if e.payload["is_final"]
        ]
        assert [e.payload["text"] for e in finals] == ["the final words"]


# ---------------------------------------------------------------------------
# Delivery in isolation: scripted buffer providers, no transport/VAD
# ---------------------------------------------------------------------------

class TestDeliveryInIsolation:
    def _delivery(self, *, buffer, stt, talker, emitter, **kwargs):
        return VoiceDelivery(
            session_id="sess-split",
            stt=stt,
            tts=FakeTts(),
            talker=talker,
            emit=emitter,
            tts_voice="af_heart",
            escalation_ack="On it.",
            utterance_snapshot=lambda: bytes(buffer),
            utterance_extends=lambda snap: buffer.startswith(snap),
            has_utterance=lambda: len(buffer) > 0,
            observe_transcript=lambda text: None,
            user_speaking=lambda: False,
            is_started=lambda: True,
            is_stopping=lambda: False,
            **kwargs,
        )

    async def test_finish_utterance_commits_the_turn_end_to_end(self):
        buffer = bytearray(frame(1) * 10)
        emitter = Emitter()
        talker = FakeTalker()
        committed = {"user": [], "assistant": []}

        async def on_user_final(text):
            committed["user"].append(text)

        async def on_assistant_final(text):
            committed["assistant"].append(text)

        delivery = self._delivery(
            buffer=buffer,
            stt=FakeStt(["hello there"]),
            talker=talker,
            emitter=emitter,
            on_user_final=on_user_final,
            on_assistant_final=on_assistant_final,
        )

        await delivery.finish_utterance(bytes(buffer))
        # finish_utterance commits the transcript synchronously and
        # starts the turn as a task (the frame loop must never wait
        # on one); let the turn run out before asserting on it.
        await wait_until(lambda: committed["assistant"])

        transcripts = emitter.of_type(EventType.VOICE_PARTIAL_TRANSCRIPT)
        assert [(e.payload["text"], e.payload["is_final"])
                for e in transcripts] == [("hello there", True)]
        assert talker.prompts == ["hello there"]
        text_deltas = [
            e.payload["text_delta"]
            for e in emitter.of_type(EventType.VOICE_OUTPUT_CHUNK)
            if e.payload.get("text_delta")
        ]
        assert "".join(text_deltas) == "The bicycle is red."
        assert committed == {
            "user": ["hello there"],
            "assistant": ["The bicycle is red."],
        }

    async def test_request_partial_publishes_from_the_snapshot_alone(self):
        buffer = bytearray(frame(1) * 10)
        emitter = Emitter()
        delivery = self._delivery(
            buffer=buffer,
            stt=FakeStt(["partial words"]),
            talker=FakeTalker(),
            emitter=emitter,
        )

        delivery.request_partial()
        await wait_until(lambda: len(emitter.events) == 1)

        (event,) = emitter.of_type(EventType.VOICE_PARTIAL_TRANSCRIPT)
        assert event.payload["text"] == "partial words"
        assert event.payload["is_final"] is False
