"""Production SmallWebRTC transport (Epic 8, E8-5.1a / F-12a).

The engine side of the streaming-voice peer connection. The
desktop renderer holds the microphone, so it is the offerer: it
creates the offer, the engine answers, and ICE candidates are
exchanged both ways. Signalling rides the engine WebSocket
(contracts: ``VOICE_RTC_OFFER_*`` / ``VOICE_RTC_ICE_*`` /
``VOICE_RTC_ICE``); this module is only the peer connection
itself.

aiortc gathers its candidates synchronously inside
``setLocalDescription`` — there is no per-candidate event — so
the engine's candidates ride the answer SDP itself; they are
additionally dispatched to the candidate listener in wire shape
(for trickle-style consumers) once the answer is set. The
renderer's candidates arrive the other way, trickled one by one
through ``add_remote_candidate``.

Received audio is decoded by aiortc, resampled to PCM16 mono at
the session sample rate, and queued for ``frames()`` — the seam
the voice pipeline consumes. TTS audio does not ride this
connection: it returns over ``VOICE_OUTPUT_CHUNK`` events, the
channel the pipeline and the desktop player are built on.

aiortc (and PyAV, its media layer) are lazy imports, like every
voice stage: probed with ``find_spec``, loaded in ``open()``,
never at import or construction time. Without aiortc the
transport reports itself unavailable and session start fails
closed honestly — the behaviour this module replaced was a
permanent stub that reported unavailable unconditionally.
"""
import asyncio
import importlib.util
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from aether_engine.voice import VoiceUnavailableError

logger = logging.getLogger("aether_engine.voice.rtc")

#: A serialised ICE candidate in the wire shape the desktop uses:
#: ``{"candidate": "candidate:...", "sdpMid": str | None,
#: "sdpMLineIndex": int | None}`` — or None for end-of-candidates.
WireCandidate = dict | None

#: Listener for this side's own gathered candidates, in wire shape.
CandidateListener = Callable[[WireCandidate], Awaitable[None]]

# Live audio is worthless stale: the frame queue is bounded and
# drops the oldest frame on overflow (~5 s at 20 ms frames).
_FRAME_QUEUE_MAX = 250

_END = None  # queue sentinel: the frame stream is over


class WebRtcVoiceTransport:
    """SmallWebRTC peer transport between Electron and the engine.

    Lifecycle mirrors the pipeline's: construction opens nothing;
    ``open()`` creates the peer connection and returns the
    ``webrtc_config`` the desktop needs; negotiation
    (``handle_offer`` / ``add_remote_candidate``) happens after
    that, driven by the signalling handlers; ``close()`` tears
    everything down and ends ``frames()``. Idempotent close.
    """

    def __init__(
        self,
        *,
        ice_servers: list[dict] | None = None,
        sample_rate: int = 16000,
    ):
        # Browser-shaped server entries: {"urls": str | list[str],
        # "username"?, "credential"?}. Default: host candidates
        # only — the engine and the desktop sit on the same
        # machine/LAN in the settled deployment.
        self._ice_servers = list(ice_servers) if ice_servers else []
        self._sample_rate = sample_rate
        self._pc = None
        self._frames: asyncio.Queue = asyncio.Queue(maxsize=_FRAME_QUEUE_MAX)
        self._pump_task: asyncio.Task | None = None
        self._listener: CandidateListener | None = None
        self._pending_candidates: list[WireCandidate] = []
        self._closed = False

    # -- availability ------------------------------------------------

    def unavailable_reason(self) -> str | None:
        if importlib.util.find_spec("aiortc") is None:
            return "aiortc is not installed (SmallWebRTC transport)"
        return None

    @property
    def connection_state(self) -> str:
        """The peer connection's state ("new" before open,
        "closed" after close) — the engine-side truth the
        signalling layer can log and tests can assert on."""
        if self._pc is not None:
            return self._pc.connectionState
        return "closed" if self._closed else "new"

    # -- lifecycle ---------------------------------------------------

    async def open(self) -> dict | None:
        reason = self.unavailable_reason()
        if reason is not None:
            raise VoiceUnavailableError(reason)
        if self._pc is not None:
            return self._webrtc_config()
        from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection

        servers = [
            RTCIceServer(
                urls=entry["urls"],
                username=entry.get("username"),
                credential=entry.get("credential"),
            )
            for entry in self._ice_servers
        ]
        pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))
        pc.on("track", self._on_track)
        pc.on("connectionstatechange", self._on_connection_state_change)
        self._pc = pc
        return self._webrtc_config()

    def _webrtc_config(self) -> dict:
        return {"iceServers": [dict(entry) for entry in self._ice_servers]}

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None
        if self._pc is not None:
            pc, self._pc = self._pc, None
            try:
                await pc.close()
            except Exception:  # a broken peer must not strand the session
                logger.debug("voice RTC peer close failed", exc_info=True)
        self._end_stream()

    def _end_stream(self) -> None:
        """End ``frames()`` promptly: queued live audio is stale
        the moment the session dies, so it is discarded rather
        than drained into a pipeline that is shutting down."""
        while True:
            try:
                self._frames.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._frames.put_nowait(_END)

    # -- negotiation ---------------------------------------------------

    async def handle_offer(self, offer: dict) -> dict:
        """Apply the renderer's offer; return our answer in the
        same ``{"type", "sdp"}`` shape."""
        pc = self._require_pc("answer an RTC offer")
        if (
            not isinstance(offer, dict)
            or offer.get("type") != "offer"
            or not offer.get("sdp")
        ):
            raise VoiceUnavailableError(
                "voice unavailable: malformed RTC offer")
        from aiortc import RTCSessionDescription

        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=offer["sdp"], type=offer["type"]))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        local = pc.localDescription
        # Gathering completed inside setLocalDescription: surface
        # the embedded candidates to the listener in wire shape,
        # then the end-of-candidates marker.
        for wire in self._sdp_candidates(local.sdp):
            self._dispatch_candidate(wire)
        self._dispatch_candidate(None)
        return {"type": local.type, "sdp": local.sdp}

    @staticmethod
    def _sdp_candidates(sdp: str) -> list[WireCandidate]:
        """The a=candidate lines of an SDP, in wire shape, each
        tagged with its media section's mid and index."""
        from aiortc.sdp import candidate_from_sdp

        wires: list[WireCandidate] = []
        m_index = -1
        mid: str | None = None
        for line in sdp.splitlines():
            if line.startswith("m="):
                m_index += 1
                mid = None
            elif line.startswith("a=mid:"):
                mid = line[len("a=mid:"):]
            elif line.startswith("a=candidate:"):
                candidate = candidate_from_sdp(line[len("a="):])
                candidate.sdpMid = mid
                candidate.sdpMLineIndex = m_index
                wires.append(
                    WebRtcVoiceTransport._serialize_candidate(candidate))
        return wires

    async def add_remote_candidate(self, candidate: WireCandidate) -> None:
        """Trickle one of the renderer's ICE candidates in. None is
        the end-of-candidates marker: recorded, nothing to apply —
        aiortc needs no explicit signal for it."""
        self._require_pc("accept an ICE candidate")
        if candidate is None:
            return
        from aiortc.sdp import candidate_from_sdp

        try:
            parsed = candidate_from_sdp(candidate["candidate"])
        except Exception:  # noqa: BLE001 — any parse failure is a malformed candidate
            raise VoiceUnavailableError(
                "voice unavailable: malformed ICE candidate") from None
        parsed.sdpMid = candidate.get("sdpMid")
        parsed.sdpMLineIndex = candidate.get("sdpMLineIndex")
        await self._pc.addIceCandidate(parsed)

    def set_local_candidate_listener(
            self, listener: CandidateListener | None) -> None:
        """Register the push channel for our own gathered
        candidates (the signalling layer forwards them to the
        renderer). Candidates gathered before a listener registers
        are buffered and flushed on registration — aiortc starts
        gathering at setLocalDescription, which only negotiation
        triggers, but the buffer keeps the ordering guarantee
        independent of handler wiring order."""
        self._listener = listener
        if listener is not None and self._pending_candidates:
            pending, self._pending_candidates = self._pending_candidates, []
            for wire in pending:
                self._dispatch_candidate(wire)

    def _require_pc(self, action: str):
        if self._pc is None:
            raise VoiceUnavailableError(
                f"voice unavailable: SmallWebRTC transport is not "
                f"open (cannot {action})")
        return self._pc

    # -- aiortc event handlers -----------------------------------------

    def _on_track(self, track) -> None:
        if track.kind != "audio":
            logger.debug(f"voice RTC: ignoring {track.kind} track")
            return
        self._pump_task = asyncio.ensure_future(self._pump(track))

    def _on_connection_state_change(self) -> None:
        state = self.connection_state
        logger.debug(f"voice RTC connection state: {state}")
        if state in ("failed", "closed"):
            # The media path is gone: end the frame stream so the
            # pipeline's run loop finishes instead of idling on a
            # dead peer. The desktop learns the same state from its
            # own peer and surfaces it in the overlay.
            self._end_stream()

    @staticmethod
    def _serialize_candidate(candidate) -> WireCandidate:
        if candidate is None:
            return None
        from aiortc.sdp import candidate_to_sdp

        return {
            "candidate": candidate_to_sdp(candidate),
            "sdpMid": candidate.sdpMid,
            "sdpMLineIndex": candidate.sdpMLineIndex,
        }

    def _dispatch_candidate(self, wire: WireCandidate) -> None:
        if self._listener is None:
            self._pending_candidates.append(wire)
            return
        task = asyncio.ensure_future(self._deliver_candidate(wire))
        task.add_done_callback(self._candidate_delivery_done)

    async def _deliver_candidate(self, wire: WireCandidate) -> None:
        if self._listener is not None:
            await self._listener(wire)

    @staticmethod
    def _candidate_delivery_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.debug(f"voice RTC candidate push failed: {exc}")

    # -- media ---------------------------------------------------------

    async def _pump(self, track) -> None:
        """Decode the remote track into the frame queue: resample
        every received frame to PCM16 mono at the session rate.
        Ends (sentinel) when the track ends or errors — a finished
        stream, never a hang."""
        import av

        resampler = av.AudioResampler(
            format="s16", layout="mono", rate=self._sample_rate)
        try:
            while True:
                frame = await track.recv()
                for out in resampler.resample(frame):
                    self._push(out.to_ndarray().tobytes())
        except asyncio.CancelledError:
            raise
        except Exception:  # MediaStreamError et al: the track is over
            logger.debug("voice RTC track pump ended", exc_info=True)
        finally:
            try:
                for out in resampler.resample(None):  # flush the tail
                    self._push(out.to_ndarray().tobytes())
            except Exception:  # a dead resampler has nothing to flush
                logger.debug("voice RTC resampler flush failed", exc_info=True)
            self._push(_END)

    def _push(self, item) -> None:
        if self._frames.full():
            try:
                self._frames.get_nowait()  # drop the oldest live frame
            except asyncio.QueueEmpty:
                pass
        try:
            self._frames.put_nowait(item)
        except asyncio.QueueFull:  # a full queue of sentinels is fine
            pass

    async def frames(self) -> AsyncIterator[bytes]:
        """Raw PCM16 frames from the user's microphone, resampled
        to the session sample rate. Ends when the peer connection
        closes, fails, or the track ends."""
        if self._pc is None and not self._closed:
            raise VoiceUnavailableError(
                "voice unavailable: SmallWebRTC transport is not open")
        while True:
            item = await self._frames.get()
            if item is _END:
                return
            yield item
