"""EQ: conversation-signal detection + response adaptation (S11).

Master-build plan §4 S11 (R5): emotional intelligence did not
exist at all — no detector, no prompt language, no toggle. This
module is the detector + briefing half. It is deliberately
small, local and deterministic: regex/heuristic signals over
the recent conversation, no LLM call, no new dependency, no
external service (latency and privacy both forbid a model call
on the chat path just to guess at tone).

WHAT THIS IS FOR — adaptation, never labelling. The read exists
so the assistant can ADAPT: slow down and simplify when the
conversation is going badly, keep answers short when the user
is in a hurry, explain one step at a time when the user is
lost. The read is NEVER surfaced to the user as a diagnosis
("you seem frustrated"), the assistant never claims to know or
share feelings, and the briefing text carries behaviour
guidance only — not the state label, not the evidence. The
evidence is kept on the read for tests and debugging, so every
adaptation is explainable in code.

TRANSIENT BY CONSTRUCTION. A read is computed per turn/task
from caller-supplied conversation data and held only in run or
session state. It is NEVER written to long-term memory: this
module imports nothing from the memory layer (no facade, no KG,
no episodic store), so there is no code path by which a mood
could become a stored fact, an episode tag or a profile entry.
Nothing about how a user felt is remembered after the
conversation moves on.

CONSERVATIVE BY DESIGN. Signals are weighted; a state requires
real accumulated weight, and the briefing additionally requires
at least medium confidence. One weak signal (a short reply, a
single "no") changes nothing. Uncertain reads resolve to
neutral — the pre-S11 behaviour. Tiredness is deliberately NOT
a state: no honest text signal distinguishes a tired user from
a busy one, so this module does not claim to detect it. The
state set is neutral / engaged / confused / frustrated /
rushed, and "rushed" also covers the plan's "tired-or-rushed"
slot via its observable half (hurry), never via a guess about
fatigue.

VOICE. Voice turns are transcripts by the time any consumer
sees them, so the text signals apply unchanged (voice rides
fast chat, which wires this module exactly as text chat does).
The one voice-native input — the Endpointer's per-turn
``TurnContext`` (speech/silence durations, ``voice/vad.py``) —
is accepted behind this same interface as weak supporting
evidence (long pauses before finishing a turn can mean the
user is thinking or lost). TTS adaptation does NOT exist: the
current TTS (``voice/tts.py``) synthesises from text + voice id
only, with no rate/pitch/SSML control, so there is nothing
honest to adapt. ``VoicePipeline.note_affect_read`` records the
latest read on the session for a future TTS that grows prosody
controls; it applies nothing today.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# House briefing discipline (learned_briefing / calendar_briefing
# pattern): bounded, empty when there is nothing to say.
EQ_BRIEFING_MAX_CHARS = 600

# How many of the most recent user messages the detector reads.
# Signals are about how the conversation is going NOW; older
# turns say little about the current reply.
DEFAULT_WINDOW = 6

STATE_NEUTRAL = "neutral"
STATE_ENGAGED = "engaged"
STATE_CONFUSED = "confused"
STATE_FRUSTRATED = "frustrated"
STATE_RUSHED = "rushed"

CONFIDENCE_LOW = "low"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_HIGH = "high"

# Signal weights. A state needs >= 2 points to be read at all
# (one medium signal), >= 3 for the medium confidence that makes
# a briefing appear, >= 5 for high. Weak signals (1) only ever
# support — they can never name a state alone.
_WEIGHT_WEAK = 1
_WEIGHT_MEDIUM = 2
_WEIGHT_STRONG = 3


@dataclass(frozen=True)
class SignalHit:
    """One fired signal: its name, its weight toward its state,
    and the human-readable evidence (which message, what matched).
    Evidence is for tests/debugging — it is never rendered into a
    prompt or shown to the user."""

    name: str
    weight: int
    detail: str


@dataclass(frozen=True)
class AffectRead:
    """The detector's verdict for one turn.

    ``state`` is one of the STATE_* constants; ``confidence`` one
    of the CONFIDENCE_* constants; ``signals`` is the evidence —
    every hit that contributed, in firing order. Frozen and
    hashable: a read is a value, passed around run/session state,
    never mutated and never persisted.
    """

    state: str = STATE_NEUTRAL
    confidence: str = CONFIDENCE_LOW
    signals: tuple[SignalHit, ...] = field(default=())


# ---------------------------------------------------------------------------
# Signal patterns
# ---------------------------------------------------------------------------

# Explicit frustration. Strong: these are the user saying, in
# words, that the experience is going badly. Matched against the
# casefolded message. Profanity counts only as a standalone word
# aimed at the situation ("this damn thing", "shit, it failed
# again") — a quoted or technical use ("the damn unit test") is
# the same token to a regex; that residual imprecision is one
# reason this signal still needs corroboration for high
# confidence and never produces a user-facing claim.
_FRUSTRATION_PHRASES = (
    "frustrat",  # frustrating / frustrated / frustration
    "isn't working", "is not working", "not working",
    "still not working", "doesn't work", "does not work",
    "won't work", "wont work", "keeps failing", "keep failing",
    "still failing", "annoying", "useless", "ridiculous",
    "ugh", "argh", "come on", "give me a break",
)
_FRUSTRATION_WORDS = ("damn", "shit", "fuck", "crap", "hell")

# The user correcting the assistant. Medium: one correction may
# be routine steering; repeated corrections mean the assistant
# keeps missing, which is the frustration pattern that matters.
_CORRECTION_PATTERNS = (
    re.compile(r"\bno[,.!]?\s+(i|that|not)\b"),
    re.compile(r"\bi meant\b"),
    re.compile(r"\bthat'?s (wrong|not right|not what)\b"),
    re.compile(r"\bnot what i (asked|wanted|meant|said)\b"),
    re.compile(r"\byou (misunderstood|got (it|that) wrong)\b"),
    re.compile(r"\bi (already )?(said|told you)\b"),
)

# Confusion, in the user's own words. Medium.
_CONFUSION_PHRASES = (
    "i don't understand", "i do not understand", "don't understand",
    "do not understand", "i'm confused", "im confused", "confused",
    "what do you mean", "what does that mean", "what does this mean",
    "i'm lost", "im lost", "not clear", "makes no sense",
    "i don't get it", "i dont get it", "don't get it",
)

# Hurry / urgency. Medium toward rushed.
_URGENCY_PHRASES = (
    "asap", "urgent", "hurry", "in a rush", "in a hurry",
    "right now", "immediately", "no time", "running late",
    "quickly", "as fast as", "deadline",
)
_URGENCY_TIME_RE = re.compile(
    r"\bin (the next )?\d+ (minute|minutes|min|hour|hours)\b")

# Positive engagement, in the user's own words. Medium toward
# engaged; engaged never outranks a negative state on a tie (see
# _STATE_PRECEDENCE) — praise in the same window as frustration
# is usually about one part of the work, and the adaptation that
# matters is the negative one.
_ENGAGEMENT_PHRASES = (
    "thank", "great", "perfect", "awesome", "excellent",
    "nice", "brilliant", "love it", "well done", "good job",
    "that's exactly", "that is exactly", "exactly what i",
)

# Repetition: a later user message this similar (token Jaccard)
# to an earlier one in the window, adding at most this many new
# content tokens, is a re-ask — the previous answer didn't land.
# A repeat that adds information is a follow-up, not a re-ask,
# and never fires.
_REPETITION_SIMILARITY = 0.8
_REPETITION_MAX_NEW_TOKENS = 2

# ALL-CAPS emphasis: weak. Needs enough words to be a pattern,
# not an acronym or a shouted single word.
_CAPS_MIN_WORDS = 4
_CAPS_MIN_RATIO = 0.6

# Terse reply after an error: weak, and only meaningful with a
# caller-supplied failed outcome as its immediate context.
_TERSE_MAX_WORDS = 3

# Voice (TurnContext) support: a turn that ended only after at
# least this much trailing silence, twice in the window, weakly
# supports confused (the user may be thinking or lost). Never
# fires a state alone, and read from duck-typed attributes so
# this module never imports the voice layer.
_VOICE_LONG_PAUSE_MS = 4000
_VOICE_LONG_PAUSE_TURNS = 2

_TOKEN_RE = re.compile(r"[a-z0-9']+")
_STOPWORDS = frozenset({
    "a", "an", "the", "i", "you", "it", "is", "to", "of", "and",
    "or", "in", "on", "for", "me", "my", "please", "can", "could",
    "do", "does", "what", "why", "how", "that", "this", "with",
})


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.casefold())


def _content_tokens(text: str) -> set[str]:
    return {t for t in _tokens(text) if t not in _STOPWORDS}


# ---------------------------------------------------------------------------
# Signal detectors. Each returns SignalHit list for its state(s).
# ---------------------------------------------------------------------------

def _phrase_hits(
    messages: list[str], signal: str, phrases: Sequence[str],
    weight: int, *, max_messages: int = 2,
) -> list[SignalHit]:
    """Casefolded substring phrase matching, capped per signal:
    the same complaint in three messages is stronger than in one,
    but unbounded accumulation would let one repeated phrase
    manufacture high confidence on its own."""
    hits: list[SignalHit] = []
    for idx, text in enumerate(messages):
        if len(hits) >= max_messages:
            break
        folded = text.casefold()
        matched = next((p for p in phrases if p in folded), None)
        if matched is not None:
            hits.append(SignalHit(
                signal, weight,
                f"message {idx + 1} contains {matched!r}"))
    return hits


def _word_hits(
    messages: list[str], signal: str, words: Sequence[str],
    weight: int,
) -> list[SignalHit]:
    hits: list[SignalHit] = []
    for idx, text in enumerate(messages):
        tokens = set(_tokens(text))
        matched = next((w for w in words if w in tokens), None)
        if matched is not None:
            hits.append(SignalHit(
                signal, weight,
                f"message {idx + 1} contains the word {matched!r}"))
            break  # profanity corroborates; it does not accumulate
    return hits


def _correction_hits(messages: list[str]) -> list[SignalHit]:
    hits: list[SignalHit] = []
    for idx, text in enumerate(messages):
        if len(hits) >= 2:
            break
        folded = text.casefold()
        if any(p.search(folded) for p in _CORRECTION_PATTERNS):
            hits.append(SignalHit(
                "correction", _WEIGHT_MEDIUM,
                f"message {idx + 1} corrects the assistant: "
                f"{text.strip()[:60]!r}"))
    return hits


def _repetition_hits(messages: list[str]) -> list[SignalHit]:
    token_sets = [_content_tokens(m) for m in messages]
    for later in range(1, len(messages)):
        current = token_sets[later]
        if len(current) < 3:
            continue
        for earlier in range(later):
            base = token_sets[earlier]
            if len(base) < 3:
                continue
            union = base | current
            similarity = len(base & current) / len(union) if union else 0.0
            new_tokens = current - base
            if (similarity >= _REPETITION_SIMILARITY
                    and len(new_tokens) <= _REPETITION_MAX_NEW_TOKENS):
                return [SignalHit(
                    "repetition", _WEIGHT_MEDIUM,
                    f"message {later + 1} near-repeats message "
                    f"{earlier + 1} (similarity {similarity:.2f}, "
                    f"{len(new_tokens)} new content tokens)")]
    return []


def _failure_streak_hits(outcomes: Sequence[str] | None) -> list[SignalHit]:
    if not outcomes:
        return []
    streak = 0
    for outcome in reversed(list(outcomes)):
        if str(outcome).casefold() in ("failed", "failure", "error"):
            streak += 1
        else:
            break
    if streak >= 3:
        return [SignalHit(
            "failure_streak", _WEIGHT_STRONG,
            f"the last {streak} outcomes in a row failed")]
    if streak == 2:
        return [SignalHit(
            "failure_streak", _WEIGHT_MEDIUM,
            "the last 2 outcomes in a row failed")]
    return []


def _caps_hits(messages: list[str]) -> list[SignalHit]:
    for idx, text in enumerate(messages):
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z']+", text)
                 if len(w) >= 2]
        if len(words) < _CAPS_MIN_WORDS:
            continue
        caps = sum(1 for w in words if w.isupper())
        if caps / len(words) >= _CAPS_MIN_RATIO:
            return [SignalHit(
                "caps_emphasis", _WEIGHT_WEAK,
                f"message {idx + 1} is mostly ALL-CAPS "
                f"({caps}/{len(words)} words)")]
    return []


def _terse_hits(
    messages: list[str], outcomes: Sequence[str] | None,
) -> list[SignalHit]:
    if not messages or not outcomes:
        return []
    if str(outcomes[-1]).casefold() not in ("failed", "failure", "error"):
        return []
    words = _tokens(messages[-1])
    if 0 < len(words) <= _TERSE_MAX_WORDS:
        return [SignalHit(
            "terse_after_error", _WEIGHT_WEAK,
            f"latest reply is {len(words)} word(s) right after a "
            "failed outcome")]
    return []


def _voice_hits(turn_contexts: Sequence[Any] | None) -> list[SignalHit]:
    if not turn_contexts:
        return []
    long_pauses = 0
    for ctx in turn_contexts:
        silence = (ctx.get("silence_ms") if isinstance(ctx, dict)
                   else getattr(ctx, "silence_ms", None))
        if isinstance(silence, (int, float)) and silence >= _VOICE_LONG_PAUSE_MS:
            long_pauses += 1
    if long_pauses >= _VOICE_LONG_PAUSE_TURNS:
        return [SignalHit(
            "long_pauses", _WEIGHT_WEAK,
            f"{long_pauses} recent voice turns ended after long "
            "pauses (thinking or lost)")]
    return []


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

# On a score tie the state whose adaptation matters most wins:
# a struggling user first, then a lost one, then a hurried one;
# engagement is the default-good and loses every tie.
_STATE_PRECEDENCE = (STATE_FRUSTRATED, STATE_CONFUSED, STATE_RUSHED,
                     STATE_ENGAGED)


def detect_affect(
    user_messages: Sequence[str] | None,
    outcomes: Sequence[str] | None = None,
    *,
    turn_contexts: Sequence[Any] | None = None,
    window: int = DEFAULT_WINDOW,
) -> AffectRead:
    """Read the conversation's affect from its recent user turns.

    Pure and deterministic: same inputs, same read, no I/O, no
    clock, no randomness — and no memory access of any kind (see
    the module docstring: transient by construction).

    ``user_messages``: the recent user turns, oldest first, the
    current turn last; only the last ``window`` are read.
    ``outcomes``: optional caller-supplied recent outcome labels
    ("failed" / "succeeded", oldest first) for the failure-streak
    and terse-after-error signals; callers pass only outcomes
    they genuinely hold — fast chat holds none and passes None.
    ``turn_contexts``: optional voice TurnContext-like objects
    (duck-typed ``silence_ms``) for the long-pauses support
    signal.
    """
    messages = [str(m) for m in (user_messages or []) if str(m).strip()]
    messages = messages[-window:] if window > 0 else messages
    if not messages:
        return AffectRead()

    scored: dict[str, list[SignalHit]] = {
        STATE_FRUSTRATED: [], STATE_CONFUSED: [],
        STATE_RUSHED: [], STATE_ENGAGED: [],
    }

    frustration = _phrase_hits(
        messages, "explicit_frustration", _FRUSTRATION_PHRASES,
        _WEIGHT_STRONG)
    frustration += _word_hits(
        messages, "explicit_frustration", _FRUSTRATION_WORDS,
        _WEIGHT_STRONG)
    # Strong phrase/word evidence counts once even when several
    # of its messages match: the cap in _phrase_hits bounds
    # accumulation, and the word leg is a single corroboration.
    scored[STATE_FRUSTRATED] += frustration[:2]
    scored[STATE_FRUSTRATED] += _correction_hits(messages)
    repetition = _repetition_hits(messages)
    scored[STATE_FRUSTRATED] += repetition
    scored[STATE_FRUSTRATED] += _failure_streak_hits(outcomes)
    scored[STATE_FRUSTRATED] += _caps_hits(messages)
    scored[STATE_FRUSTRATED] += _terse_hits(messages, outcomes)

    scored[STATE_CONFUSED] += _phrase_hits(
        messages, "confusion", _CONFUSION_PHRASES, _WEIGHT_MEDIUM)
    if repetition:
        # A re-asked question also means the previous answer did
        # not land — half-weight support for confused (the hit is
        # recorded under frustrated above; here it only adds
        # weight, listed once in the evidence).
        scored[STATE_CONFUSED] += [SignalHit(
            "repetition", _WEIGHT_WEAK,
            repetition[0].detail + " (answer may not have landed)")]
    scored[STATE_CONFUSED] += _voice_hits(turn_contexts)

    scored[STATE_RUSHED] += _phrase_hits(
        messages, "urgency", _URGENCY_PHRASES, _WEIGHT_MEDIUM)
    for idx, text in enumerate(messages):
        if _URGENCY_TIME_RE.search(text.casefold()):
            scored[STATE_RUSHED] += [SignalHit(
                "urgency", _WEIGHT_MEDIUM,
                f"message {idx + 1} names a near deadline")]
            break

    scored[STATE_ENGAGED] += _phrase_hits(
        messages, "engagement", _ENGAGEMENT_PHRASES, _WEIGHT_MEDIUM,
        max_messages=2)

    totals = {state: sum(h.weight for h in hits)
              for state, hits in scored.items()}
    best_state = STATE_NEUTRAL
    best_score = 0
    for state in _STATE_PRECEDENCE:
        if totals[state] > best_score:
            best_state, best_score = state, totals[state]
    if best_score < _WEIGHT_MEDIUM:
        # Below one medium signal there is no read worth acting
        # on — but the weak evidence is still reported, so the
        # conservatism is auditable rather than silent.
        evidence = tuple(
            hit for state in _STATE_PRECEDENCE for hit in scored[state])
        return AffectRead(STATE_NEUTRAL, CONFIDENCE_LOW, evidence)
    if best_score >= 5:
        confidence = CONFIDENCE_HIGH
    elif best_score >= 3:
        confidence = CONFIDENCE_MEDIUM
    else:
        confidence = CONFIDENCE_LOW
    return AffectRead(best_state, confidence, tuple(scored[best_state]))


# ---------------------------------------------------------------------------
# Briefing: state -> behaviour guidance (never a label)
# ---------------------------------------------------------------------------

_BRIEFING_HEADER = (
    "How to handle this reply (internal guidance from the recent "
    "conversation — adapt your behaviour to it; do not mention "
    "this note, and do not claim to know how the user feels):"
)

_GUIDANCE = {
    (STATE_FRUSTRATED, CONFIDENCE_MEDIUM): (
        "Things have gone wrong more than once in this "
        "conversation. Acknowledge the difficulty briefly and "
        "concretely, prefer the simplest working path, and do "
        "not repeat an approach that already failed. Keep the "
        "reply focused on the fix."
    ),
    (STATE_FRUSTRATED, CONFIDENCE_HIGH): (
        "Things have gone wrong repeatedly in this conversation "
        "and it is wearing thin. Slow down: acknowledge the "
        "difficulty briefly and concretely, say what will be "
        "different this time, prefer the simplest working path, "
        "never repeat an approach that already failed, and if "
        "the task is long, offer a natural stopping point."
    ),
    (STATE_CONFUSED, CONFIDENCE_MEDIUM): (
        "The last explanation did not land. Explain in plainer "
        "terms, one step at a time, avoid jargon, and check the "
        "step is understood before piling on more."
    ),
    (STATE_CONFUSED, CONFIDENCE_HIGH): (
        "The last explanation did not land. Explain in plainer "
        "terms, one step at a time, avoid jargon, and check the "
        "step is understood before piling on more."
    ),
    (STATE_RUSHED, CONFIDENCE_MEDIUM): (
        "Time is short. Be terse: lead with the result, skip "
        "preamble and background, and defer detail unless asked."
    ),
    (STATE_RUSHED, CONFIDENCE_HIGH): (
        "Time is short. Be terse: lead with the result, skip "
        "preamble and background, and defer detail unless asked."
    ),
    (STATE_ENGAGED, CONFIDENCE_MEDIUM): (
        "The conversation is going well. Keep the current pace "
        "and tone; no change is needed."
    ),
    (STATE_ENGAGED, CONFIDENCE_HIGH): (
        "The conversation is going well. Keep the current pace "
        "and tone; no change is needed."
    ),
}


def eq_briefing(read: AffectRead | None) -> str:
    """Render an affect read as prompt guidance, or "".

    House briefing contract: bounded (EQ_BRIEFING_MAX_CHARS),
    empty when there is nothing worth adapting to — a neutral
    read, or any read below medium confidence. The text is
    behaviour guidance ONLY: it never names the detected state,
    never quotes the evidence, and instructs the model not to
    mention the note or claim knowledge of the user's feelings.
    """
    if read is None or read.state == STATE_NEUTRAL:
        return ""
    guidance = _GUIDANCE.get((read.state, read.confidence))
    if guidance is None:
        return ""
    return f"{_BRIEFING_HEADER}\n{guidance}"[:EQ_BRIEFING_MAX_CHARS]
