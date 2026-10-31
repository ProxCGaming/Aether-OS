import asyncio
import logging
from typing import Any

MAX_VALIDATION_PINGS = 5


from aether_engine.config import (
    CLOUD_PROVIDERS,
    save_config,
)
from aether_engine.providers.discovery import fetch_available_models
from aether_engine.providers.litellm_provider import LiteLLMProvider, validate_api_key
from aether_engine.routing.fallback import (
    get_fallback_candidates,
)
from aether_engine.secrets.storage import SecretStore
from aether_engine.state import engine_state

logger = logging.getLogger("aether_engine.orchestration.core")

async def _build_provider_list_async(store: SecretStore) -> list:
    """Build provider status list for PROVIDER_LIST_RESPONSE with auto-discovery."""
    result = []
    stored = set(store.list_providers())
    cfg = engine_state.user_config
    for p in CLOUD_PROVIDERS:
        models = cfg.provider_models.get(p.name)
        base_url = cfg.custom_base_urls.get(p.name, "")
        if (not models) and (p.name in stored):
            try:
                key = store.load_provider(p.name)
                if key:
                    disc = await fetch_available_models(p.name, key, base_url=base_url)
                    if disc.models:
                        models = disc.models
                        cfg.provider_models[p.name] = disc.models
                        cfg.provider_excluded_models[p.name] = disc.excluded
                        engine_state.model_registry.update_provider_models(p.name, disc.models)
                        save_config(cfg)
            except Exception as e:  # noqa: BLE001 — discovery is best-effort per provider; failure is logged, never fatal
                logger.warning(f"Error discovering models for {p.name}: {e}")

        models = models or []

        result.append({
            "name": p.name,
            "display_name": p.display_name,
            "icon": p.icon,
            "key_url": p.key_url,
            "supports_custom_url": getattr(p, "supports_custom_url", False),
            "base_url": base_url,
            "models": models,
            "excluded_models": cfg.provider_excluded_models.get(p.name, []),
            "has_key": p.name in stored,
            "is_default": cfg.default_provider == p.name,
            "default_model": cfg.default_model if cfg.default_provider == p.name else (models[0] if models else ""),
            "health": engine_state.health_manager.get_state(p.name).status,
        })

    # --- Dynamic custom providers (user-added, not in CLOUD_PROVIDERS) ---
    known_names = {p.name for p in CLOUD_PROVIDERS}
    all_custom = set(cfg.custom_base_urls.keys()) | set(cfg.provider_models.keys()) | stored
    dynamic = all_custom - known_names
    for pname in sorted(dynamic):
        models = cfg.provider_models.get(pname, [])
        base_url = cfg.custom_base_urls.get(pname, "")
        fallback_name = pname.removeprefix("custom_")
        display_name = cfg.custom_provider_names.get(pname, fallback_name.replace("_", " ").title())
        provider_type = cfg.custom_provider_types.get(pname, "openai_compatible")

        if not models and (pname in stored or provider_type == "openai_compatible"):
            try:
                key = store.load_provider(pname) if pname in stored else ""
                if key or provider_type == "openai_compatible":
                    disc = await fetch_available_models(pname, key, base_url=base_url)
                    if disc.models:
                        models = disc.models
                        cfg.provider_models[pname] = disc.models
                        cfg.provider_excluded_models[pname] = disc.excluded
                        engine_state.model_registry.update_provider_models(pname, disc.models)
                        save_config(cfg)
            except Exception as e:  # noqa: BLE001 — discovery is best-effort per provider; failure is logged, never fatal
                logger.warning(f"Error discovering models for {pname}: {e}")

        result.append({
            "name": pname,
            "display_name": display_name,
            "icon": "⊕" if provider_type == "openai_compatible" else "◈",
            "key_url": "",
            "supports_custom_url": True,
            "base_url": base_url,
            "models": models,
            "excluded_models": cfg.provider_excluded_models.get(pname, []),
            "has_key": pname in stored or provider_type == "openai_compatible",
            "is_default": cfg.default_provider == pname,
            "default_model": cfg.default_model if cfg.default_provider == pname else (models[0] if models else ""),
            "health": engine_state.health_manager.get_state(pname).status,
            "provider_type": provider_type,
        })
        
    return result

def _build_provider_list(store: SecretStore) -> list:
    """Synchronous fallback for building provider status list."""
    result = []
    stored = set(store.list_providers())
    cfg = engine_state.user_config
    for p in CLOUD_PROVIDERS:
        models = cfg.provider_models.get(p.name)
        base_url = cfg.custom_base_urls.get(p.name, "")
        if not models:
            models = [m.id for m in engine_state.model_registry.get_models_for_provider(p.name)]
        if not models:
            models = list(p.models)
        result.append({
            "name": p.name,
            "display_name": p.display_name,
            "icon": p.icon,
            "key_url": p.key_url,
            "supports_custom_url": getattr(p, "supports_custom_url", False),
            "base_url": base_url,
            "models": models,
            "excluded_models": cfg.provider_excluded_models.get(p.name, []),
            "has_key": p.name in stored,
            "is_default": cfg.default_provider == p.name,
            "default_model": cfg.default_model if cfg.default_provider == p.name else (models[0] if models else ""),
            "health": engine_state.health_manager.get_state(p.name).status,
        })

    # --- Dynamic custom providers (user-added, not in CLOUD_PROVIDERS) ---
    known_names = {p.name for p in CLOUD_PROVIDERS}
    all_custom = set(cfg.custom_base_urls.keys()) | set(cfg.provider_models.keys()) | stored
    for pname in sorted(all_custom - known_names):
        models = cfg.provider_models.get(pname, [])
        base_url = cfg.custom_base_urls.get(pname, "")
        fallback_name = pname.removeprefix("custom_")
        display_name = cfg.custom_provider_names.get(pname, fallback_name.replace("_", " ").title())
        provider_type = cfg.custom_provider_types.get(pname, "openai_compatible")
        if not models:
            models = [m.id for m in engine_state.model_registry.get_models_for_provider(pname)]
        result.append({
            "name": pname,
            "display_name": display_name,
            "icon": "⊕" if provider_type == "openai_compatible" else "◈",
            "key_url": "",
            "supports_custom_url": True,
            "base_url": base_url,
            "models": models,
            "excluded_models": cfg.provider_excluded_models.get(pname, []),
            "has_key": pname in stored or provider_type == "openai_compatible",
            "is_default": cfg.default_provider == pname,
            "default_model": cfg.default_model if cfg.default_provider == pname else (models[0] if models else ""),
            "health": engine_state.health_manager.get_state(pname).status,
            "provider_type": provider_type,
        })
    return result

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
    cache_key = (provider_name, api_key, base_url or "", persist_models)
    task = _inflight_validations.get(cache_key)
    if task is None or task.done():
        task = asyncio.create_task(
            _validate_provider_key(provider_name, api_key, base_url=base_url, persist_models=persist_models)
        )
        _inflight_validations[cache_key] = task
        task.add_done_callback(lambda _t, k=cache_key: _inflight_validations.pop(k, None))
    return dict(await task)


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
    if not base_url:
        base_url = engine_state.user_config.custom_base_urls.get(provider_name)
    disc = await fetch_available_models(provider_name, api_key, base_url=base_url)
    discovered_models = disc.models

    candidates: list[str] = [m for m in discovered_models if m][:MAX_VALIDATION_PINGS]
    if not candidates:
        # No discovered models: fall back to the provider's default test model.
        candidates = [""]

    is_custom = provider_name in ("custom_openai", "custom") or provider_name.startswith("custom_")

    result: dict[str, Any] = {"status": "network_error", "message": "Validation failed."}
    for model_id in candidates:
        attempt = await validate_api_key(provider_name, api_key, model=model_id, base_url=base_url)
        result = attempt
        if attempt["status"] == "connected":
            if model_id:
                result = dict(attempt)
                result["message"] = f"API key is valid (model: {model_id})."
            break
        # A single model can reject the request (entitlement/quota/model-specific 401)
        # while the key itself is valid, so keep scanning the remaining candidates.

    # For OpenAI-compatible endpoints the /models call is authenticated, so a
    # successful model listing means the key is accepted even if no sampled model
    # responded to the chat ping (e.g. only one model on the plan works).
    if result.get("status") != "connected" and discovered_models and is_custom:
        result = {
            "status": "connected",
            "message": f"API key is valid ({len(discovered_models)} models available).",
            "latency_ms": result.get("latency_ms", 0.0),
        }

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
            engine_state.user_config.provider_models[provider_name] = discovered_models
            engine_state.user_config.provider_excluded_models[provider_name] = disc.excluded
            engine_state.model_registry.update_provider_models(provider_name, discovered_models)
            save_config(engine_state.user_config)
    elif result["status"] == "invalid_key":
        engine_state.health_manager.record_failure(provider_name, result["message"], is_retriable=False)
    else:
        engine_state.health_manager.record_failure(provider_name, result["message"], is_retriable=True)
    return result

def _create_provider_instance(provider_name: str, model: str, secret_store: SecretStore):
    """Factory for LLM provider instances — all providers route through LiteLLM.

    Computes per-node fallback candidates from the health manager and model registry
    so that retriable errors within a single node's call can automatically retry with
    a different provider (Fix 4, Phase 5.1).
    """
    from aether_engine.providers.litellm_provider import (
        FallbackModel,
        resolve_litellm_model,
    )

    api_key = secret_store.load_provider(provider_name)
    base_url = engine_state.user_config.custom_base_urls.get(provider_name)

    fallback_models: list[FallbackModel] = []
    
    if not getattr(engine_state.user_config, "disable_fallbacks", False):
        try:
            # 1. Intra-provider fallbacks: alternative healthy chat models from the SAME provider
            same_provider_models = engine_state.model_registry.get_models_for_provider(provider_name)
            for entry in same_provider_models:
                if entry.id != model and entry.capabilities and "chat" in entry.capabilities:
                    fallback_models.append(FallbackModel(
                        model=resolve_litellm_model(entry.id, provider_name),
                        api_key=api_key,
                        provider_name=provider_name,
                        base_url=base_url,
                    ))
                if len(fallback_models) >= 2:
                    break

            # 2. Cross-provider candidates
            fb_chain = getattr(engine_state.user_config, "fallback_chain", [])
            if fb_chain:
                # If custom fallback chain specified, pick top model from each in order
                for fb_prov in fb_chain:
                    if fb_prov == provider_name or not engine_state.health_manager.is_provider_available(fb_prov):
                        continue
                    fb_models = engine_state.model_registry.get_models_for_provider(fb_prov)
                    for entry in fb_models:
                        if entry.capabilities and "chat" in entry.capabilities:
                            fb_api_key = secret_store.load_provider(fb_prov)
                            fb_base_url = engine_state.user_config.custom_base_urls.get(fb_prov)
                            fallback_models.append(FallbackModel(
                                model=resolve_litellm_model(entry.id, fb_prov),
                                api_key=fb_api_key,
                                provider_name=fb_prov,
                                base_url=fb_base_url,
                            ))
                            break # Just pick one per provider
            else:
                # Default logic using get_fallback_candidates
                candidates = get_fallback_candidates(
                    failed_provider=provider_name,
                    configured_providers=engine_state.configured_providers,
                    health_manager=engine_state.health_manager,
                    registry=engine_state.model_registry,
                )
                for entry in candidates:
                    fb_api_key = secret_store.load_provider(entry.provider)
                    fb_base_url = engine_state.user_config.custom_base_urls.get(entry.provider)
                    fallback_models.append(FallbackModel(
                        model=resolve_litellm_model(entry.id, entry.provider),
                        api_key=fb_api_key,
                        provider_name=entry.provider,
                        base_url=fb_base_url,
                    ))
        except Exception:  # noqa: BLE001, S110 — fallback computation is best-effort; a failure means no fallbacks, not a crash
            pass  # Fallback computation is best-effort

    return LiteLLMProvider(
        api_key=api_key,
        model=model,
        provider_name=provider_name,
        base_url=base_url,
        fallback_models=fallback_models,
        reasoning_effort=getattr(engine_state.user_config, "reasoning_effort", "standard"),
        ttsr_rules=getattr(engine_state.user_config, "ttsr_rules", None),
    )
