"""Epic 1, E1-08: model-written session titles.

After a session's first completed task, if its title is still the
auto default (empty, "New Chat", or a plain prefix of the first user
message — the shape every auto-title takes), a model writes a short
real title for it. The writer is the ``title`` auxiliary model when
configured, otherwise the task's own provider/model. Strictly
best-effort: any failure keeps the existing title.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("aether_engine.session_titles")

TITLE_TIMEOUT_SECONDS = 15
_MAX_TITLE_CHARS = 60

_SYSTEM = (
    "Write a short, specific title (at most 6 words) for this conversation. "
    "Reply with the title only — no quotes, no punctuation at the end."
)


def is_default_title(title: str | None, first_user_content: str | None) -> bool:
    """True when ``title`` is still an untouched auto title."""
    if not title or title.strip() in ("", "New Chat"):
        return True
    if first_user_content:
        candidate = title.strip()
        # Auto titles are the beginning of the first prompt, cut off.
        if first_user_content.startswith(candidate) and len(candidate) < len(first_user_content):
            return True
    return False


def clean_title(raw: str | None) -> str | None:
    """Normalise a model's title reply, or None when unusable."""
    if not raw:
        return None
    text = " ".join(raw.strip().strip('"').strip("'").split())
    text = text.rstrip(".")
    if not text:
        return None
    return text[:_MAX_TITLE_CHARS]


async def generate_session_title(provider: Any, user_prompt: str, assistant_response: str) -> str | None:
    """Ask ``provider`` for a title; None on any failure or timeout."""
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"User: {user_prompt[:500]}\nAssistant: {assistant_response[:500]}"},
    ]
    parts: list[str] = []

    async def _collect() -> None:
        async for chunk in provider.call_stream(messages, tools=[], node_name="title"):
            if chunk.text:
                parts.append(chunk.text)
            if chunk.finish_reason:
                break

    try:
        await asyncio.wait_for(_collect(), timeout=TITLE_TIMEOUT_SECONDS)
    except Exception as e:  # noqa: BLE001 — titles are best-effort decoration; never fail or delay a task over one.
        logger.warning(f"Title generation failed, keeping the default title: {e}")
        return None
    return clean_title("".join(parts))


def resolve_title_provider(user_config: Any, secret_store: Any, fallback: Any) -> Any:
    """The title auxiliary override when usable, else the task provider."""
    override = ((getattr(user_config, "auxiliary_models", None) or {}).get("title") or "").strip()
    if override and override != "auto" and secret_store:
        p_name, sep, m_name = override.partition(":::")
        if sep and p_name and m_name:
            try:
                from aether_engine.orchestration.core import _create_provider_instance

                return _create_provider_instance(p_name, m_name, secret_store)
            except Exception as e:  # noqa: BLE001 — unusable title override falls back to the task provider.
                logger.warning(f"Title auxiliary override {override!r} unusable, using the task provider: {e}")
    return fallback
