"""Epic 1, E1-12: the advisor pass.

When an advisor model is configured (``auxiliary_models["advisor"]``
as a ``"provider:::model"`` spec), each specialist invocation first
gets a short independent review of the task — risks, blind spots,
things to verify — injected into its system prompt as advice to weigh,
never instructions to obey.

The advisor is strictly optional and strictly best-effort: without a
usable configured spec there is no advisor call and zero behaviour
change, and any advisor failure (unusable provider, timeout, error)
degrades to no notes rather than failing the task.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from aether_engine.langgraph.state import blackboard_briefing

logger = logging.getLogger("aether_engine.langgraph.advisor")

ADVISOR_TIMEOUT_SECONDS = 30
_MAX_NOTES_CHARS = 1500


def resolve_advisor_provider(user_config: Any | None, secret_store: Any) -> Any | None:
    """The configured advisor provider, or None when no advisor is set.

    Unlike the compression summariser there is NO fallback to the run
    provider: the advisor exists only when explicitly configured.
    """
    override = ((getattr(user_config, "auxiliary_models", None) or {}).get("advisor") or "").strip()
    if not override or override == "auto" or not secret_store:
        return None
    p_name, sep, m_name = override.partition(":::")
    if not (sep and p_name and m_name):
        logger.warning("Advisor auxiliary override %r is not a 'provider:::model' spec; advisor disabled", override)
        return None
    try:
        from aether_engine.orchestration.core import _create_provider_instance

        return _create_provider_instance(p_name, m_name, secret_store)
    except Exception as e:  # noqa: BLE001 — an unusable advisor override disables the advisor; it must never break a task.
        logger.warning(f"Advisor auxiliary override {override!r} unusable, advisor disabled: {e}")
        return None


async def advisor_notes(config: dict, state: dict, agent: str) -> str:
    """Review notes for ``agent``'s imminent work, or "" for none."""
    configurable = config.get("configurable", {}) or {}
    provider = resolve_advisor_provider(configurable.get("user_config"), configurable.get("secret_store"))
    if provider is None:
        return ""
    prompt_parts = [
        f"You are an independent reviewer. Another agent ({agent}) is about to act on this task. Give short, concrete review notes: risks, blind spots, and what to verify. A few bullet lines at most; no preamble.",
        f"Task: {state.get('original_prompt') or ''}",
    ]
    briefing = (state.get("context_briefing") or "").strip()
    if briefing:
        prompt_parts.append(f"Memory briefing: {briefing}")
    board = blackboard_briefing(state)
    if board:
        prompt_parts.append(f"Team blackboard so far:\n{board}")
    messages = [{"role": "user", "content": "\n\n".join(prompt_parts)}]
    try:
        parts: list[str] = []

        async def _collect() -> None:
            async for chunk in provider.call_stream(messages, tools=[], node_name="advisor"):
                if chunk.text:
                    parts.append(chunk.text)
                if chunk.finish_reason:
                    break

        await asyncio.wait_for(_collect(), timeout=ADVISOR_TIMEOUT_SECONDS)
    except Exception as e:  # noqa: BLE001 — advisor boundary: any failure (incl. timeout) means no notes, never a failed task.
        logger.warning(f"Advisor call failed, continuing without notes: {e}")
        return ""
    return "".join(parts).strip()[:_MAX_NOTES_CHARS]
