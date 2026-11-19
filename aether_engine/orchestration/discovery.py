"""Provider model discovery and provider-list building.

Split out of ``orchestration/core.py`` in the Wave 5 craft pass:
discovery — the negative cache, the per-provider fetch/apply rule
and the provider-list builder — is one job, distinct from key
validation (``orchestration.validation``) and provider construction
(``orchestration.factory``). ``orchestration.core`` remains as a
thin re-export shim; its namespace is this package's historical
import and patch surface.

Two disciplines carry over from the monolith:

- The negative-cache state (``_discovery_failures``) lives here
  with its only mutators.
- ``fetch_available_models`` / ``save_config`` are resolved through
  ``orchestration.core`` at call time: the suite patches them on
  the core namespace (``aether_engine.orchestration.core.fetch_available_models``),
  and a direct import here would bind a private copy the patch
  never reaches — the same reason the langgraph nodes lazy-import
  ``_create_provider_instance`` from core.

The llama.cpp router provider follows the same split: the list
builder never probes inline (PROVIDER_LIST answers from cache on
the message loop), so the router probe runs in
:func:`discover_pending_provider_models` — the background half —
and in the ``discover=True`` build, refreshing a module-level
snapshot the builder reads. ``llamacpp`` is a reserved provider
id (it is also the SecretStore id of the router's API key), so
it is excluded from the dynamic-custom derivation: a stored
router key must never mint a phantom custom provider.
"""

import logging
import time
from typing import Any

from aether_engine.config import (
    CLOUD_PROVIDERS,
    anchor_default_model,
    custom_provider_flavor,
)
from aether_engine.secrets.storage import SecretStore
from aether_engine.state import engine_state

logger = logging.getLogger(__name__)

# Negative cache for provider model discovery (RUX-B-02): a provider
# whose discovery just failed is not re-fetched on every list/refresh
# request — each attempt costs a full DISCOVERY_TIMEOUT stall. While
# the last failure is younger than the TTL, discovery for that provider
# is skipped; a successful discovery clears the record immediately.
DISCOVERY_FAILURE_TTL_SECONDS = 60.0

_discovery_failures: dict[str, float] = {}

# The llama.cpp router snapshot: the last probe's model rows
# ([{"id", "loaded"}]) plus the change-detection key, or None
# when the feature is disabled or the router did not answer.
# Written only by _probe_llamacpp_router; read by the provider
# list builder.
_llamacpp_snapshot: dict[str, Any] | None = None


def discovery_recently_failed(provider_name: str) -> bool:
    """True while ``provider_name``'s last discovery failure is within the TTL."""
    failed_at = _discovery_failures.get(provider_name)
    return failed_at is not None and (time.monotonic() - failed_at) < DISCOVERY_FAILURE_TTL_SECONDS


def record_discovery_failure(provider_name: str) -> None:
    _discovery_failures[provider_name] = time.monotonic()


def clear_discovery_failure(provider_name: str) -> None:
    _discovery_failures.pop(provider_name, None)


async def _apply_discovery(store: SecretStore, pname: str, key: str, flavor: str | None = None) -> bool:
    """Fetch + cache one provider's models — the shared discovery rule.

    Both ``_discover_*`` wrappers differ only in how the key is
    loaded and whether a ``provider_flavor`` applies; everything
    from the fetch onward is this one rule: fetch the catalog,
    record failures in the negative cache (RUX-B-02), and on
    success anchor the configured default (B10), replace the
    persisted model list, update the registry and save config.

    Returns True when the provider's model list changed.
    Best-effort: failures are logged, never raised.
    """
    from aether_engine.orchestration.core import fetch_available_models, save_config

    cfg = engine_state.user_config
    try:
        disc = await fetch_available_models(
            pname, key, base_url=cfg.custom_base_urls.get(pname, ""),
            provider_flavor=flavor)
    except Exception as e:  # noqa: BLE001 — discovery is best-effort per provider; failure is logged, never fatal
        record_discovery_failure(pname)
        logger.warning(f"Error discovering models for {pname}: {e}")
        return False
    if not disc.ok:
        record_discovery_failure(pname)
        return False
    clear_discovery_failure(pname)
    if not disc.models:
        return False
    # B10: keep the configured default anchored in the replaced list.
    models = anchor_default_model(cfg, pname, disc.models)
    cfg.provider_models[pname] = models
    cfg.provider_excluded_models[pname] = disc.excluded
    engine_state.model_registry.update_provider_models(pname, models)
    save_config(cfg)
    return True


async def _discover_cloud_provider_models(store: SecretStore, provider) -> bool:
    """Fetch + cache models for one cloud provider with an empty cache.

    Returns True when the provider's model list changed. Best-effort:
    failures are recorded in the negative cache (RUX-B-02) and logged,
    never raised.
    """
    pname = provider.name
    if discovery_recently_failed(pname):
        return False
    try:
        key = store.load_provider(pname)
    except Exception as e:  # noqa: BLE001 — discovery is best-effort per provider; failure is logged, never fatal
        record_discovery_failure(pname)
        logger.warning(f"Error discovering models for {pname}: {e}")
        return False
    if not key:
        return False
    return await _apply_discovery(store, pname, key)


async def _discover_custom_provider_models(store: SecretStore, pname: str, stored: set) -> bool:
    """Fetch + cache models for one dynamic custom provider with an empty cache.

    Same contract as :func:`_discover_cloud_provider_models`.
    """
    cfg = engine_state.user_config
    if discovery_recently_failed(pname):
        return False
    provider_type = cfg.custom_provider_types.get(pname, "openai_compatible")
    try:
        key = store.load_provider(pname) if pname in stored else ""
    except Exception as e:  # noqa: BLE001 — discovery is best-effort per provider; failure is logged, never fatal
        record_discovery_failure(pname)
        logger.warning(f"Error discovering models for {pname}: {e}")
        return False
    if not (key or provider_type == "openai_compatible"):
        return False
    return await _apply_discovery(
        store, pname, key, flavor=custom_provider_flavor(pname, cfg))


async def _probe_llamacpp_router(store: SecretStore) -> bool:
    """Refresh the llama.cpp router snapshot with one probe.

    The snapshot exists only while the feature is enabled AND
    the router answers AND it reports at least one model — a
    down router, a disabled feature and an empty router all
    leave the provider ABSENT from the list, never an empty
    husk. (The local-provider precedent is absence: Ollama has
    no provider-list entry at all; its models live on their own
    surface.) Returns True when the snapshot's visible content
    changed, so the caller pushes a refreshed list. Best-effort:
    a failed probe reads as "router not answering", never raises.
    """
    global _llamacpp_snapshot
    cfg = engine_state.user_config
    rows: list[dict[str, Any]] = []
    if getattr(cfg, "llamacpp_enabled", False):
        try:
            from aether_engine.models.llama_router import (
                LlamaRouterManager,
            )

            manager = LlamaRouterManager.from_config(cfg, secret_store=store)
            status = await manager.router_status()
            if status["running"]:
                listing = await manager.list_models()
                rows = [
                    {"id": m["id"], "loaded": m["loaded"]}
                    for m in listing["models"]
                ]
        except Exception as e:  # noqa: BLE001 — discovery is best-effort; a failed probe means no snapshot
            logger.warning(f"llama.cpp router probe failed: {e}")
            rows = []
    if not rows:
        changed = _llamacpp_snapshot is not None
        _llamacpp_snapshot = None
        return changed
    key = (
        tuple(sorted(r["id"] for r in rows)),
        tuple(sorted(r["id"] for r in rows if r["loaded"] is True)),
    )
    changed = _llamacpp_snapshot is None or _llamacpp_snapshot["key"] != key
    _llamacpp_snapshot = {"key": key, "rows": rows}
    return changed


async def discover_pending_provider_models(store: SecretStore) -> bool:
    """Discover models for every provider whose cache is still empty.

    This is the discovery half of :func:`_build_provider_list_async`,
    split out so PROVIDER_LIST can answer from cache immediately and
    run this in a background task (RUX-B-02). Returns True when at
    least one provider's model list changed.
    """
    stored = set(store.list_providers())
    cfg = engine_state.user_config
    changed = False
    for p in CLOUD_PROVIDERS:
        if not cfg.provider_models.get(p.name) and p.name in stored and await _discover_cloud_provider_models(store, p):
            changed = True
    known_names = {p.name for p in CLOUD_PROVIDERS}
    all_custom = set(cfg.custom_base_urls.keys()) | set(cfg.provider_models.keys()) | stored
    for pname in sorted(all_custom - known_names - {"llamacpp"}):
        provider_type = cfg.custom_provider_types.get(pname, "openai_compatible")
        if not cfg.provider_models.get(pname) and (pname in stored or provider_type == "openai_compatible") and await _discover_custom_provider_models(store, pname, stored):
            changed = True
    if await _probe_llamacpp_router(store):
        changed = True
    return changed


def _provider_model_meta(models: list[str]) -> dict[str, dict[str, Any]]:
    """Per-model picker metadata, exposed for the model pickers (FF3-07).

    Every field already exists engine-side; this only carries it to
    the provider list — no new computation and no invented values:

    - ``capabilities`` / ``cost_input_1m`` / ``cost_output_1m`` /
      ``tier`` come from the curated benchmark cache
      (``capability/benchmark_cache.py``), the same source the
      capability classifier reads.
    - ``unusable`` is the engine's own learned-unusable verdict —
      :func:`pin_targets_unusable_model`, the exact predicate the
      provider resolvers consult before honouring a model pin — so
      a picker badge can never disagree with a pin refusal.

    Models with neither source are absent from the result: the UI
    renders missing metadata as absent, never as a guess. Metadata
    must never break the provider list, so lookup failures degrade
    to whatever was gathered (or an empty dict).
    """
    meta: dict[str, dict[str, Any]] = {}
    try:
        from aether_engine.capability.benchmark_cache import (
            get_benchmark_for_model,
        )
    except Exception:  # noqa: BLE001 — metadata is best-effort
        get_benchmark_for_model = None
    try:
        from aether_engine.langgraph.model_routing import (
            pin_targets_unusable_model,
        )
    except Exception:  # noqa: BLE001 — metadata is best-effort
        pin_targets_unusable_model = None
    for model in models:
        entry: dict[str, Any] = {}
        if get_benchmark_for_model is not None:
            try:
                bench = get_benchmark_for_model(model)
            except Exception:  # noqa: BLE001 — metadata is best-effort
                bench = None
            if bench:
                if bench.get("capabilities"):
                    entry["capabilities"] = list(bench["capabilities"])
                for key in ("cost_input_1m", "cost_output_1m", "tier"):
                    if bench.get(key) is not None:
                        entry[key] = bench[key]
        if pin_targets_unusable_model is not None:
            try:
                if pin_targets_unusable_model(model):
                    entry["unusable"] = True
            except Exception:  # noqa: BLE001 — metadata is best-effort
                pass
        if entry:
            meta[model] = entry
    return meta


async def _build_provider_list_async(store: SecretStore, discover: bool = True) -> list:
    """Build provider status list for PROVIDER_LIST_RESPONSE with auto-discovery.

    With ``discover=False`` the list is built from the model cache
    only — no network fetches — so the caller can answer immediately
    and run :func:`discover_pending_provider_models` off the message
    loop instead (RUX-B-02).
    """
    result = []
    stored = set(store.list_providers())
    cfg = engine_state.user_config
    for p in CLOUD_PROVIDERS:
        models = cfg.provider_models.get(p.name)
        base_url = cfg.custom_base_urls.get(p.name, "")
        if discover and (not models) and (p.name in stored):
            await _discover_cloud_provider_models(store, p)
            models = cfg.provider_models.get(p.name)

        models = models or []

        result.append({
            "name": p.name,
            "display_name": p.display_name,
            "icon": p.icon,
            "key_url": p.key_url,
            "supports_custom_url": getattr(p, "supports_custom_url", False),
            "base_url": base_url,
            "models": models,
            "model_meta": _provider_model_meta(models),
            "excluded_models": cfg.provider_excluded_models.get(p.name, []),
            "has_key": p.name in stored,
            "is_default": cfg.default_provider == p.name,
            "default_model": cfg.default_model if cfg.default_provider == p.name else (models[0] if models else ""),
            "health": engine_state.health_manager.get_state(p.name).status,
        })

    # --- Dynamic custom providers (user-added, not in CLOUD_PROVIDERS) ---
    # "llamacpp" is reserved (the router provider + its SecretStore
    # key id) and never a dynamic custom provider.
    known_names = {p.name for p in CLOUD_PROVIDERS}
    all_custom = set(cfg.custom_base_urls.keys()) | set(cfg.provider_models.keys()) | stored
    dynamic = all_custom - known_names - {"llamacpp"}
    for pname in sorted(dynamic):
        models = cfg.provider_models.get(pname, [])
        base_url = cfg.custom_base_urls.get(pname, "")
        fallback_name = pname.removeprefix("custom_")
        display_name = cfg.custom_provider_names.get(pname, fallback_name.replace("_", " ").title())
        provider_type = cfg.custom_provider_types.get(pname, "openai_compatible")

        if discover and not models and (pname in stored or provider_type == "openai_compatible"):
            await _discover_custom_provider_models(store, pname, stored)
            models = cfg.provider_models.get(pname, [])

        result.append({
            "name": pname,
            "display_name": display_name,
            "icon": "⊕" if provider_type == "openai_compatible" else "◈",
            "key_url": "",
            "supports_custom_url": True,
            "base_url": base_url,
            "models": models,
            "model_meta": _provider_model_meta(models),
            "excluded_models": cfg.provider_excluded_models.get(pname, []),
            "has_key": pname in stored or provider_type == "openai_compatible",
            "is_default": cfg.default_provider == pname,
            "default_model": cfg.default_model if cfg.default_provider == pname else (models[0] if models else ""),
            "health": engine_state.health_manager.get_state(pname).status,
            "provider_type": provider_type,
        })

    # --- llama.cpp router provider (local serving) ---
    # Present only while the feature is enabled and a probe has
    # seen the router answering with models (the snapshot —
    # refreshed by the background discovery pass, and here when
    # this build is allowed to discover). Its models are the
    # router's own, with the Wave 4 model_meta shape plus the
    # router-reported Loaded state per model.
    if discover and getattr(cfg, "llamacpp_enabled", False):
        await _probe_llamacpp_router(store)
    if getattr(cfg, "llamacpp_enabled", False) and _llamacpp_snapshot is not None:
        rows = _llamacpp_snapshot["rows"]
        models = [r["id"] for r in rows]
        meta = _provider_model_meta(models)
        for r in rows:
            if r["loaded"] is not None:
                meta.setdefault(r["id"], {})["loaded"] = r["loaded"]
        result.append({
            "name": "llamacpp",
            "display_name": "llama.cpp",
            "icon": "◑",
            "key_url": "",
            "supports_custom_url": False,
            "base_url": cfg.llamacpp_router_url,
            "models": models,
            "model_meta": meta,
            "excluded_models": [],
            "has_key": "llamacpp" in stored,
            "is_default": cfg.default_provider == "llamacpp",
            "default_model": cfg.default_model if cfg.default_provider == "llamacpp" else (models[0] if models else ""),
            "health": "healthy",
            "provider_type": "openai_compatible",
        })

    return result
