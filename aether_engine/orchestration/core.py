import asyncio
import json
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
import time
from typing import Any, Dict, List, Optional, Set
from pathlib import Path
from pydantic import BaseModel

MAX_VALIDATION_PINGS = 5

from fastapi import APIRouter, FastAPI, Query, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse

from aether_common.auth import (
    DEFAULT_TOKEN_PATH,
    generate_token,
    verify_token,
    write_token,
)
from aether_common.contracts import (
    SCHEMA_VERSION,
    Event,
    EventType,
    InvalidEventPayloadError,
    SchemaVersionMismatchError,
    TaskState,
)
from aether_engine.audit import AuditLogger
from aether_engine.config import (
    CLOUD_PROVIDERS,
    PROVIDER_MAP,
    UserConfig,
    load_config,
    save_config,
)
from aether_engine.models.local_models import OllamaManager
from aether_engine.langgraph.executor import run_langgraph_task, resume_langgraph_task, get_pending_approvals
from aether_engine.providers.discovery import fetch_available_models
from aether_engine.providers.base import ProviderError
from aether_engine.providers.litellm_provider import LiteLLMProvider, validate_api_key
from aether_engine.routing.fallback import (
    classify_provider_error,
    get_fallback_candidates,
    is_retriable_error,
)
from aether_engine.routing.health import (
    GLOBAL_HEALTH_MANAGER,
    ProviderHealthManager,
    ProviderHealthState,
)
from aether_engine.routing.policy import (
    GLOBAL_ROUTING_POLICY,
    RoutingDecision,
    RoutingPolicy,
    infer_task_requirements,
)
from aether_engine.routing.registry import (
    GLOBAL_MODEL_REGISTRY,
    ModelEntry,
    ModelRegistry,
)
from aether_engine.routing.startup import (
    StartupValidationResult,
    validate_startup_configuration,
)
from aether_engine.validation.diagnostics import run_environment_diagnostics
from aether_engine.scheduler.capability_jobs import CapabilitySchedulerManager
from aether_engine.secrets.dpapi import SecretDecryptionError
from aether_engine.secrets.storage import ProviderNotFoundError, SecretStore
from aether_engine.tools.registry import ToolRegistry, create_current_time_tool, create_file_tools, create_web_tools
from aether_engine.routing.task_class_router import infer_task_class
from aether_engine.workers.job_object import WorkerJobObject
from aether_engine.memory import session_store

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
                    disc_models = await fetch_available_models(p.name, key, base_url=base_url)
                    if disc_models:
                        models = disc_models
                        cfg.provider_models[p.name] = disc_models
                        engine_state.model_registry.update_provider_models(p.name, disc_models)
                        save_config(cfg)
            except Exception as e:
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
        fallback_name = pname[7:] if pname.startswith("custom_") else pname
        display_name = cfg.custom_provider_names.get(pname, fallback_name.replace("_", " ").title())
        provider_type = cfg.custom_provider_types.get(pname, "openai_compatible")

        if not models and (pname in stored or provider_type == "openai_compatible"):
            try:
                key = store.load_provider(pname) if pname in stored else ""
                if key or provider_type == "openai_compatible":
                    disc_models = await fetch_available_models(pname, key, base_url=base_url)
                    if disc_models:
                        models = disc_models
                        cfg.provider_models[pname] = disc_models
                        engine_state.model_registry.update_provider_models(pname, disc_models)
                        save_config(cfg)
            except Exception as e:
                logger.warning(f"Error discovering models for {pname}: {e}")

        result.append({
            "name": pname,
            "display_name": display_name,
            "icon": "⊕" if provider_type == "openai_compatible" else "◈",
            "key_url": "",
            "supports_custom_url": True,
            "base_url": base_url,
            "models": models,
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
        fallback_name = pname[7:] if pname.startswith("custom_") else pname
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
    base_url: Optional[str] = None,
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
    discovered_models = await fetch_available_models(provider_name, api_key, base_url=base_url)

    candidates: List[str] = [m for m in discovered_models if m][:MAX_VALIDATION_PINGS]
    if not candidates:
        # No discovered models: fall back to the provider's default test model.
        candidates = [""]

    is_custom = provider_name in ("custom_openai", "custom") or provider_name.startswith("custom_")

    result: Dict[str, Any] = {"status": "network_error", "message": "Validation failed."}
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

    # Attach discovered models to the result dictionary
    result["models"] = discovered_models

    if result["status"] == "connected":
        latency = result.get("latency_ms", 100.0)
        engine_state.health_manager.record_success(provider_name, latency)
        if discovered_models and persist_models:
            engine_state.user_config.provider_models[provider_name] = discovered_models
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
    from aether_engine.providers.litellm_provider import resolve_litellm_model, FallbackModel

    api_key = secret_store.load_provider(provider_name)
    base_url = engine_state.user_config.custom_base_urls.get(provider_name)

    fallback_models: List[FallbackModel] = []
    
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
        except Exception:
            pass  # Fallback computation is best-effort

    return LiteLLMProvider(
        api_key=api_key,
        model=model,
        provider_name=provider_name,
        base_url=base_url,
        fallback_models=fallback_models,
    )
