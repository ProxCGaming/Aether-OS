"""Conversation compaction / summarisation (Epic 1, E1-06).

Long sessions must not grow the model's context without bound, and
compaction must never silently truncate user-visible history. The
pattern (Deep Agents study): when a thread's history passes a
threshold, the older turns are folded into a compact summary; models
see [summary as system context] + the recent verbatim tail, while the
full history stays untouched in the checkpoint and the session store
(the UI keeps showing everything — only the model's view changes).

Division of labour:

- ``plan_compaction`` is the pure core: given the thread's messages,
  the existing summary and the marker of how many messages that
  summary already covers, it decides whether to compact and exactly
  which slice to fold. No I/O, no model call — trivially testable.
- ``compact_history`` applies a plan through an injected async
  summariser callable, so tests drive it with a stub and the executor
  wires a real provider.
- ``model_facing_messages`` / ``context_summary_section`` are the read
  side: the ONE view every model consumer (specialist nodes, the
  supervisor's routing prompt) builds its input through, so the
  compacted view cannot drift between nodes.
- The executor runs compaction at run assembly (before the graph
  starts) and stores the result in two state fields —
  ``context_summary`` and ``context_summary_upto`` (the count of
  thread messages folded into the summary). The marker makes
  compaction incremental across turns: a later round folds only the
  turns that aged since the previous one, never re-summarising from
  scratch. The messages channel itself is never rewritten.

The summariser model resolves from the ``compression`` auxiliary
override (``auxiliary_models["compression"]`` — this module's call
site is why that task class has a real engine seam, cf. config.py
``AUXILIARY_TASK_CLASSES``), falling back to the run's own provider.
"""
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aether_engine.langgraph.state import message_dicts

logger = logging.getLogger("aether_engine.langgraph.compaction")

# Threshold defaults (the brief's numbers; overridable per user via
# UserConfig compaction_* fields — see compaction_config_from).
DEFAULT_TRIGGER_MESSAGES = 40
DEFAULT_TRIGGER_CHARS = 24_000
DEFAULT_KEEP_RECENT = 12

# An async callable: (existing summary or None, folded messages) -> new summary.
Summariser = Callable[[str | None, list[dict]], Awaitable[str]]


@dataclass(frozen=True)
class CompactionConfig:
    """When to compact and how much verbatim tail to keep."""

    trigger_messages: int = DEFAULT_TRIGGER_MESSAGES
    trigger_chars: int = DEFAULT_TRIGGER_CHARS
    keep_recent: int = DEFAULT_KEEP_RECENT


@dataclass(frozen=True)
class CompactionPlan:
    """A decided fold: messages[fold_start:fold_end] go into the summary."""

    fold_start: int
    fold_end: int  # exclusive; becomes the new context_summary_upto
    folded_count: int
    total_messages: int
    total_chars: int


def compaction_config_from(user_config: Any | None) -> CompactionConfig:
    """Thresholds from the persisted user config (defaults when unset)."""
    if user_config is None:
        return CompactionConfig()
    return CompactionConfig(
        trigger_messages=int(
            getattr(user_config, "compaction_trigger_messages", DEFAULT_TRIGGER_MESSAGES)
            or DEFAULT_TRIGGER_MESSAGES
        ),
        trigger_chars=int(
            getattr(user_config, "compaction_trigger_chars", DEFAULT_TRIGGER_CHARS)
            or DEFAULT_TRIGGER_CHARS
        ),
        keep_recent=int(
            getattr(user_config, "compaction_keep_recent", DEFAULT_KEEP_RECENT)
            or DEFAULT_KEEP_RECENT
        ),
    )


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return str(content)


def _total_chars(messages: list[dict]) -> int:
    return sum(len(_message_text(m)) for m in messages)


def plan_compaction(
    messages: list,
    summary_upto: int = 0,
    config: CompactionConfig | None = None,
) -> CompactionPlan | None:
    """Decide whether *messages* (the thread's full history) compact.

    Returns None when the model-facing history is within thresholds or
    there is nothing new to fold. Otherwise returns the fold slice:
    everything from ``summary_upto`` (older turns are already inside
    the existing summary) up to a tail boundary that keeps the most
    recent ``keep_recent`` messages verbatim. The boundary never starts
    the kept tail with a tool result — its owning assistant tool-call
    would be folded away, leaving an orphan providers reject — so it
    walks back over any leading tool messages.
    """
    cfg = config or CompactionConfig()
    dicts = message_dicts(messages)
    total = len(dicts)
    chars = _total_chars(dicts)
    if total <= cfg.trigger_messages and chars <= cfg.trigger_chars:
        return None

    upto = max(0, min(int(summary_upto or 0), total))
    fold_end = total - cfg.keep_recent
    while fold_end > upto and dicts[fold_end].get("role") == "tool":
        fold_end -= 1
    if fold_end <= upto:
        return None
    return CompactionPlan(
        fold_start=upto,
        fold_end=fold_end,
        folded_count=fold_end - upto,
        total_messages=total,
        total_chars=chars,
    )


async def compact_history(
    messages: list,
    existing_summary: str | None,
    summary_upto: int,
    summariser: Summariser,
    config: CompactionConfig | None = None,
) -> tuple[str, int] | None:
    """Apply compaction: returns (new_summary, new_upto) or None.

    The summariser receives the existing summary plus ONLY the newly
    folded slice — incremental by construction (E1-06 acceptance).
    """
    plan = plan_compaction(messages, summary_upto, config)
    if plan is None:
        return None
    dicts = message_dicts(messages)
    folded = dicts[plan.fold_start:plan.fold_end]
    new_summary = await summariser(existing_summary, folded)
    if not (new_summary or "").strip():
        # A summariser that returns nothing must not advance the
        # marker: those turns would be silently lost from the model's
        # view. Leave state untouched and try again next run.
        logger.warning(
            "Compaction summariser returned an empty summary; leaving history uncompacted"
        )
        return None
    logger.info(
        "Context compacted: folded %d of %d messages (%d chars) into summary "
        "(marker %d -> %d)",
        plan.folded_count, plan.total_messages, plan.total_chars,
        plan.fold_start, plan.fold_end,
    )
    return new_summary, plan.fold_end


def model_facing_messages(state: dict | None) -> list[dict]:
    """The messages a model should see for this state.

    Without a summary this is exactly ``message_dicts(state messages)``
    — byte-identical to pre-compaction behaviour. With one, it is the
    verbatim tail after the folded prefix; the summary itself travels
    separately (``context_summary_section``) because each consumer
    renders its own system prompt.
    """
    dicts = message_dicts((state or {}).get("messages", []))
    summary = ((state or {}).get("context_summary") or "").strip()
    upto = int((state or {}).get("context_summary_upto") or 0)
    if not summary or upto <= 0:
        return dicts
    return dicts[min(upto, len(dicts)):]


def context_summary_section(state: dict | None) -> str:
    """The summary rendered as a system-prompt section ("" when none)."""
    summary = ((state or {}).get("context_summary") or "").strip()
    if not summary:
        return ""
    upto = int((state or {}).get("context_summary_upto") or 0)
    return (
        "Summary of the earlier conversation "
        f"({upto} message(s) compacted; the full history remains in the session):\n"
        f"{summary}"
    )


_SUMMARY_INSTRUCTION = (
    "You are compacting a conversation so an AI assistant can continue it "
    "with a smaller context. Write an updated summary that preserves: the "
    "user's goals and constraints, decisions made and why, concrete facts, "
    "names, file paths, commands and their outcomes, tool results that "
    "later turns may rely on, and any open threads or unanswered questions. "
    "Be concise and factual; do not invent anything that was not said."
)


def _render_turns(messages: list[dict]) -> str:
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role") or "unknown"
        text = _message_text(msg)
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            names = []
            for tc in tool_calls:
                if isinstance(tc, dict):
                    names.append(str(tc.get("function", {}).get("name") or "tool"))
            if names:
                text = f"{text} [called: {', '.join(names)}]".strip()
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


def make_provider_summariser(provider: Any) -> Summariser:
    """Build the production summariser over an Aether provider.

    Uses the provider's plain streaming call (no tools); the result is
    the same text-collection the chat paths use.
    """

    async def _summarise(existing_summary: str | None, folded: list[dict]) -> str:
        parts = [_SUMMARY_INSTRUCTION]
        if existing_summary:
            parts.append(f"Existing summary of everything before these turns:\n{existing_summary}")
        parts.append(f"New turns to fold into the summary:\n{_render_turns(folded)}")
        messages = [{"role": "user", "content": "\n\n".join(parts)}]
        chunks: list[str] = []
        async for chunk in provider.call_stream(messages, tools=None, node_name="compaction"):
            if chunk.text:
                chunks.append(chunk.text)
            if chunk.finish_reason:
                break
        return "".join(chunks).strip()

    return _summarise


def resolve_summariser_provider(user_config: Any | None, secret_store: Any, fallback: Any, *, local_only: bool = False) -> Any:
    """Resolve the summariser provider: compression override or fallback.

    Mirrors how the specialist nodes resolve per-agent overrides and
    how routing resolves the vision auxiliary override: a
    ``"provider:::model"`` string wins when it is usable; anything else
    (absent, ``"auto"``, unusable) falls back to the run's provider.

    ``local_only`` (finding M17 residual) carries the run's routing
    constraint into this construction too: folded history can contain
    image content (stringified by the summariser's prompt builder),
    so a local-only run's summariser must not acquire cloud fallbacks.
    """
    override = ((getattr(user_config, "auxiliary_models", None) or {}).get("compression") or "").strip()
    if override and override != "auto" and secret_store:
        p_name, sep, m_name = override.partition(":::")
        if sep and p_name and m_name:
            try:
                from aether_engine.orchestration.core import _create_provider_instance

                return _create_provider_instance(p_name, m_name, secret_store, local_only=local_only)
            except Exception as e:  # noqa: BLE001 — an unusable compression override falls back to the run provider; compaction must not fail the task.
                logger.warning(
                    f"Compression auxiliary override {override!r} unusable, "
                    f"using the run provider for summarisation: {e}"
                )
    return fallback


def serialise_plan(plan: CompactionPlan) -> str:
    """One-line plan description for audit details."""
    return json.dumps(
        {
            "folded": plan.folded_count,
            "total_messages": plan.total_messages,
            "total_chars": plan.total_chars,
            "upto": plan.fold_end,
        },
        sort_keys=True,
    )
