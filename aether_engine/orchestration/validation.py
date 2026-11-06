"""Provider key validation — the Settings "Test" button path.

Split out of ``orchestration/core.py`` in the Wave 5 craft pass;
see ``orchestration.discovery`` for the split rationale. The
in-flight dedupe state (``_inflight_validations``) lives here with
its only users.

Patch-surface discipline (same as discovery): the suite patches
``_validate_provider_key``, ``_validate_provider_key_inner``,
``fetch_available_models``, ``validate_api_key`` and ``save_config``
on the ``orchestration.core`` namespace, so those names are
resolved through core at call time rather than bound by a direct
import here — a private copy would silently bypass the patches.
"""

import asyncio
import logging
from typing import Any

from aether_engine.config import anchor_default_model, custom_provider_flavor
from aether_engine.state import engine_state

logger = logging.getLogger(__name__)

MAX_VALIDATION_PINGS = 5


async def _validate_provider_key(
    provider_name: str,
    api_key: str,
    base_url: str | None = None,
    persist_models: bool = True,
    budget_s: float = 30.0,
) -> dict:
    """Validate a provider key, hard-capped at ``budget_s`` seconds.

    Discovery plus up to MAX_VALIDATION_PINGS sequential pings could
    previously take ~60s; a caller that awaited it inline (the Test
    button) wedged for the whole scan. Past the budget the caller gets
    an honest timeout result instead of silence.
    """
    from aether_engine.orchestration.core import _validate_provider_key_inner

    try:
        return await asyncio.wait_for(
            _validate_provider_key_inner(provider_name, api_key, base_url=base_url, persist_models=persist_models),
            timeout=budget_s,
        )
    except TimeoutError:
        logger.warning(f"Provider validation for {provider_name} exceeded the {budget_s:.0f}s budget")
        return {
            "status": "network_error",
            "message": f"Validation timed out after {budget_s:.0f}s — the provider did not answer in time. Try again.",
            "models": [],
            "excluded_models": [],
            "latency_ms": budget_s * 1000.0,
        }


# In-flight validations, keyed by (provider, key, base_url, persist):
# duplicate Test clicks attach to the SAME run instead of queueing a
# second full discovery+ping scan behind the first.
_inflight_validations: dict[tuple, asyncio.Task] = {}


async def validate_provider_key_shared(
    provider_name: str,
    api_key: str,
    base_url: str | None = None,
    persist_models: bool = False,
) -> dict:
    """``_validate_provider_key`` with in-flight dedupe.

    A repeat request for the identical provider+key while one is
    running awaits the running task; every waiter gets a copy of the
    same result. The cache entry is dropped when the run finishes, so
    a later Test always re-validates for real.
    """
    from aether_engine.orchestration.core import _validate_provider_key

    cache_key = (provider_name, api_key, base_url or "", persist_models)
    task = _inflight_validations.get(cache_key)
    if task is None or task.done():
        task = asyncio.create_task(
            _validate_provider_key(provider_name, api_key, base_url=base_url, persist_models=persist_models)
        )
        _inflight_validations[cache_key] = task

        def _drop_finished(_t, k=cache_key, t=task):
            # N-23: evict only OUR task — a newer validation may
            # have been registered for the same key after this one
            # finished, and an unconditional pop would evict it.
            if _inflight_validations.get(k) is t:
                _inflight_validations.pop(k, None)

        task.add_done_callback(_drop_finished)
    # N-23: shield the shared run — cancelling one waiter (socket
    # drop, Test-panel timeout) must not cancel the validation every
    # other deduped waiter is attached to.
    return dict(await asyncio.shield(task))


async def _validate_provider_key_inner(
    provider_name: str,
    api_key: str,
    base_url: str | None = None,
    persist_models: bool = True,
) -> dict:
    """Validate a provider key and discover its available models in real-time.

    Discovery can return many models while only a subset are usable with the
    current key/quota, so the validation ping walks through several candidate
    models and stops at the first one that responds. This avoids reporting a
    false failure caused by pinging a single quota-exhausted model.

    ``persist_models`` is False for unsaved/template providers so that merely
    testing a key in the panel does not leave discovery data behind in config.
    """
    from aether_engine.orchestration.core import (
        fetch_available_models,
        save_config,
        validate_api_key,
    )

    if not base_url:
        base_url = engine_state.user_config.custom_base_urls.get(provider_name)
    # E5-F05: discovery and the validation ping speak the provider's
    # saved protocol — an anthropic-flavour custom is discovered and
    # pinged as Anthropic, not as an OpenAI-compatible endpoint.
    flavor = custom_provider_flavor(provider_name, engine_state.user_config)
    disc = await fetch_available_models(
        provider_name, api_key, base_url=base_url, provider_flavor=flavor)
    discovered_models = disc.models

    candidates: list[str] = [m for m in discovered_models if m][:MAX_VALIDATION_PINGS]
    if not candidates:
        # No discovered models: fall back to the provider's default test model.
        candidates = [""]

    result: dict[str, Any] = {"status": "network_error", "message": "Validation failed."}
    for model_id in candidates:
        attempt = await validate_api_key(
            provider_name, api_key, model=model_id, base_url=base_url,
            provider_flavor=flavor)
        result = attempt
        if attempt["status"] == "connected":
            if model_id:
                result = dict(attempt)
                result["message"] = f"API key is valid (model: {model_id})."
            break
        # A single model can reject the request (entitlement/quota/model-specific 401)
        # while the key itself is valid, so keep scanning the remaining candidates.

    # E5-F06: the ping verdict governs key validity — full stop. A custom
    # endpoint's /models listing is often public (served without auth), so
    # a successful catalog fetch proves nothing about the key and must
    # never convert a failed ping (explicit invalid_key, or a network
    # error) into "connected". The catalog's legitimate contribution is
    # availability info only: the model list attached below, and the
    # persisted models when the ping genuinely connects.

    # Attach discovered models (and the excluded ones, for the provider card
    # "View all" expander) to the result dictionary.
    result["models"] = discovered_models
    result["excluded_models"] = disc.excluded
    if disc.error is not None:
        result["discovery_error"] = disc.error.to_dict()
        # The fetch failed AND no ping connected: surface the real fetch
        # error (with HTTP code) instead of a generic ping failure.
        if result.get("status") != "connected":
            code_part = f"HTTP {disc.error.code}" if disc.error.code else "no response"
            result = {
                "status": "network_error",
                "message": f"Model list fetch failed ({code_part}): {disc.error.message}",
                "models": discovered_models,
                "excluded_models": disc.excluded,
                "discovery_error": disc.error.to_dict(),
            }

    if result["status"] == "connected":
        latency = result.get("latency_ms", 100.0)
        engine_state.health_manager.record_success(provider_name, latency)
        if discovered_models and persist_models:
            # B10: keep the configured default anchored in the
            # replaced list. (The response payload above keeps the
            # raw discovery result; only the persisted list anchors.)
            anchored_models = anchor_default_model(
                engine_state.user_config, provider_name, discovered_models)
            engine_state.user_config.provider_models[provider_name] = anchored_models
            engine_state.user_config.provider_excluded_models[provider_name] = disc.excluded
            engine_state.model_registry.update_provider_models(provider_name, anchored_models)
            save_config(engine_state.user_config)
    elif result["status"] == "invalid_key":
        engine_state.health_manager.record_failure(provider_name, result["message"], is_retriable=False)
    else:
        engine_state.health_manager.record_failure(provider_name, result["message"], is_retriable=True)
    return result
