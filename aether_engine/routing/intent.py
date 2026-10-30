"""Intent classification for the START_TASK fast-chat split (Epic 1, E1-04).

Every chat message used to enter the full supervisor graph, so even a
greeting paid for routing, delegation and checkpoint machinery. This
module decides whether a prompt is plain conversation (fast path: a
direct streamed reply from the resolved chat model) or work (the
supervisor graph, unchanged).

Order of decision:
  1. Deterministic work heuristics — code fences, file references and
     imperative tool-shaped requests are never second-guessed by a model.
  2. A tiny structured verdict from the resolved chat model, when a
     provider is available.
  3. Deterministic chat fallback (greetings / plain questions) for
     stub or offline cases where no verdict could be obtained.

Bias: ambiguous prompts go to the graph. A work request answered by the
tool-less fast path would be under-served; a chat message taking the
graph only costs latency.
"""
import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from aether_engine.providers.base import BaseProvider

logger = logging.getLogger("aether_engine.routing.intent")

CHAT = "chat"
WORK = "work"

# A reply this long is a document or a spec, not conversation.
_LONG_PROMPT_CHARS = 1200
# Fallback chat shapes only apply to messages of conversational size.
_FALLBACK_CHAT_MAX_CHARS = 500
_GREETING_MAX_CHARS = 80


@dataclass(frozen=True)
class IntentDecision:
    """Where a prompt should be served from, and why."""

    route: str  # CHAT or WORK
    reason: str
    source: str  # "heuristic" | "model" | "fallback"

    @property
    def is_chat(self) -> bool:
        return self.route == CHAT


# ---------------------------------------------------------------------------
# Deterministic work signals
# ---------------------------------------------------------------------------

# A file path or bare code filename. The lookbehind keeps words like
# "and/or" from matching the path alternative.
_FILE_REFERENCE_RE = re.compile(
    r"(?<![\w/~.])(?:~|\.{1,2})?/[\w.\-]+(?:/[\w.\-]+)*/?"
    r"|(?<![\w/])[\w\-]+(?:/[\w\-]+)+\.[A-Za-z]{1,6}\b"
    r"|[A-Za-z]:\\(?:[\w.\-]+\\)*[\w.\-]+"
    r"|\b[\w\-]+\.(?:py|pyw|js|jsx|ts|tsx|mjs|cjs|rs|go|java|c|h|hh|cpp|hpp|cxx|cs|rb|php|swift|kt|kts|sh|bash|zsh|ps1|sql|html|htm|css|scss|json|jsonc|toml|yaml|yml|xml|csv|tsv|ipynb)\b",
    re.IGNORECASE,
)

_WORK_VERB_OBJECT_RE = re.compile(
    r"\b(?:write|create|generate|build|make|edit|modify|update|delete|remove|rename|move|copy|fix|debug|refactor|implement|install|deploy|run|execute|commit|publish|download|fetch|scrape|search|find|list|read|open|check|investigate|review|test)\b"
    r"[^.?!]{0,80}?"
    r"\b(?:files?|code|script|function|class|module|program|app|application|website|web\s?page|component|tests?|bug|errors?|command|server|database|db|repo|repository|project|api|endpoint|config|configuration|directory|folder|branch|web|internet|online|docs|documentation)\b",
    re.IGNORECASE,
)

_WORK_PHRASE_RES = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bsearch (?:the web|online)\b",
        r"\blook (?:it|this|that|them|\w+) up online\b",
        r"\bbrowse the (?:web|internet)\b",
        r"\bcurrent (?:weather|news|price|score)\b",
        r"\blatest (?:news|version|release)\b",
    )
]

_NUMBERED_STEP_RE = re.compile(r"(?m)^\s*\d{1,2}[.)]\s+\S")


def work_heuristic_reason(prompt: str) -> str | None:
    """Return why *prompt* is definitely work, or None if not obvious."""
    if "```" in prompt:
        return "contains a code block"
    if _FILE_REFERENCE_RE.search(prompt):
        return "references a file"
    if _WORK_VERB_OBJECT_RE.search(prompt):
        return "imperative tool-shaped request"
    for pattern in _WORK_PHRASE_RES:
        if pattern.search(prompt):
            return "asks for web or current information"
    if len(_NUMBERED_STEP_RE.findall(prompt)) >= 2 or (
        " and then " in prompt.lower() and _WORK_VERB_OBJECT_RE.search(prompt)
    ):
        return "multi-step request"
    if len(prompt) > _LONG_PROMPT_CHARS:
        return "long structured request"
    return None


# ---------------------------------------------------------------------------
# Deterministic chat fallback (only used when no model verdict is available)
# ---------------------------------------------------------------------------

_GREETING_RE = re.compile(
    r"^\s*(?:hi+|hello|hey+|yo|hola|namaste|good\s+(?:morning|afternoon|evening|night)|thanks|thank\s+you|thx|bye|goodbye|ok|okay|cool|nice|great|awesome|wow|lol|haha)\b",
    re.IGNORECASE,
)

_QUESTION_START_RE = re.compile(
    r"^\s*(?:what|who|whom|whose|when|where|why|which|how|is|are|was|were|do|does|did|can|could|would|should|may|explain|describe|define|tell\s+me|give\s+me|list\s+(?:some|a\s+few)|name\s+(?:some|a\s+few)|compare|difference\s+between)\b",
    re.IGNORECASE,
)


def fallback_chat_reason(prompt: str) -> str | None:
    """Return why *prompt* reads as plain conversation, or None."""
    if len(prompt) <= _GREETING_MAX_CHARS and _GREETING_RE.search(prompt):
        return "greeting or smalltalk"
    if len(prompt) <= _FALLBACK_CHAT_MAX_CHARS:
        if prompt.rstrip().endswith("?"):
            return "plain question"
        if _QUESTION_START_RE.search(prompt):
            return "explanation request"
    return None


# ---------------------------------------------------------------------------
# Model verdict
# ---------------------------------------------------------------------------

_CLASSIFIER_SYSTEM = (
    "You route user messages for an AI assistant. Reply with exactly one word.\n"
    "CHAT: plain conversation, greetings, opinions, jokes, or questions "
    "answerable from general knowledge and the conversation so far.\n"
    "WORK: anything that needs tools or multiple steps — files, code, "
    "commands, web search or research, creating, editing or fixing things.\n"
    "Reply with only CHAT or WORK."
)

_VERDICT_RE = re.compile(r"\b(CHAT|WORK)\b")


async def _collect_verdict(provider: "BaseProvider", prompt: str) -> str | None:
    messages = [
        {"role": "system", "content": _CLASSIFIER_SYSTEM},
        {"role": "user", "content": prompt[:2000]},
    ]
    text = ""
    stream = provider.call_stream(messages, tools=None, node_name="intent_classifier")
    try:
        async for chunk in stream:
            if chunk.text:
                text += chunk.text
                match = _VERDICT_RE.search(text.upper())
                if match:
                    return match.group(1).lower()
                if len(text) > 200:
                    break
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            await aclose()
    match = _VERDICT_RE.search(text.upper())
    return match.group(1).lower() if match else None


async def _model_verdict(
    provider: "BaseProvider", prompt: str, timeout_s: float
) -> str | None:
    try:
        return await asyncio.wait_for(_collect_verdict(provider, prompt), timeout=timeout_s)
    except Exception:  # Classifier boundary: any provider/transport failure means "no verdict"; heuristics decide instead.
        logger.debug("Intent model verdict unavailable; falling back to heuristics", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def classify_intent(
    prompt: str,
    provider: Optional["BaseProvider"] = None,
    *,
    model_timeout_s: float = 12.0,
) -> IntentDecision:
    """Classify *prompt* as CHAT (fast path) or WORK (supervisor graph)."""
    stripped = prompt.strip()
    if not stripped:
        return IntentDecision(CHAT, "empty prompt", "heuristic")

    reason = work_heuristic_reason(stripped)
    if reason:
        return IntentDecision(WORK, reason, "heuristic")

    if provider is not None:
        verdict = await _model_verdict(provider, stripped, model_timeout_s)
        if verdict:
            return IntentDecision(verdict, "model verdict", "model")

    reason = fallback_chat_reason(stripped)
    if reason:
        return IntentDecision(CHAT, reason, "fallback")

    return IntentDecision(WORK, "ambiguous — biased to the supervisor graph", "fallback")
