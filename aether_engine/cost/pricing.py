"""Pricing for the Epic 8 cost ledger (E8-A8.1a).

Every completed model call is priced exactly one of three ways:

1. **Local** — Ollama-served calls are always $0.00 (``is_local``).
2. **LiteLLM** — ``litellm.completion_cost()`` for models LiteLLM
   knows. Verified against litellm 1.86: the default model
   ``gemini/gemini-3.5-flash`` is NOT in LiteLLM's cost map and
   ``completion_cost`` raises for it, which is why the fallback table
   below exists and is load-bearing, not decorative.
3. **Fallback table** — curated USD rates for models LiteLLM misses.

A model no source can price is reported ``unpriced`` at $0.00 —
honest, never silently treated as a priced zero. Calls whose token
counts were estimated (the provider reported no usage) price as
``estimated`` so surfaces can distinguish them from exact figures.

LiteLLM is imported lazily inside functions (Epic 9: the import costs
seconds and no pricing can happen before a provider call anyway).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("aether_engine.cost.pricing")

# USD per 1M tokens: (input, output). Fallback only — LiteLLM's own
# cost map wins wherever it has the model.
FALLBACK_PRICE_TABLE: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
}

_LOCAL_PROVIDERS = {"ollama"}
_LOCAL_MODEL_PREFIXES = ("ollama/", "ollama_chat/")

# Prefixes stripped to reach the bare model id used as a table key.
_KNOWN_PREFIXES = (
    "openrouter/", "gemini/", "anthropic/", "deepseek/", "openai/",
    "groq/", "mistral/", "ollama/", "ollama_chat/", "azure/",
    "bedrock/", "vertex_ai/", "huggingface/", "together_ai/",
    "perplexity/", "cohere/",
)


@dataclass(frozen=True)
class PriceResult:
    """Outcome of pricing one call."""

    cost_usd: float
    status: str  # "priced" | "unpriced" | "estimated"
    is_local: bool


def is_local_call(model: str, provider_name: str) -> bool:
    """True when the call was served by a local runtime (Ollama)."""
    if (provider_name or "").lower() in _LOCAL_PROVIDERS:
        return True
    return (model or "").lower().startswith(_LOCAL_MODEL_PREFIXES)


def _bare_model_id(model: str) -> str:
    bare = model or ""
    for prefix in _KNOWN_PREFIXES:
        if bare.startswith(prefix):
            return bare[len(prefix):]
    return bare


def price_call(
    model: str,
    provider_name: str,
    input_tokens: int,
    output_tokens: int,
    *,
    estimated: bool = False,
) -> PriceResult:
    """Price one completed call. Never raises."""
    if is_local_call(model, provider_name):
        return PriceResult(cost_usd=0.0, status="priced", is_local=True)

    status = "estimated" if estimated else "priced"
    candidates = [model, _bare_model_id(model)]

    try:
        import litellm

        for candidate in candidates:
            try:
                cost = litellm.completion_cost(
                    model=candidate,
                    prompt_tokens=input_tokens,
                    completion_tokens=output_tokens,
                )
            except Exception:  # unknown model/provider shapes raise varied errors; the table is next.
                logger.debug("completion_cost failed for %s", candidate, exc_info=True)
                continue
            if cost is not None:
                return PriceResult(cost_usd=float(cost), status=status, is_local=False)
    except Exception:  # litellm itself unavailable: the fallback table still prices.
        logger.debug("LiteLLM pricing unavailable; using fallback table", exc_info=True)

    for candidate in candidates:
        rates = FALLBACK_PRICE_TABLE.get(candidate)
        if rates is not None:
            in_rate, out_rate = rates
            cost = input_tokens / 1_000_000 * in_rate + output_tokens / 1_000_000 * out_rate
            return PriceResult(cost_usd=cost, status=status, is_local=False)

    return PriceResult(cost_usd=0.0, status="unpriced", is_local=False)


def estimate_token_counts(
    *,
    model: str,
    messages: list[dict[str, Any]] | None,
    output_text: str,
) -> tuple[int, int] | None:
    """Best-effort token estimate when a provider reported no usage.

    Returns ``(input_tokens, output_tokens)`` or None when no counter
    is available — callers then record the call with zero tokens and
    an ``unpriced`` status rather than inventing numbers.
    """
    try:
        import litellm

        input_tokens = litellm.token_counter(model=model, messages=messages or [])
        output_tokens = litellm.token_counter(model=model, text=output_text or "")
        return int(input_tokens), int(output_tokens)
    except Exception:  # estimation is best-effort by design.
        logger.debug("Token estimation unavailable for %s", model, exc_info=True)
        return None
