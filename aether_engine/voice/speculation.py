"""S16: speculative READ-ONLY preparation on partial transcripts.

While the user is still speaking, the delivery half already
transcribes partials of the growing utterance (E7-07,
``delivery._run_partial``). Before S16 those partials fed only
the display and the Smart Turn endpointer; when the final
transcript landed, the turn paid the full cost of preparing its
memory context serially (audit R20 / G6). This module is the
speculative layer — and, inseparably, its guard (the plan text
requires both be built together: "no speculative lookup exists —
and therefore no guard restricting speculation to read-only work
exists either").

WHAT SPECULATION DOES: when the delivery half reports a STABLE
partial (see ``delivery._observe_partial_stability`` for the
signal), the preparer runs the ONE S7 briefing builder
(``fast_chat.fast_chat_memory_briefing`` — profile digest +
assembler-scored recall + lessons, the exact function a real
turn uses) on the partial text, in the background, and holds the
resulting string in memory. When the final transcript lands and
MATCHES (``speculation_match``), the voice talker hands the
prepared section to ``run_fast_chat``, which adopts it instead
of recomputing — one builder, no fork, no second scoring path.

THE GUARD (structural, not a runtime check on a general
executor): a ``SpeculativePreparer`` is constructed with exactly
ONE callable — the briefing builder — and nothing else. It holds
no talker, no emitter, no tool registry, no store handle, no
session callback. By construction it therefore cannot execute a
tool, launch a task, classify/escalate a turn, emit a
user-visible event, or commit anything to history: the only
effect it can have on the world is invoking that one read-only
builder and keeping the returned string. ``allowed_callables``
names the set so tests pin it exactly. A speculation is also
never *consumed* speculatively: it is adopted only by the real
turn, only after the final transcript exists and matches; only
the real turn's own path commits (Q28 discipline — the final
transcript is the only thing that ever commits).

BOUNDS: at most one speculation in flight per preparer (one
per voice session); a newer stable partial supersedes and
cancels the older; a hard time budget (``SPECULATION_BUDGET_S``)
bounds the builder call — a speculation that has not finished
inside it is dropped, never waited for (``take`` never blocks:
the turn proceeds without it, exactly as pre-S16). Kill metrics
are in-memory counters only (started / used / discarded, debug
-logged per transition) — no new store for an optimisation's
bookkeeping.

FAILURE ISOLATION: the builder's own contract is never-raises,
but the preparer swallows everything anyway (a failed or timed
-out speculation is simply absent), and ``take`` on a mismatch,
a barge-in or a session end discards silently. With speculation
off or unusable, turn behaviour is pre-S16 byte for byte.

WHY THE TOGGLE DEFAULTS OFF (``UserConfig.voice_speculation_enabled``,
evidence, not caution): the S7 builder's assembler leg prefers
the backend's HYBRID search (S6), and that read path is not
side-effect-free today — ``knowledge_graph.search_facts_semantic``
embeds the query through LiteLLM on every call and lazily
BACKFILLS fact embeddings (writes into ``fact_embeddings``)
before scanning, and the episodic leg's ``query_episodes``
backfills episode embeddings the same way. The default embedder
is local (``ollama/qwen3-embedding:0.6b``), but
``episodic.litellm_embedding_model`` passes any prefixed model
through — a cloud embedder is a supported configuration (Q17,
opt-in) — so a speculative call can fire a network embedding
request carrying text the user has not finished saying, and can
write derived rows, on partials that may never become a turn.
The guard's discipline for PARTIAL text is stricter than for a
committed turn, so speculation stays opt-in until the read path
grows a no-embed/no-backfill mode. When the owner enables it,
the exact S7 builder runs — the same calls the real turn would
make a moment later, made early.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger("aether_engine.voice.speculation")

# The one callable the guard allows: the S7 fast-chat memory
# briefing builder (prompt text -> briefing section, "" when
# nothing is recalled). See the module docstring.
PrepareBriefingFn = Callable[[str], Awaitable[str]]

# Hard lifetime of one speculation, seconds. The partial→final
# gap in a live session is a breath (endpointing silence + the
# final transcription); a preparation that has not landed well
# inside this bound is worthless — the final it was for has
# already been served without it (take never waits, so this
# bound only retires the background task itself).
SPECULATION_BUDGET_S = 20.0

# Match rule (speculation_match): the overlap coefficient
# |P ∩ F| / min(|P|, |F|) over the distinct word tokens of the
# speculated and final texts must reach this — 1.0 means one
# text's tokens are a subset of the other's (the common case:
# the final is the partial plus the tail of the sentence). 0.8
# tolerates STT revising a word or two between the partial and
# the final while still refusing a genuinely different sentence
# that merely shares its function words.
MATCH_OVERLAP_THRESHOLD = 0.8

# A partial shorter than this many distinct tokens is never
# speculated on: tiny utterances ("yes", "okay go") are both the
# least stable and the least worth preparing for — and with fewer
# tokens the overlap rule cannot tell a match from a coincidence,
# so below this floor only normalised equality matches at all.
MIN_SPECULATION_TOKENS = 3

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    """Distinct word tokens, the assembler's house convention:
    casefolded, length >= 2 (single letters carry no topical
    signal and would inflate overlap on short texts)."""
    return {
        token for token in _TOKEN_RE.findall((text or "").casefold())
        if len(token) >= 2
    }


def _normalised(text: str) -> str:
    """The token sequence, casefolded and joined — the equality
    leg of the match rule ("What's the weather?" vs "whats the
    weather" is the same sentence to STT: punctuation and case
    differ, the words don't)."""
    return " ".join(_TOKEN_RE.findall((text or "").casefold()))


def speculation_match(speculated: str, final: str) -> bool:
    """Whether a briefing prepared for ``speculated`` may serve
    the turn whose final transcript is ``final``.

    Normalised equality matches outright. Otherwise the token
    overlap coefficient must reach MATCH_OVERLAP_THRESHOLD, and
    only when both texts carry at least MIN_SPECULATION_TOKENS
    distinct tokens — below that floor, overlap is coincidence
    territory and only equality matches. An empty side never
    matches. Pure and total: any input yields a bool.
    """
    if not (speculated or "").strip() or not (final or "").strip():
        return False
    if _normalised(speculated) == _normalised(final):
        return True
    speculated_tokens = _tokens(speculated)
    final_tokens = _tokens(final)
    if min(len(speculated_tokens), len(final_tokens)) < MIN_SPECULATION_TOKENS:
        return False
    overlap = len(speculated_tokens & final_tokens) / min(
        len(speculated_tokens), len(final_tokens))
    return overlap >= MATCH_OVERLAP_THRESHOLD


class SpeculativePreparer:
    """The guarded speculation runner for one voice session.

    Constructed with the ONE allowed callable and nothing else
    (see the module docstring — the allowlist is the guard).
    ``enabled=False`` makes every entry point a no-op, so a
    session wired with a disabled preparer behaves exactly like
    a session with none.
    """

    #: The allowlist, by name — the complete set of callables a
    #: preparer can ever invoke. Tests pin this exactly.
    ALLOWLIST = ("prepare_briefing",)

    def __init__(
        self,
        *,
        prepare_briefing: PrepareBriefingFn,
        enabled: bool = True,
        budget_s: float = SPECULATION_BUDGET_S,
    ):
        self._callables: dict[str, PrepareBriefingFn] = {
            "prepare_briefing": prepare_briefing,
        }
        self._enabled = bool(enabled)
        self._budget_s = float(budget_s)
        # The single in-flight (or completed, awaiting its final)
        # speculation: {"text", "task", "started_at"} or None.
        self._pending: dict | None = None
        self._counters = {"started": 0, "used": 0, "discarded": 0}

    @property
    def allowed_callables(self) -> tuple[str, ...]:
        """The names of every callable this preparer holds —
        exactly ALLOWLIST, by construction."""
        return tuple(self._callables)

    @property
    def counters(self) -> dict[str, int]:
        """Kill metrics (in-memory only): speculations started,
        adopted by a real turn, and discarded (superseded,
        mismatched, expired, failed, or dropped at a boundary)."""
        return dict(self._counters)

    # -- starting --------------------------------------------------------

    def note_stable_partial(self, text: str) -> None:
        """A stable partial was observed: prepare for it.

        Synchronous and fire-and-forget (called from the partial
        transcription loop — it must never slow that loop). A
        partial for the text already in flight is ignored; a
        different text supersedes (the older speculation is
        cancelled and counted discarded).
        """
        if not self._enabled:
            return
        text = (text or "").strip()
        if len(_tokens(text)) < MIN_SPECULATION_TOKENS:
            return
        pending = self._pending
        if pending is not None and pending["text"] == text:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # speculation is async machinery; no loop, no start
            return
        if pending is not None:
            self._drop("superseded by a newer stable partial")
        task = loop.create_task(self._run(text))
        self._pending = {
            "text": text,
            "task": task,
            "started_at": time.monotonic(),
        }
        self._counters["started"] += 1
        logger.debug(
            "speculation started for %r (started=%d)",
            text, self._counters["started"])

    async def _run(self, text: str) -> str | None:
        """The background preparation: the ONE allowed callable,
        budget-bounded. Returns its briefing ("" is a legitimate
        result — the builder found nothing to recall), or None
        when the call failed or overran the budget: a failed
        speculation is simply absent, never an error."""
        try:
            briefing = await asyncio.wait_for(
                self._callables["prepare_briefing"](text),
                timeout=self._budget_s)
        except Exception:  # includes the budget's TimeoutError
            logger.debug(
                "speculative briefing preparation failed for %r",
                text, exc_info=True)
            return None
        return briefing if isinstance(briefing, str) else None

    # -- consumption -----------------------------------------------------

    def take(self, final_text: str) -> str | None:
        """Adopt the pending speculation for a real turn, or None.

        Never blocks: a speculation still running when its final
        lands is dropped, not awaited — the turn proceeds exactly
        as pre-S16. Single-use: the slot is cleared whatever the
        outcome, so a stale briefing can never serve a later turn.
        Returns the prepared briefing on a match (possibly "" —
        the builder legitimately recalled nothing), None on any
        discard.
        """
        pending = self._pending
        if pending is None:
            return None
        self._pending = None
        task = pending["task"]
        if not task.done():
            task.cancel()
            self._count_discard(
                "final landed before preparation finished", pending)
            return None
        briefing = None if task.cancelled() else task.result()
        if briefing is None:
            self._count_discard("preparation failed or timed out", pending)
            return None
        if time.monotonic() - pending["started_at"] > self._budget_s:
            self._count_discard("speculation expired", pending)
            return None
        if not speculation_match(pending["text"], final_text):
            self._count_discard("final transcript differs", pending)
            return None
        self._counters["used"] += 1
        logger.debug(
            "speculation adopted for %r (used=%d)",
            final_text, self._counters["used"])
        return briefing

    def discard(self, reason: str) -> None:
        """Drop any pending speculation without serving it —
        called at every boundary where the partial's context dies:
        a new utterance, a barge-in, an escalated turn, an empty
        final, session end. Silent by design (a debug log line
        and the counter are the whole footprint)."""
        if self._pending is not None:
            self._drop(reason)

    # -- internals ---------------------------------------------------------

    def _drop(self, reason: str) -> None:
        pending, self._pending = self._pending, None
        if pending is None:
            return
        if not pending["task"].done():
            pending["task"].cancel()
        self._count_discard(reason, pending)

    def _count_discard(self, reason: str, pending: dict) -> None:
        self._counters["discarded"] += 1
        logger.debug(
            "speculation discarded (%s) for %r (discarded=%d)",
            reason, pending["text"], self._counters["discarded"])
