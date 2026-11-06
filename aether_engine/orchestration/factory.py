"""Provider instance construction.

Split out of ``orchestration/core.py`` in the Wave 5 craft pass;
see ``orchestration.discovery`` for the split rationale.

``create_tiny_local_aux_provider`` resolves
``_create_provider_instance`` through ``orchestration.core`` at
call time: the suite patches the factory on the core namespace
(and the langgraph nodes lazy-import it from there), so a direct
in-module call would silently bypass the patch surface.
"""

import logging

from aether_engine.config import custom_provider_flavor
from aether_engine.providers.litellm_provider import LiteLLMProvider
from aether_engine.routing.fallback import get_configured_fallback_candidates
from aether_engine.routing.registry import is_local_provider
from aether_engine.routing.startup import is_keyless_custom_provider
from aether_engine.secrets.storage import ProviderNotFoundError, SecretStore
from aether_engine.state import engine_state

logger = logging.getLogger(__name__)


def _load_provider_key(provider_name: str, secret_store: SecretStore, config) -> str:
    """The API key for *provider_name*: its stored secret, or the
    no-key placeholder for providers that legitimately have none.

    No secret is not an error for Ollama (the local daemon) or for a
    custom OpenAI-compatible provider whose saved configuration is
    complete (base URL + catalog, ADR 0013) — E5-F04. The keyless
    decision is driven by the provider's saved configuration shape
    (``is_keyless_custom_provider``), not by a name allowlist. Every
    other provider raises ProviderNotFoundError: a missing key for a
    key-requiring provider is a real configuration error.
    """
    try:
        return secret_store.load_provider(provider_name)
    except ProviderNotFoundError:
        if provider_name == "ollama" or is_keyless_custom_provider(provider_name, config):
            return "sk-no-key-required"
        raise


def _create_provider_instance(provider_name: str, model: str, secret_store: SecretStore, *, local_only: bool = False, require_vision: bool = False, user_config=None):
    """Factory for LLM provider instances — all providers route through LiteLLM.

    Computes per-node fallback candidates from the health manager and model registry
    so that retriable errors within a single node's call can automatically retry with
    a different provider (Fix 4, Phase 5.1).

    Finding M17 constraints (both default off, preserving Epic 5
    behaviour for unconstrained routes): ``local_only`` forbids any
    cross-provider fallback to a cloud provider, and ``require_vision``
    forbids fallbacks to models that cannot see images — together
    they guarantee a call pinned to an explicit local vision model
    can never be retried with the image on a cloud model.

    Finding E5-F02: ``user_config`` is the configuration authoritative
    for this construction — the task's effective (layered) config on
    task paths. The fallback policy (``disable_fallbacks`` /
    ``fallback_chain``), custom base URLs, reasoning effort and TTSR
    rules are all read from it, so the per-call enforcement point
    honours the same policy as the task-level retry factory. Callers
    without a task config omit it and get the global config, exactly
    as before.
    """
    from aether_engine.providers.litellm_provider import (
        FallbackModel,
        resolve_litellm_model,
    )

    config = user_config if user_config is not None else engine_state.user_config

    api_key = _load_provider_key(provider_name, secret_store, config)
    base_url = config.custom_base_urls.get(provider_name)
    # E5-F05: the saved flavour steers construction and every model
    # resolution below (primary + fallback candidates), so an
    # anthropic-flavour custom is dispatched through the Anthropic
    # protocol instead of being silently treated as OpenAI-compatible.
    flavor = custom_provider_flavor(provider_name, config)

    fallback_models: list[FallbackModel] = []

    if not getattr(config, "disable_fallbacks", False):
        try:
            # 1. Intra-provider fallbacks: alternative healthy chat models from the SAME provider
            same_provider_models = engine_state.model_registry.get_models_for_provider(provider_name)
            for entry in same_provider_models:
                if entry.id != model and entry.capabilities and "chat" in entry.capabilities:
                    if require_vision and "vision" not in entry.capabilities:
                        continue
                    fallback_models.append(FallbackModel(
                        model=resolve_litellm_model(
                            entry.id, provider_name, provider_flavor=flavor),
                        api_key=api_key,
                        provider_name=provider_name,
                        base_url=base_url,
                    ))
                if len(fallback_models) >= 2:
                    break

            # 2. Cross-provider candidates — derived by the single
            # fallback-policy authority (routing.fallback), the same
            # function the task-level retry factory consults, from
            # the same config object (E5-F02).
            candidates = get_configured_fallback_candidates(
                failed_provider=provider_name,
                config=config,
                configured_providers=engine_state.configured_providers,
                health_manager=engine_state.health_manager,
                registry=engine_state.model_registry,
                local_only=local_only,
                require_vision=require_vision,
            )
            for entry in candidates:
                try:
                    fb_api_key = _load_provider_key(entry.provider, secret_store, config)
                except ProviderNotFoundError:
                    # N-14: an unkeyed chain entry is skipped, not
                    # chain-fatal — aborting here silently dropped
                    # every later candidate (and the outer handler's
                    # log misdescribed the partial result as "no
                    # fallbacks" at all).
                    logger.warning(
                        "Skipping fallback candidate %s/%s: no stored API key",
                        entry.provider, entry.id)
                    continue
                fb_base_url = config.custom_base_urls.get(entry.provider)
                fallback_models.append(FallbackModel(
                    model=resolve_litellm_model(
                        entry.id, entry.provider,
                        provider_flavor=custom_provider_flavor(entry.provider, config)),
                    api_key=fb_api_key,
                    provider_name=entry.provider,
                    base_url=fb_base_url,
                ))
        except Exception:  # fallback computation is best-effort; a failure means no fallbacks, not a crash
            # E5-F02 (debuggability): the failure is logged, not
            # silently swallowed — a broken fallback computation
            # must be visible when a configured chain "does nothing".
            logger.warning(
                "Fallback computation failed for %s/%s; provider will have no fallbacks",
                provider_name, model, exc_info=True)

    return LiteLLMProvider(
        api_key=api_key,
        model=model,
        provider_name=provider_name,
        base_url=base_url,
        fallback_models=fallback_models,
        reasoning_effort=getattr(config, "reasoning_effort", "standard"),
        ttsr_rules=getattr(config, "ttsr_rules", None),
        provider_flavor=flavor,
    )


def create_tiny_local_aux_provider(user_config, secret_store, *, local_only: bool = False):
    """E5-F19: build a provider over an available tiny local model
    for auxiliary housekeeping (session titles, compaction summaries),
    or None when no such model is available/buildable.

    "Tiny local" is read from the live registry: a model on a local
    provider (``is_local_provider``) carrying both the ``chat`` and
    ``fast`` capabilities — ``fast`` is the registry's own small-model
    class (``_infer_capabilities`` grants it to mini/small/haiku-class
    models). Candidates order by (priority, provider, id), the
    registry's preference order. Construction goes through the same
    factory as an explicit ``provider:::model`` override; any failure
    returns None so the caller falls back to its task/run provider —
    housekeeping must never fail over a missing local model.
    """
    if not secret_store:
        return None
    try:
        candidates = [
            entry for entry in engine_state.model_registry.list_all_models()
            if is_local_provider(entry.provider)
            and "chat" in entry.capabilities
            and "fast" in entry.capabilities
        ]
    except Exception:  # an unreadable registry means no local preference, never a crash
        logger.warning("Tiny-local aux lookup failed; no local preference", exc_info=True)
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda e: (e.priority, e.provider, e.id))
    entry = candidates[0]
    from aether_engine.orchestration.core import _create_provider_instance

    try:
        return _create_provider_instance(
            entry.provider, entry.id, secret_store,
            local_only=local_only, user_config=user_config)
    except Exception as e:  # noqa: BLE001 — an unbuildable local model falls back, never fails the caller
        logger.warning(
            f"Tiny local model {entry.provider}/{entry.id} unusable for "
            f"auxiliary work, falling back: {e}")
        return None
