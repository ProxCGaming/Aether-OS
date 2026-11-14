"""Voice transcription + delivery path: STT -> talker -> TTS.

One half of the voice cascade coordinator (split out of
``pipeline.py`` in the Wave 5 structure wave, along the seam the
Pass B craft verdict named: split the intake path from
transcription/delivery).

This module owns everything downstream of the microphone:
partial and final transcription, turn routing (the talker
answers chat; a turn the escalation classifier calls work
launches a background task and is answered by a spoken
acknowledgement), reply delivery (word-by-word text deltas,
each completed sentence synthesized and pushed as audio),
out-of-turn announcements, and the heard-words accounting that
decides what an interrupted turn commits — only the words
actually heard, at word granularity (N09/N10; see
``interrupt_output`` and ``note_playback_progress``).

Frame intake — transport frames, VAD feeding, endpointing,
barge-in detection — lives in ``aether_engine.voice.intake``.
The two paths meet only at the narrow surface ``VoicePipeline``
wires between them:

* delivery reads the intake half's live utterance buffer
  through snapshot callables (partials transcribe a snapshot
  while intake keeps appending) and feeds finished partials
  into the main endpointer (``observe_transcript``, S4-M16);
* delivery asks the intake half one question — whether the
  user is currently speaking — for the announcement quiet
  wait;
* the session's started/stopping flags stay with
  ``VoicePipeline`` and are read through callables, so every
  loop here sees the live values.

Because partial transcription runs as this half's own task off
a snapshot, a slow STT can delay partials but can never stall
frame intake (E7-07 — the structural fix, formerly an inline
await in the frame loop).
"""
import asyncio
import base64
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from aether_common.contracts import Event, EventType
from aether_engine.voice.degradation import PROFILE_UNAVAILABLE, SessionDegradation
from aether_engine.voice.stt import SttEngine
from aether_engine.voice.tts import TtsEngine

logger = logging.getLogger("aether_engine.voice.delivery")

Talker = Callable[[str], AsyncIterator[str]]
EmitFn = Callable[[Event], Awaitable[None]]
TurnCallback = Callable[[str], Awaitable[None]]
EscalationClassifier = Callable[[str], Awaitable[bool]]
SttSwapFn = Callable[[str], Awaitable[bool]]
FatalFn = Callable[[str], Awaitable[None]]

_SENTENCE_BOUNDARY = re.compile(r"[.!?…]+[\"'”’)\]]*\s+")
_WORD = re.compile(r"\S+")


def _split_sentences(buffer: str) -> tuple[list[str], str]:
    """Split off sentences whose boundary is proven (terminator +
    following whitespace); the tail stays buffered for more deltas."""
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_BOUNDARY.finditer(buffer):
        sentences.append(buffer[start:match.end()])
        start = match.end()
    return sentences, buffer[start:]


def _heard_prefix(sentence: str, emitted: int, total: int) -> str:
    """The heard part of a sentence whose audio was interrupted:
    the chunk-proportional prefix of its words (E8-5.1b word
    granularity for the only-actually-heard commit rule)."""
    if total <= 0 or emitted <= 0:
        return ""
    if emitted >= total:
        return sentence
    words = list(_WORD.finditer(sentence))
    if not words:
        return sentence
    heard_words = (len(words) * emitted) // total
    if heard_words <= 0:
        return ""
    if heard_words >= len(words):
        return sentence
    end = words[heard_words - 1].end()
    if end < len(sentence) and sentence[end] == " ":
        end += 1
    return sentence[:end]


def _word_deltas(text: str) -> list[str]:
    """Split text into word deltas, each carrying its trailing
    whitespace, so the deltas rejoin into the exact original."""
    deltas: list[str] = []
    pos = 0
    for match in _WORD.finditer(text):
        end = match.end()
        while end < len(text) and text[end] == " ":
            end += 1
        deltas.append(text[pos:end])
        pos = end
    if pos < len(text):
        deltas.append(text[pos:])
    return deltas


async def _static_deltas(text: str) -> AsyncIterator[str]:
    """Word-sized deltas for text that already exists whole (an
    escalation acknowledgement, an announcement): the transcript
    still renders it word by word, as Q28 requires."""
    for delta in _word_deltas(text):
        yield delta


class VoiceDelivery:
    """The transcription/delivery half of one voice session.

    Constructed by ``VoicePipeline`` with the session's stages,
    callbacks and the intake half's buffer callables; the
    pipeline owns lifecycle (start/stop) and the started /
    stopping flags, read here through callables.
    """

    def __init__(
        self,
        *,
        session_id: str,
        stt: SttEngine,
        tts: TtsEngine,
        talker: Talker,
        emit: EmitFn,
        on_user_final: TurnCallback | None = None,
        on_assistant_final: TurnCallback | None = None,
        on_turn_error: TurnCallback | None = None,
        tts_voice: str,
        sample_rate: int = 16000,
        escalation_classifier: EscalationClassifier | None = None,
        on_escalation: TurnCallback | None = None,
        escalation_ack: str,
        utterance_snapshot: Callable[[], bytes],
        utterance_extends: Callable[[bytes], bool],
        has_utterance: Callable[[], bool],
        observe_transcript: Callable[[str], None],
        user_speaking: Callable[[], bool],
        is_started: Callable[[], bool],
        is_stopping: Callable[[], bool],
        degradation: SessionDegradation | None = None,
        swap_stt: SttSwapFn | None = None,
        on_fatal: FatalFn | None = None,
    ):
        self.session_id = session_id
        self._stt = stt
        self._tts = tts
        # S15: the session's degradation tracker + the
        # pipeline's execution seams for its decisions (swap
        # the STT engine, end the session). All three are None
        # for a pipeline built without a ladder plan — the
        # historical behaviour then applies unchanged (a failed
        # transcription drops its turn, a failed synthesis
        # degrades its sentence to text, nothing is measured).
        self._degradation = degradation
        self._swap_stt = swap_stt
        self._on_fatal = on_fatal
        self._talker = talker
        self._emit = emit
        self._on_user_final = on_user_final
        self._on_assistant_final = on_assistant_final
        self._on_turn_error = on_turn_error
        self._tts_voice = tts_voice
        self._sample_rate = sample_rate
        # E8-5.1b escalation seams: both must be wired for a turn to
        # escalate; either missing keeps every turn with the talker
        # (the 5.1a behaviour).
        self._escalation_classifier = escalation_classifier
        self._on_escalation = on_escalation
        self._escalation_ack = escalation_ack
        # The intake half's side of the seam (wired by
        # VoicePipeline): live utterance buffer reads, the main
        # endpointer's transcript observation, the user-speaking
        # question, and the session flags.
        self._utterance_snapshot = utterance_snapshot
        self._utterance_extends = utterance_extends
        self._has_utterance = has_utterance
        self._observe_transcript = observe_transcript
        self._user_speaking = user_speaking
        self._is_started = is_started
        self._is_stopping = is_stopping

        self._last_partial_text = ""
        self._turn_active = False
        self._turn_task: asyncio.Task | None = None
        # E7-07: partial transcription runs as its own task off
        # the frame loop (see request_partial); _partial_queued is
        # the coalescing flag for requests that arrive while one
        # is already in flight.
        self._partial_task: asyncio.Task | None = None
        self._partial_queued = False
        self._announcing = False
        # N10: the identity of the announcement currently speaking
        # (None for reply turns), so a barge-in landing mid-
        # announcement can tag its event with it.
        self._announcing_id: str | None = None
        self._interrupt_requested = False
        # Heard-words tracking (E8-5.1b, N09): _heard_entries holds
        # one record per sentence whose audio the current output
        # activity has emitted — its text, its first chunk_seq, its
        # chunk total and how many chunks were emitted — in
        # emission order; _heard_text_now() derives the heard text
        # from these records (playback reports when the renderer
        # sends them, emission counts when it never does).
        self._heard_entries: list[dict] = []
        # N09 playback accounting: session-scoped, so both survive
        # _reset_heard() — chunk seqs are never reused, and a late
        # report for a previous activity's chunks can only name
        # seqs below the next activity's first chunk. _played_seq
        # is the highest chunk_seq the renderer confirmed FINISHED
        # playing; _progress_seen flips on the first well-formed
        # report (including the -1 "reporting live" one).
        self._next_chunk_seq = 0
        self._played_seq = -1
        self._progress_seen = False
        # The heard text published at a barge-in cut, frozen so the
        # history commit — which lands when the output loop notices
        # the interrupt — is exactly the published text.
        self._frozen_heard: str | None = None

    @property
    def output_active(self) -> bool:
        """A reply turn or an announcement is currently speaking."""
        return self._turn_active or self._announcing

    def _reset_heard(self) -> None:
        self._heard_entries = []
        self._frozen_heard = None

    def request_interrupt(self) -> None:
        """Ask the in-flight output to stop at the next chunk
        boundary (session teardown; VoicePipeline.stop)."""
        self._interrupt_requested = True

    def cancel_pending(self) -> None:
        """Cancel the in-flight turn and partial tasks.

        Called by VoicePipeline when the frame loop ends: turns
        run as their own tasks (the frame loop must stay free for
        barge-in detection), so one must never be orphaned; the
        partial task works off the intake half's utterance
        buffer, so it must not outlive the loop either (E7-07).
        """
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
        if self._partial_task is not None and not self._partial_task.done():
            self._partial_task.cancel()
        self._turn_active = False

    # -- degradation seams (S15) -----------------------------------------

    def replace_stt(self, engine: SttEngine) -> None:
        """Swap the STT engine mid-session (the pipeline's
        step-down execution: the new engine is already open
        when this lands, so no transcription waits on a load).
        A partial transcription in flight on the old engine is
        provisional by contract — its failure or staleness is
        already tolerated by ``_run_partial``."""
        self._stt = engine

    async def _emit_session_status(
        self, *, profile: str, notice: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Push the session's degradation state (VOICE_SESSION_STATUS):
        the current profile plus, on a transition, the notice or
        the ending reason. Fire-and-forget like every voice push."""
        payload: dict = {"session_id": self.session_id, "profile": profile}
        if notice is not None:
            payload["notice"] = notice
        if reason is not None:
            payload["reason"] = reason
        await self._emit(Event(
            type=EventType.VOICE_SESSION_STATUS, payload=payload))

    async def _try_swap_stt(self, model: str) -> bool:
        """Execute a tracker-decided STT swap via the pipeline's
        seam. False when no seam is wired (an injected STT the
        factory cannot rebuild) or the new engine failed to
        load — the caller decides what False means (a latency
        step-down just doesn't happen; a failure swap falls
        through to ending the session)."""
        if self._swap_stt is None:
            return False
        try:
            return bool(await self._swap_stt(model))
        except Exception:  # a swap failure is a decision input, not a crash
            logger.warning("voice STT swap failed", exc_info=True)
            return False

    async def _handle_stt_success(self, duration_ms: float) -> None:
        """A final transcription landed: feed the tracker; on
        its one-shot step-down decision, swap engines and
        announce. A swap that cannot execute leaves the session
        on the current model — still working, just slow — and
        the latch means it is not retried every turn."""
        if self._degradation is None:
            return
        step = self._degradation.note_stt_success(duration_ms)
        if step is None:
            return
        notice, new_model = step
        if await self._try_swap_stt(new_model):
            await self._emit_session_status(
                profile=self._degradation.profile, notice=notice)

    async def _handle_stt_failure(self, error: Exception) -> None:
        """A final transcription failed. Below the tracker's
        consecutive-failure limit the turn is simply dropped
        (the historical behaviour). At the limit the ladder
        acts: one swap to a smaller model (announced), or —
        no smaller model, swap already used, or the swap
        itself cannot load — the session ends with the
        specific stage + error as its reason, never a generic
        failure."""
        if self._degradation is None:
            return
        failing_model = self._degradation.stt_model
        action, payload = self._degradation.note_stt_failure(str(error))
        if action == "drop":
            return
        if action == "swap":
            new_model = payload or ""
            if await self._try_swap_stt(new_model):
                await self._emit_session_status(
                    profile=self._degradation.profile,
                    notice=(
                        "Transcription kept failing with "
                        f"'{failing_model}', so I've switched to "
                        f"'{new_model}' — it may be a little less "
                        "accurate."))
                return
            reason = (
                "voice unavailable: speech-to-text failed "
                f"repeatedly with '{failing_model}' (last error: "
                f"{error}) and the fallback model '{new_model}' "
                "could not be loaded — the session has ended.")
        else:  # "end"
            reason = payload or f"voice unavailable: speech-to-text failed ({error})"
        self._degradation.profile = PROFILE_UNAVAILABLE
        await self._emit_session_status(
            profile=PROFILE_UNAVAILABLE, reason=reason)
        if self._on_fatal is not None:
            await self._on_fatal(reason)

    async def _handle_tts_failure(self) -> None:
        """A synthesis failed mid-session: the tracker flips
        the session to text-only (later sentences skip the TTS
        stage entirely instead of failing one by one) and the
        transition is announced exactly once."""
        if self._degradation is None:
            return
        notice = self._degradation.note_tts_failure()
        if notice is not None:
            await self._emit_session_status(
                profile=self._degradation.profile, notice=notice)

    def _timed_talker(self, text: str) -> AsyncIterator[str]:
        """The talker stream with its time-to-first-delta
        recorded as a stage measurement (S15). A pass-through
        when no tracker is wired."""
        talker = self._talker
        degradation = self._degradation

        async def _gen() -> AsyncIterator[str]:
            started = time.perf_counter()
            first = True
            async for delta in talker(text):
                if first:
                    first = False
                    if degradation is not None:
                        degradation.record_stage_sample(
                            "talker", "",
                            (time.perf_counter() - started) * 1000.0)
                yield delta

        return _gen()

    # -- playback accounting (N09) --------------------------------------

    def note_playback_progress(self, played_seq: int) -> None:
        """Record a renderer playback report (N09): the highest
        chunk_seq the desktop has FINISHED playing, or -1 for
        "reporting live, nothing finished yet". Fire-and-forget —
        the websocket layer calls this per inbound
        VOICE_PLAYBACK_PROGRESS event; malformed values are
        ignored, never fatal, and reports only ever move the
        position forward. A report arriving after a cut was
        published for the current activity does not move that
        activity's accounting (the cut is final); it still arms
        playback mode for what follows."""
        try:
            seq = int(played_seq)
        except (TypeError, ValueError):
            return
        self._progress_seen = True
        if self._frozen_heard is not None:
            return
        self._played_seq = max(self._played_seq, seq)

    def _played_count(self, entry: dict) -> int:
        """How many of one emitted sentence's chunks the renderer
        has confirmed played. A sentence's chunk seqs are
        contiguous, assigned in emission order."""
        played = self._played_seq - entry["first_seq"] + 1
        return max(0, min(entry["total"], played))

    def _heard_text_now(self) -> str:
        """Everything the listener has actually heard of the
        current output activity.

        Once the renderer has reported any playback progress this
        session, only PLAYED chunks count (N09): fully-played
        sentences in full, the first partially played one as its
        chunk-proportional word prefix, and nothing from later
        sentences however much of them was emitted. When no
        progress has ever been reported (a client without playback
        reporting), the same walk runs on EMITTED counts — the
        pre-N09 estimate the older suites pin."""
        if self._frozen_heard is not None:
            return self._frozen_heard
        parts: list[str] = []
        for entry in self._heard_entries:
            total = entry["total"]
            if self._progress_seen:
                available = self._played_count(entry)
            else:
                available = min(entry["emitted"], total)
            if available >= total:
                parts.append(entry["sentence"])
                continue
            if available > 0:
                parts.append(_heard_prefix(
                    entry["sentence"], available, total))
            break
        return "".join(parts)

    # -- barge-in response ------------------------------------------------

    async def _await_output_settle(self) -> None:
        """Bounded wait for the in-flight output activity (a reply
        turn or an announcement) to wind down once the interrupt
        flag is set."""
        for _ in range(2000):
            if not self.output_active:
                break
            await asyncio.sleep(0.002)

    async def interrupt_output(self) -> dict:
        """Stop the in-flight output (a reply turn or an
        announcement); report what was actually heard.

        Returns the VOICE_BARGE_IN payload fields:
        ``{"stopped_at_char", "committed_text"}``. The committed
        text — only the words actually heard, at word granularity —
        is also what ``on_assistant_final`` commits for the turn:
        the snapshot published here is frozen, and the commit reads
        exactly it.

        Timing (N09): when the renderer reports playback progress,
        the accounting is independent of the output loop, so the
        cut is published IMMEDIATELY — the pre-N09 code first polled
        up to ~4 s for the output task to settle, while the desktop
        kept playing the unheard tail. The settle wait still runs
        afterwards in that mode, but only as sequencing for the
        caller (the barge-in path replays the interrupting frames
        once the old output has wound down). With emission-based
        accounting (no progress ever reported) the heard text is
        only final once the output loop has noticed the interrupt,
        so the settle still precedes the snapshot, as it did
        pre-N09.

        When the interrupted output is an announcement, the event
        also carries its ``announcement_id`` (N10): the heard prefix
        of an announcement is NOT a new message — the announced
        task's own message already stands in chat — and the tag is
        how the desktop knows not to commit it as one.
        """
        announcement_id = self._announcing_id if self._announcing else None
        if self.output_active:
            self._interrupt_requested = True
            if not self._progress_seen:
                await self._await_output_settle()
            heard = self._heard_text_now()
            self._frozen_heard = heard
            payload = {
                "session_id": self.session_id,
                "stopped_at_char": len(heard),
                "committed_text": heard,
            }
            if announcement_id is not None:
                payload["announcement_id"] = announcement_id
            await self._emit(Event(
                type=EventType.VOICE_BARGE_IN,
                payload=payload,
            ))
            if self._progress_seen:
                await self._await_output_settle()
        heard = self._heard_text_now()
        return {
            "stopped_at_char": len(heard),
            "committed_text": heard,
        }

    # -- partial transcription (E7-07) ------------------------------------

    def begin_utterance(self) -> None:
        """A new utterance started on the intake path: the
        partial machinery's per-utterance text state resets."""
        self._last_partial_text = ""

    def request_partial(self) -> None:
        """Schedule a provisional transcription of the growing
        utterance WITHOUT blocking frame intake (E7-07).

        The intake loop only schedules; the transcription itself
        runs in ``_run_partial``. While one is in flight, further
        requests coalesce into a single re-run over the freshest
        buffer. A slow STT (seconds on CPU) can therefore delay
        partials but never stall intake — on the old inline shape
        the bounded RTC frame queue overflowed during every slow
        partial and silently dropped the user's speech."""
        if not self._has_utterance():
            return
        if self._partial_task is not None and not self._partial_task.done():
            self._partial_queued = True
            return
        self._partial_task = asyncio.ensure_future(self._run_partial())

    async def _run_partial(self) -> None:
        try:
            while True:
                self._partial_queued = False
                snapshot = self._utterance_snapshot()
                if not snapshot:
                    return
                try:
                    text = await self._stt.transcribe(
                        snapshot, sample_rate=self._sample_rate)
                except Exception:  # a partial is provisional: never kill the loop
                    logger.debug(
                        "voice partial transcription failed", exc_info=True)
                    return
                text = (text or "").strip()
                # The utterance this snapshot belonged to may have
                # ended (or a new one begun) while transcription
                # ran: publish only when the live buffer still
                # extends the snapshot, so a stale partial never
                # resurrects a finished turn's text.
                if (
                    text
                    and text != self._last_partial_text
                    and self._utterance_extends(snapshot)
                ):
                    self._last_partial_text = text
                    # S4-M16 (Smart Turn): feed the partial into the main
                    # endpointer so its turn-completion policy can bend the
                    # trailing-silence window for this utterance. The
                    # barge-in endpointer is intentionally NOT fed — it
                    # never observes transcripts and stays silence-only.
                    self._observe_transcript(text)
                    await self._emit(Event(
                        type=EventType.VOICE_PARTIAL_TRANSCRIPT,
                        payload={
                            "session_id": self.session_id,
                            "speaker": "user",
                            "text": text,
                            "is_final": False,
                        },
                    ))
                if not self._partial_queued:
                    return
        finally:
            if self._partial_task is asyncio.current_task():
                self._partial_task = None

    # -- final transcription + turn routing --------------------------------

    async def finish_utterance(self, audio: bytes) -> None:
        """Transcribe a finished utterance (its audio, handed over
        by the intake half at the endpoint) and route the turn.

        S15: the final transcription is the measured one — its
        duration feeds the degradation tracker's latency window
        and per-model store (partials are provisional and scale
        with buffer growth, so they are never measured), and a
        failure feeds the consecutive-failure ladder instead of
        always just dropping the turn."""
        self._last_partial_text = ""
        transcribe_started = time.perf_counter()
        try:
            text = await self._stt.transcribe(audio, sample_rate=self._sample_rate)
        except Exception as e:  # noqa: BLE001 — the ladder decides: drop, swap, or end
            logger.warning(f"voice transcription failed: {e}")
            await self._handle_stt_failure(e)
            return
        await self._handle_stt_success(
            (time.perf_counter() - transcribe_started) * 1000.0)
        text = (text or "").strip()
        if not text:
            return
        await self._emit(Event(
            type=EventType.VOICE_PARTIAL_TRANSCRIPT,
            payload={
                "session_id": self.session_id,
                "speaker": "user",
                "text": text,
                "is_final": True,
            },
        ))
        if self._on_user_final is not None:
            await self._on_user_final(text)
        # Route the turn (E8-5.1b). Turn state initialises HERE,
        # synchronously, before the turn task is scheduled: the
        # frame loop keeps running (full-duplex), and a barge-in
        # detected before the task's first slice must not be lost
        # to a reset inside the task.
        self._turn_active = True
        self._interrupt_requested = False
        self._reset_heard()
        if await self._should_escalate(text):
            self._turn_task = asyncio.ensure_future(
                self._run_escalation_turn(text))
        else:
            self._turn_task = asyncio.ensure_future(self._run_turn(text))

    async def _should_escalate(self, text: str) -> bool:
        """Ask the wired classifier whether this turn is work.
        Both seams must be present, and a classifier failure
        degrades to the talker — the turn is never lost to a
        routing error."""
        if self._escalation_classifier is None or self._on_escalation is None:
            return False
        try:
            return bool(await self._escalation_classifier(text))
        except Exception:  # degrade to chat, never drop the turn
            logger.warning(
                "voice escalation classification failed; "
                "keeping the turn with the talker", exc_info=True)
            return False

    async def _run_turn(self, text: str) -> None:
        """One talker turn. Turn state was initialised by
        ``finish_utterance`` before this task was scheduled."""
        try:
            await self._deliver_reply(self._timed_talker(text))
        except Exception as e:  # noqa: BLE001 — the talker failing must not kill the session
            logger.warning(f"voice turn failed: {e}")
            if self._on_turn_error is not None:
                await self._on_turn_error(str(e))
        finally:
            self._turn_active = False

    async def _run_escalation_turn(self, text: str) -> None:
        """Escalation turn (E8-5.1b): the job launches FIRST —
        the background task lane is a separate execution and never
        waits on this lane — then the spoken acknowledgement masks
        its latency as this turn's reply. If the launch itself
        fails, the talker answers instead: the user is never left
        with silence where an answer was possible."""
        try:
            launched = False
            try:
                await self._on_escalation(text)
                launched = True
            except Exception:  # fall back, never strand the turn
                logger.warning(
                    "voice escalation launch failed; "
                    "falling back to the talker", exc_info=True)
            if launched:
                await self._deliver_reply(_static_deltas(self._escalation_ack))
            else:
                await self._deliver_reply(self._timed_talker(text))
        except Exception as e:  # noqa: BLE001 — a failed turn must not kill the session
            logger.warning(f"voice escalation turn failed: {e}")
            if self._on_turn_error is not None:
                await self._on_turn_error(str(e))
        finally:
            self._turn_active = False

    # -- announcements ------------------------------------------------------

    async def speak_announcement(
        self, text: str, *, announcement_id: str | None = None,
        kind: str | None = None, message_id: str | None = None,
        quiet_timeout_s: float,
    ) -> bool:
        """Speak out-of-turn text — a finished background task's
        summary (E8-5.1b). Waits for a quiet moment (no reply
        playing, the user not mid-utterance), then speaks with the
        same word-by-word streaming and barge-in rules as a reply.
        Never commits to history: the task machinery already
        persisted the summary as the task's own message. Returns
        False when the session is not live — the summary still
        stands in chat either way — and likewise when no quiet
        moment arrives within ``quiet_timeout_s`` (E7-06): the wait
        is a courtesy, never an open-ended hold on the caller's
        task. (VoicePipeline passes its module's
        ``_ANNOUNCEMENT_QUIET_TIMEOUT_S`` — the value tests patch.)

        N10: ``announcement_id``/``kind`` tag every emitted chunk
        as this announcement (never as a talker reply), and
        ``message_id`` — the announced message's own id in the
        session store, when there is one — rides along, so the
        desktop can match the spoken text to the message already
        standing in chat instead of rendering it a second time."""
        text = (text or "").strip()
        if not text or not self._is_started() or self._is_stopping():
            return False
        quiet_deadline = time.monotonic() + quiet_timeout_s
        while not self._is_stopping() and (
            self.output_active
            or self._user_speaking()
        ):
            if time.monotonic() >= quiet_deadline:
                return False
            await asyncio.sleep(0.005)
        if self._is_stopping() or not self._is_started():
            return False
        self._announcing = True
        self._announcing_id = announcement_id
        self._interrupt_requested = False
        self._reset_heard()
        try:
            await self._deliver_reply(
                _static_deltas(text), commit=False,
                announcement_id=announcement_id, announcement_kind=kind,
                message_id=message_id)
        except Exception:  # a failed announcement must not kill the session
            logger.warning("voice announcement failed", exc_info=True)
        finally:
            self._announcing = False
            self._announcing_id = None
        return True

    # -- reply delivery -----------------------------------------------------

    async def _deliver_reply(
        self, deltas: AsyncIterator[str], *, commit: bool = True,
        announcement_id: str | None = None,
        announcement_kind: str | None = None,
        message_id: str | None = None,
    ) -> None:
        """Stream text deltas word by word and speak each completed
        sentence. On interruption the committed text is the heard
        text (word granularity); otherwise it is the full reply.
        ``commit=False`` (announcements) streams and speaks without
        touching history; the announcement identity, when given,
        tags every chunk this delivery emits."""
        parts: list[str] = []
        sentence_buffer = ""
        interrupted = False
        async for delta in deltas:
            if self._interrupt_requested or self._is_stopping():
                interrupted = True
                break
            if not delta:
                continue
            parts.append(delta)
            await self._emit_chunk(
                text_delta=delta, audio_chunk=None,
                announcement_id=announcement_id,
                announcement_kind=announcement_kind, message_id=message_id)
            sentence_buffer += delta
            sentences, sentence_buffer = _split_sentences(sentence_buffer)
            for sentence in sentences:
                await self._speak(
                    sentence, announcement_id=announcement_id,
                    announcement_kind=announcement_kind,
                    message_id=message_id)
                if self._interrupt_requested or self._is_stopping():
                    interrupted = True
                    break
            if interrupted:
                break
        if not interrupted and sentence_buffer.strip():
            await self._speak(
                sentence_buffer, announcement_id=announcement_id,
                announcement_kind=announcement_kind, message_id=message_id)
            if self._interrupt_requested or self._is_stopping():
                interrupted = True
        if not commit:
            return
        committed = self._heard_text_now() if interrupted else "".join(parts)
        if committed.strip() and self._on_assistant_final is not None:
            await self._on_assistant_final(committed.strip())

    async def _speak(
        self, sentence: str, *, announcement_id: str | None = None,
        announcement_kind: str | None = None,
        message_id: str | None = None,
    ) -> None:
        """Synthesize one sentence and push its audio chunks.

        The sentence's audio is fully synthesized BEFORE emission
        so the chunk total is known. Emission registers the
        sentence in the heard accounting together with its chunks'
        seq range (N09); how much of it counts as heard is decided
        at read time — played chunks when the renderer reports
        playback progress, emitted chunks when it never does — so
        an interrupt landing mid-sentence needs no fold-in here.
        A TTS failure degrades the turn to text-only, never fatal.
        A silent synthesis (zero chunks) registers nothing: no
        audio means nothing was heard, so the sentence can never
        be credited on the interrupted path (it still stands in
        the full commit of an uninterrupted reply, which
        ``_deliver_reply`` builds from the reply text itself).

        S15: when the session's ladder has speech output off —
        the plan's text-only rung, or a mid-session TTS failure
        that flipped the session text-only — the stage is not
        called at all; the sentence's text has already streamed.
        While speech is on, the time to the first audio chunk
        is recorded as the TTS stage measurement (the bench's
        "TTS first-audio" figure, live), and a synthesis failure
        is reported to the tracker, which announces the
        text-only transition once and stops further attempts.
        """
        if self._degradation is not None and not self._degradation.tts_enabled:
            return
        chunks: list[bytes] = []
        synth_started = time.perf_counter()
        first_chunk_ms: float | None = None
        try:
            async for audio in self._tts.synthesize(sentence, voice=self._tts_voice):
                if self._interrupt_requested or self._is_stopping():
                    return  # nothing of this sentence was emitted
                if first_chunk_ms is None:
                    first_chunk_ms = (
                        time.perf_counter() - synth_started) * 1000.0
                chunks.append(audio)
        except Exception:  # the mouth failing must not kill the conversation
            logger.warning("voice TTS failed; continuing text-only", exc_info=True)
            await self._handle_tts_failure()
            return
        if first_chunk_ms is not None and self._degradation is not None:
            self._degradation.record_stage_sample(
                "tts", self._tts_voice, first_chunk_ms)
        if not chunks:
            return
        entry = {
            "sentence": sentence,
            "first_seq": self._next_chunk_seq,
            "total": len(chunks),
            "emitted": 0,
        }
        self._next_chunk_seq += len(chunks)
        self._heard_entries.append(entry)
        seq = entry["first_seq"]
        for audio in chunks:
            if self._interrupt_requested or self._is_stopping():
                return
            await self._emit_chunk(
                text_delta="",
                audio_chunk=base64.b64encode(audio).decode("ascii"),
                announcement_id=announcement_id,
                announcement_kind=announcement_kind, message_id=message_id,
                chunk_seq=seq,
            )
            entry["emitted"] += 1
            seq += 1

    async def _emit_chunk(
        self, *, text_delta: str, audio_chunk: str | None,
        announcement_id: str | None = None,
        announcement_kind: str | None = None,
        message_id: str | None = None,
        chunk_seq: int | None = None,
    ) -> None:
        payload: dict = {
            "session_id": self.session_id,
            "speaker": "assistant",
            "text_delta": text_delta,
            "audio_chunk": audio_chunk,
            "timestamp": time.time(),
        }
        if chunk_seq is not None:
            # N09: the audio chunk's session-monotonic identity.
            # The renderer reports finished seqs back as playback
            # progress — the input the heard accounting reads.
            payload["chunk_seq"] = chunk_seq
        if announcement_id is not None:
            # N10: this chunk is an announcement, not a reply turn.
            # The identity is stable per announced event, so the
            # desktop can route it away from the committable reply
            # stream; message_id, when present, is the announced
            # message's own id in the session store.
            payload["announcement_id"] = announcement_id
            if announcement_kind is not None:
                payload["announcement_kind"] = announcement_kind
            if message_id is not None:
                payload["message_id"] = message_id
        await self._emit(Event(
            type=EventType.VOICE_OUTPUT_CHUNK,
            payload=payload,
        ))
