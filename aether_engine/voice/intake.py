"""Voice intake path: frames -> VAD -> endpointing -> utterance.

One half of the voice cascade coordinator (split out of
``pipeline.py`` in the Wave 5 structure wave; the Pass B craft
verdict on the old file was that five responsibilities in 1,031
lines — VAD feeding, endpointing, partial transcription,
final/commit, announcements + barge-in — made E7-07's
intake-blocks-on-transcription flaw structural).

This module owns everything from the transport's ``frames()`` to
a finished utterance: VAD feeding, the main endpointer, the
full-duplex barge-in watch (a second endpointer, so the main
path's utterance state is never corrupted), utterance
accumulation, the partial-transcription cadence, and barge-in
detection/capture/replay.

It never calls the STT or TTS itself. The two paths meet only at
the narrow surface of ``VoiceDelivery``
(``aether_engine.voice.delivery``), wired by ``VoicePipeline``
after both halves are constructed (``bind_delivery``):

* intake -> delivery: a finished utterance's audio
  (``finish_utterance``), a partial-transcription request
  (``request_partial`` — schedule only, never a wait), a new
  utterance starting (``begin_utterance``), and barge-in
  detection asking for output to stop (``interrupt_output``).
* delivery -> intake: whether output is currently active (the
  frame router's one question), and read access to the live
  utterance buffer for partial snapshots.

That one-way scheduling seam is E7-07's structural fix made
permanent: a transcription that takes seconds can delay
partials, but frame intake has no code path that waits on one.
"""
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Protocol

from aether_engine.voice.vad import Endpointer, VadEngine

if TYPE_CHECKING:
    from aether_engine.voice.delivery import VoiceDelivery


class VoiceTransport(Protocol):
    """Audio frame source + session config (SmallWebRTC seam)."""

    def unavailable_reason(self) -> str | None:
        """Why this transport cannot run here, or None when it can."""
        ...

    async def open(self) -> dict | None:
        """Open the peer connection; returns the webrtc_config the
        desktop side needs, or None."""
        ...

    def frames(self) -> AsyncIterator[bytes]:
        """Raw PCM16 frames from the user's microphone."""
        ...

    async def close(self) -> None:
        """Tear the connection down. Idempotent; ends ``frames()``."""
        ...


class VoiceIntake:
    """The frame-consuming half of one voice session.

    Constructed by ``VoicePipeline``, which also constructs the
    ``VoiceDelivery`` half and then calls ``bind_delivery``:
    an intake is not usable unbound. Session lifecycle (start /
    stop, the started check before ``run``) stays with the
    pipeline; this class owns only the frame path.
    """

    def __init__(
        self,
        *,
        vad: VadEngine,
        transport: VoiceTransport,
        endpointer: Endpointer,
        barge_endpointer: Endpointer,
        sample_rate: int = 16000,
        partial_min_audio_ms: int = 250,
        is_stopping: Callable[[], bool],
    ):
        self._vad = vad
        self._transport = transport
        # Both endpointers live here: the main one drives turn
        # taking on the intake path; the barge-in one watches
        # frames while output plays. VoicePipeline keeps its own
        # references to the same two objects (tests wrap their
        # methods in place), and the delivery half observes
        # transcripts into the main one through a call-time
        # lookup the pipeline wires.
        self._endpointer = endpointer
        self._barge_endpointer = barge_endpointer
        self._sample_rate = sample_rate
        self._partial_min_audio_ms = partial_min_audio_ms
        # Session teardown flag, owned by VoicePipeline (stop()
        # sets it); read through the callable so the frame loops
        # always see the live value.
        self._is_stopping = is_stopping
        self._delivery: VoiceDelivery | None = None

        self._utterance = bytearray()
        self._since_partial_ms = 0
        self._pending_partial_done = False
        # Barge-in capture state (E8-5.1b): frames of the
        # interrupting speech, accumulated by the barge-in
        # endpointer while output plays, replayed into the main
        # path once the interrupt lands.
        self._barge_frames: list[bytes] = []
        self._barge_speech_ms = 0

    def bind_delivery(self, delivery: "VoiceDelivery") -> None:
        """Wire the transcription/delivery half (called once by
        VoicePipeline, immediately after construction)."""
        self._delivery = delivery

    # -- buffer access for the delivery half --------------------------

    def has_utterance(self) -> bool:
        """Whether any audio is currently accumulated."""
        return bool(self._utterance)

    def utterance_snapshot(self) -> bytes:
        """An immutable copy of the live utterance buffer."""
        return bytes(self._utterance)

    def utterance_extends(self, snapshot: bytes) -> bool:
        """Whether the live buffer still extends ``snapshot``
        (the partial runner's staleness check: the utterance the
        snapshot belonged to is still the one growing)."""
        return self._utterance.startswith(snapshot)

    @property
    def user_speaking(self) -> bool:
        """Either endpointer currently hears the user (the
        announcement quiet-wait's question)."""
        return self._endpointer.in_speech or self._barge_endpointer.in_speech

    def reset_barge(self) -> None:
        """Drop any captured barge-in speech and reset its
        endpointer (session teardown)."""
        self._barge_frames = []
        self._barge_speech_ms = 0
        self._barge_endpointer.reset()

    # -- main loop -----------------------------------------------------

    async def run(self) -> None:
        """Consume frames until the transport ends or the
        session's stopping flag lands. VoicePipeline owns the
        started check before this runs and the delivery-task
        cleanup after it returns."""
        async for data in self._transport.frames():
            if self._is_stopping():
                break
            await self._consume_frame(data)

    async def _consume_frame(self, data: bytes) -> None:
        if self._delivery.output_active:
            await self._consume_frame_during_output(data)
            return
        if self._barge_frames:
            # The reply ended while the user was mid-interruption:
            # their captured speech starts the next utterance.
            frames = self._barge_frames
            self._barge_frames = []
            self._barge_speech_ms = 0
            self._barge_endpointer.reset()
            for captured in frames:
                if self._is_stopping():
                    return
                await self._consume_frame(captured)
            if self._is_stopping():
                return
            if self._delivery.output_active:
                # The replay itself started the next turn.
                await self._consume_frame_during_output(data)
                return
        await self._consume_main_frame(data)

    async def _consume_frame_during_output(self, data: bytes) -> None:
        """Full-duplex watch (E8-5.1b): while output plays, frames
        feed the barge-in endpointer instead of the main path.
        Sustained speech is a barge-in — playback stops at once
        and the captured frames replay into the main path as the
        next utterance. Anything shorter than the min-speech
        guard (a cough, a backchannel) is discarded."""
        probability = await self._vad.probability(data, self._sample_rate)
        frame_ms = round(len(data) / 2 / self._sample_rate * 1000)
        endpointer = self._barge_endpointer
        event = endpointer.feed(probability, frame_ms)
        if event == "speech_started":
            self._barge_frames = [data]
            self._barge_speech_ms = frame_ms
            return
        if event == "utterance_discarded":
            self._barge_frames = []
            self._barge_speech_ms = 0
            return
        if event == "utterance_end":
            # The user spoke a whole short utterance over the reply.
            self._barge_frames.append(data)
            await self._trigger_barge_in()
            return
        if not endpointer.in_speech:
            return
        self._barge_frames.append(data)
        if probability >= endpointer.config.speech_threshold:
            self._barge_speech_ms += frame_ms
        if self._barge_speech_ms >= endpointer.config.min_speech_ms:
            await self._trigger_barge_in()

    async def _trigger_barge_in(self) -> None:
        frames = self._barge_frames
        self._barge_frames = []
        self._barge_speech_ms = 0
        self._barge_endpointer.reset()
        await self._delivery.interrupt_output()
        if self._is_stopping():
            return
        for captured in frames:
            if self._is_stopping():
                return
            await self._consume_frame(captured)

    async def _consume_main_frame(self, data: bytes) -> None:
        probability = await self._vad.probability(data, self._sample_rate)
        frame_ms = round(len(data) / 2 / self._sample_rate * 1000)
        endpointer = self._endpointer
        event = endpointer.feed(probability, frame_ms)
        if event == "speech_started":
            self._utterance = bytearray(data)
            self._delivery.begin_utterance()
            self._since_partial_ms = 0
            self._pending_partial_done = False
            return
        if event == "utterance_discarded":
            self._utterance = bytearray()
            return
        if event == "utterance_end":
            self._utterance += data
            audio = bytes(self._utterance)
            self._utterance = bytearray()
            await self._delivery.finish_utterance(audio)
            return
        if not endpointer.in_speech:
            return
        self._utterance += data
        if probability >= endpointer.config.speech_threshold:
            self._since_partial_ms += frame_ms
            if self._since_partial_ms >= self._partial_min_audio_ms:
                self._since_partial_ms = 0
                # Schedule only (E7-07): the transcription runs
                # on the delivery half's own task; intake never
                # waits for it.
                self._delivery.request_partial()
        elif not self._pending_partial_done:
            # First trailing-silence frame: one more partial with the
            # full utterance context before the endpoint lands.
            self._pending_partial_done = True
            self._delivery.request_partial()
