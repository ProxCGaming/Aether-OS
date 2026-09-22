"""FastAPI Engine application with authenticated loopback WebSocket, provider health, intelligent routing, safe fallbacks, local model task downloads, and capability check scheduling."""
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

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect, status
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
from aether_engine.scheduler.capability_jobs import CapabilitySchedulerManager
from aether_engine.secrets.dpapi import SecretDecryptionError
from aether_engine.secrets.storage import ProviderNotFoundError, SecretStore
from aether_engine.tools.registry import ToolRegistry, create_current_time_tool, create_file_tools, create_web_tools
from aether_engine.routing.task_class_router import infer_task_class
from aether_engine.workers.job_object import WorkerJobObject
from aether_engine.memory import session_store

logger = logging.getLogger("aether_engine.app")

pending_plugin_installs = {}


# ---------------------------------------------------------------------------
# Engine state (singleton per process)
# ---------------------------------------------------------------------------
class EngineState:
    def __init__(self):
        self.token_path = DEFAULT_TOKEN_PATH
        self.audit_logger = AuditLogger()
        self.auth_token = ""
        self.user_config = UserConfig()
        self.health_manager = GLOBAL_HEALTH_MANAGER
        self.model_registry = GLOBAL_MODEL_REGISTRY
        self.routing_policy = GLOBAL_ROUTING_POLICY
        self.scheduler_manager = CapabilitySchedulerManager()
        self.ollama_manager = OllamaManager()
        from aether_engine.plugins.installer import PluginInstaller
        self.plugin_installer = PluginInstaller(Path.home() / ".aether" / "plugins")
        self.active_provider: str = "google_gemini"
        self.active_model: str = "gemini-2.5-flash"
        self.configured_providers: List[str] = []
        self.disabled_nodes: Set[str] = set()

    def initialize(self) -> str:
        self.auth_token = generate_token()
        write_token(self.auth_token, self.token_path)
        self.user_config = load_config()

        # Register persisted discovered models into the model registry
        for prov_name, prov_models in self.user_config.provider_models.items():
            if prov_models:
                self.model_registry.update_provider_models(prov_name, prov_models)

        # Run startup consistency validation
        secret_store = SecretStore()
        val_res = validate_startup_configuration(
            user_config=self.user_config,
            secret_store=secret_store,
            health_manager=self.health_manager,
            registry=self.model_registry,
        )
        self.active_provider = val_res.active_provider
        self.active_model = val_res.active_model
        self.configured_providers = val_res.configured_providers

        if val_res.was_fallback:
            logger.warning(f"Startup fallback applied: {val_res.fallback_reason}")
            self.audit_logger.log_event("STARTUP_FALLBACK", {"reason": val_res.fallback_reason})

        # Start background capability scheduler with startup misfire recovery
        self.scheduler_manager.start()

        self.audit_logger.log_event("ENGINE_STARTED", {
            "token_path": str(self.token_path),
            "active_provider": self.active_provider,
            "active_model": self.active_model,
        })
        return self.auth_token

    def initialize_auth(self) -> str:
        """Alias for backward compatibility."""
        return self.initialize()

    def shutdown(self):
        self.scheduler_manager.stop()


engine_state = EngineState()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    engine_state.initialize()
    logger.info(
        f"AETHER Engine initialized. Active: {engine_state.active_provider}/{engine_state.active_model}. "
        f"Auth token written to {engine_state.token_path}"
    )

    # Run background model discovery for configured providers on startup
    secret_store = SecretStore()
    stored = set(secret_store.list_providers())
    cfg = engine_state.user_config
    all_providers = set(stored)
    for p in cfg.custom_provider_types:
        if cfg.custom_provider_types[p] == "openai_compatible":
            all_providers.add(p)
    all_providers.add("custom_openai")

    for pname in all_providers:
        try:
            key = secret_store.load_provider(pname) if pname in stored else ""
            is_custom = pname == "custom_openai" or cfg.custom_provider_types.get(pname) == "openai_compatible"
            if key or is_custom:
                base_url = cfg.custom_base_urls.get(pname)
                disc_models = await fetch_available_models(pname, key, base_url=base_url)
                if disc_models:
                    cfg.provider_models[pname] = disc_models
                    engine_state.model_registry.update_provider_models(pname, disc_models)
                    if cfg.default_provider == pname:
                        if cfg.default_model not in disc_models or "lite" in cfg.default_model:
                            cfg.default_model = disc_models[0]
                            engine_state.active_model = disc_models[0]
                    save_config(cfg)
        except Exception as e:
            logger.warning(f"Failed startup model discovery for {pname}: {e}")

    yield
    engine_state.shutdown()
    engine_state.audit_logger.log_event("ENGINE_STOPPED")
    logger.info("AETHER Engine stopped.")


app = FastAPI(title="AETHER Engine", version="0.3.5", lifespan=lifespan)


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "schema_version": SCHEMA_VERSION})


# ---------------------------------------------------------------------------
# Provider management helpers
# ---------------------------------------------------------------------------
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


MAX_VALIDATION_PINGS = 20


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

    # Build fallback model list for per-node retry (uses existing get_fallback_candidates)
    fallback_models: List[FallbackModel] = []
    try:
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



async def _execute_tool_in_worker(tool_name: str, args: Dict[str, Any], task_class: str) -> Any:
    """Run an approved risky tool via the shared sandbox worker. Thin wrapper that injects engine_state context."""
    from aether_engine.workers.sandbox import execute_tool_in_worker
    return await execute_tool_in_worker(
        tool_name=tool_name,
        args=args,
        task_class=task_class,
        audit_logger=engine_state.audit_logger,
        task_classes=engine_state.user_config.task_classes,
    )



# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------
def _extract_token(ws: WebSocket, query_token: Optional[str]) -> Optional[str]:
    if query_token:
        return query_token
    auth = ws.headers.get("authorization", "")
    return auth[7:].strip() if auth.startswith("Bearer ") else None


@app.websocket("/ws/tasks")
async def ws_tasks(ws: WebSocket, token: Optional[str] = Query(default=None)):
    ip = ws.client.host if ws.client else "unknown"
    port = ws.client.port if ws.client else None

    extracted = _extract_token(ws, token)
    if not extracted or not verify_token(extracted, engine_state.auth_token):
        engine_state.audit_logger.log_connection(False, ip, port, "Unauthorized")
        logger.warning(f"Rejected unauthenticated connection from {ip}:{port}")
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await ws.accept()
    engine_state.audit_logger.log_connection(True, ip, port, "Authenticated successfully")
    logger.info(f"Accepted authenticated connection from {ip}:{port}")

    # Send Hello
    await ws.send_text(
        Event(
            type=EventType.HELLO,
            payload={
                "message": "AETHER Engine Ready",
                "version": "0.3.5",
                "active_provider": engine_state.active_provider,
                "active_model": engine_state.active_model,
                "health": engine_state.health_manager.get_all_statuses(),
            },
        ).to_json()
    )

    # ── Recover Pending Approvals (Checkpoint Durability) ──
    try:
        pending_approvals = await get_pending_approvals()
        for p in pending_approvals:
            await ws.send_text(Event(
                type=EventType.TOOL_APPROVAL_REQUEST,
                request_id=p["thread_id"],
                payload={
                    "tool_name": p["tool_name"],
                    "args": p["args"],
                    "task_id": p["task_id"],
                    "approval_key": p["thread_id"],
                    "task_class": p["task_class"],
                    "workspace_roots": engine_state.user_config.workspace_roots or [str(Path.home() / "AetherWorkspace")],
                },
            ).to_json())
    except Exception as e:
        logger.warning(f"Error fetching pending approvals: {e}")

    task_runner: Optional[asyncio.Task] = None
    active_downloads: Dict[str, asyncio.Task] = {}
    secret_store = SecretStore()

    async def execute_task_generator(generator, p_name: str, p_model: str, req_id: str, session_id: Optional[str] = None):
        try:
            fallback_attempted = False
            primary_failed = False
            failure_event = None

            async for ev in generator:
                if ev.type == EventType.TASK_CREATED:
                    ev.payload["routing"] = {
                        "provider": p_name,
                        "model": p_model,
                        "reason": "Default Routing",
                        "capabilities": [],
                    }
                    engine_state.audit_logger.log_task_transition(
                        ev.payload.get("task_id", "unknown"),
                        TaskState.PENDING,
                        TaskState.RUNNING,
                        ev.payload,
                    )
                elif ev.type in (EventType.TASK_COMPLETED, EventType.TASK_CANCELLED, EventType.TASK_FAILED):
                    to = {
                        EventType.TASK_COMPLETED: TaskState.SUCCEEDED,
                        EventType.TASK_CANCELLED: TaskState.CANCELLED,
                        EventType.TASK_FAILED: TaskState.FAILED,
                    }[ev.type]
                    engine_state.audit_logger.log_task_transition(
                        ev.payload.get("task_id", "unknown"),
                        TaskState.RUNNING,
                        to,
                        ev.payload,
                    )

                if ev.type == EventType.TASK_FAILED and not fallback_attempted and ev.payload.get("retriable"):
                    primary_failed = True
                    failure_event = ev
                    break

                if ev.type == EventType.TASK_COMPLETED:
                    latency = ev.payload.get("latency_ms", 100.0)
                    engine_state.health_manager.record_success(p_name, latency)
                    if session_id and ev.payload.get("response"):
                        # Save the final response from assistant
                        try:
                            session_store.add_message(session_id, "assistant", ev.payload["response"])
                        except Exception as e:
                            logger.error(f"Failed to save session message: {e}")

                if ev.type == EventType.TOOL_APPROVAL_REQUEST:
                    engine_state.audit_logger.log_event("APPROVAL_REQUESTED", {
                        "tool_name": ev.payload.get("tool_name"),
                        "args": ev.payload.get("args"),
                        "task_id": ev.payload.get("task_id", "unknown"),
                    })

                await ws.send_text(ev.to_json())

            if primary_failed and failure_event:
                err_msg = failure_event.payload.get("error", "Unknown error")
                engine_state.health_manager.record_failure(p_name, err_msg, is_retriable=True)
                await ws.send_text(failure_event.to_json())
                return
        except asyncio.CancelledError:
            engine_state.audit_logger.log_event("TASK_CANCELLED", {"task_id": "unknown"})
            raise
        except Exception as e:
            logger.error(f"Task generator failed with exception: {e}", exc_info=True)
            try:
                await ws.send_text(Event(
                    type=EventType.ERROR,
                    request_id=req_id,
                    payload={"error": f"Internal engine error: {str(e)}"}
                ).to_json())
            except Exception:
                pass

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = Event.from_json(raw)
            except (SchemaVersionMismatchError, InvalidEventPayloadError) as e:
                await ws.send_text(Event(type=EventType.ERROR, payload={"error": str(e)}).to_json())
                continue

            req_id = msg.request_id

            # -----------------------------------------------------------
            # Task lifecycle events
            # -----------------------------------------------------------
            if msg.type == EventType.START_TASK:
                if task_runner and not task_runner.done():
                    task_runner.cancel()
                    try:
                        await task_runner
                    except asyncio.CancelledError:
                        pass

                prompt = msg.payload.get("prompt", "").strip() or "Say hello"
                cfg = engine_state.user_config
                requested_model = msg.payload.get("model")
                session_id = msg.payload.get("session_id")
                
                if not session_id:
                    # Create new session if none provided
                    title = prompt[:30] + "..." if len(prompt) > 30 else prompt
                    session_id = session_store.create_session(title)
                else:
                    sess_info = session_store.get_session(session_id)
                    if sess_info and sess_info.get("title") in ("New Conversation", "New Chat") and not sess_info.get("messages"):
                        title = prompt[:30] + "..." if len(prompt) > 30 else prompt
                        session_store.update_session_title(session_id, title)
                        try:
                            await ws.send_text(Event(
                                type=EventType.SESSION_LIST_RESPONSE,
                                request_id=req_id,
                                payload={"sessions": session_store.list_sessions()}
                            ).to_json())
                        except Exception:
                            pass
                try:
                    session_store.add_message(session_id, "user", prompt)
                except Exception as e:
                    logger.error(f"Failed to save user message: {e}")

                stored_providers = set(secret_store.list_providers())
                engine_state.configured_providers = list(stored_providers)

                decision: RoutingDecision = engine_state.routing_policy.select_route(
                    prompt=prompt,
                    configured_providers=engine_state.configured_providers,
                    health_manager=engine_state.health_manager,
                    user_default_provider=cfg.default_provider,
                    user_default_model=cfg.default_model,
                    requested_model=requested_model,
                )

                try:
                    provider_instance = _create_provider_instance(
                        decision.provider, decision.model, secret_store
                    )
                except ProviderNotFoundError:
                    await ws.send_text(Event(
                        type=EventType.TASK_FAILED,
                        request_id=req_id,
                        payload={
                            "task_id": "none", "retriable": False, "state": TaskState.FAILED.value,
                            "error": f"API key not found for {decision.provider}. Please add your key in Settings → Models.",
                        },
                    ).to_json())
                    continue
                except SecretDecryptionError as e:
                    await ws.send_text(Event(
                        type=EventType.TASK_FAILED,
                        request_id=req_id,
                        payload={
                            "task_id": "none", "retriable": False, "state": TaskState.FAILED.value,
                            "error": f"Failed to decrypt API key: {e}. Please re-enter your key in Settings → Models.",
                        },
                    ).to_json())
                    continue

                tools = ToolRegistry()
                tools.register(create_current_time_tool())
                
                reqs = infer_task_requirements(prompt)
                
                # Only give file/shell tools if it's a coding task or explicitly requested
                if reqs.wants_code or (requested_model is not None):
                    for tool in create_file_tools():
                        tools.register(tool)
                # Always provide web search as a safe fallback
                for tool in create_web_tools():
                    tools.register(tool)

                workspace_roots = engine_state.user_config.workspace_roots or [str(Path.home() / "AetherWorkspace")]

                task_generator = run_langgraph_task(
                    prompt=prompt,
                    provider=provider_instance,
                    tools=tools,
                    request_id=req_id,
                    approval_handler=None,
                    workspace_roots=workspace_roots,
                    disabled_nodes=engine_state.disabled_nodes,
                    session_id=session_id,
                )

                task_runner = asyncio.create_task(
                    execute_task_generator(task_generator, decision.provider, decision.model, req_id, session_id=session_id)
                )

            elif msg.type == EventType.CANCEL_TASK:
                if task_runner and not task_runner.done():
                    task_runner.cancel()
                    logger.info("Cancellation requested for active task runner.")
                    await ws.send_text(Event(
                        type=EventType.TASK_CANCELLED,
                        request_id=req_id,
                        payload={"task_id": "unknown", "state": TaskState.CANCELLED.value},
                    ).to_json())
                else:
                    await ws.send_text(Event(
                        type=EventType.ERROR,
                        request_id=req_id,
                        payload={"error": "No active task to cancel"},
                    ).to_json())

            elif msg.type == EventType.PING:
                await ws.send_text(Event(
                    type=EventType.PONG,
                    request_id=req_id,
                    payload={"echo_ts": msg.ts},
                ).to_json())

            elif msg.type in (EventType.TOOL_APPROVAL_GRANTED, EventType.TOOL_APPROVAL_REJECTED):
                approval_key = msg.payload.get("approval_key")
                is_approved = (msg.type == EventType.TOOL_APPROVAL_GRANTED)
                
                if is_approved:
                    engine_state.audit_logger.log_event("APPROVAL_GRANTED", {"approval_key": approval_key})
                else:
                    engine_state.audit_logger.log_event("APPROVAL_REJECTED", {"approval_key": approval_key})
                
                # We need to construct a provider instance for the resume generator.
                # In a real system, provider state would be part of the task metadata or checkpointer.
                # Here we recreate the active provider instance.
                try:
                    provider_instance = _create_provider_instance(
                        engine_state.active_provider, engine_state.active_model, secret_store
                    )
                except Exception:
                    continue # Ignore if provider cannot be instantiated

                tools = ToolRegistry()
                tools.register(create_current_time_tool())
                for tool in create_file_tools(): tools.register(tool)
                for tool in create_web_tools(): tools.register(tool)

                workspace_roots = engine_state.user_config.workspace_roots or [str(Path.home() / "AetherWorkspace")]

                # Pass the approved decision back into LangGraph
                resume_gen = resume_langgraph_task(
                    thread_id=approval_key,
                    approved=is_approved,
                    provider=provider_instance,
                    tools=tools,
                    workspace_roots=workspace_roots,
                    disabled_nodes=engine_state.disabled_nodes,
                )

                if task_runner and not task_runner.done():
                    task_runner.cancel()
                    
                req_id = msg.request_id or "resume"
                task_runner = asyncio.create_task(
                    execute_task_generator(resume_gen, engine_state.active_provider, engine_state.active_model, req_id, session_id=None)
                )

            # -----------------------------------------------------------
            # Provider management & health events
            # -----------------------------------------------------------
            elif msg.type == EventType.PROVIDER_LIST_REQUEST:
                providers = await _build_provider_list_async(secret_store)
                cfg = engine_state.user_config
                await ws.send_text(Event(
                    type=EventType.PROVIDER_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"providers": providers, "default_provider": cfg.default_provider, "default_model": cfg.default_model},
                ).to_json())

            elif msg.type in (EventType.PROVIDER_VALIDATE_REQUEST, EventType.SETTINGS_PROVIDER_VALIDATE_REQUEST):
                pname = msg.payload.get("provider", "")
                key = msg.payload.get("api_key", "")
                base_url = msg.payload.get("base_url", "")
                resp_type = (
                    EventType.SETTINGS_PROVIDER_VALIDATE_RESULT
                    if msg.type == EventType.SETTINGS_PROVIDER_VALIDATE_REQUEST
                    else EventType.PROVIDER_VALIDATE_RESPONSE
                )
                is_custom = pname == "custom_openai" or engine_state.user_config.custom_provider_types.get(pname) == "openai_compatible"
                
                # If key is empty (e.g. masked in UI), attempt to load the saved key
                if not key:
                    saved_key = secret_store.load_provider(pname)
                    if saved_key:
                        key = saved_key
                
                # If base_url is empty, attempt to load the saved base_url
                if not base_url:
                    saved_url = engine_state.user_config.custom_base_urls.get(pname)
                    if saved_url:
                        base_url = saved_url
                
                # Only persist discovered models for providers that are already
                # saved; testing a throwaway/template provider must not leak
                # discovery data into the persisted config.
                is_registered = (
                    pname in set(secret_store.list_providers())
                    or pname in engine_state.user_config.custom_base_urls
                    or pname in engine_state.user_config.custom_provider_types
                )

                if not key and not is_custom:
                    await ws.send_text(Event(
                        type=resp_type,
                        request_id=req_id,
                        payload={"provider": pname, "status": "invalid_key", "message": "API key cannot be empty."},
                    ).to_json())
                else:
                    result = await _validate_provider_key(
                        pname, key, base_url=base_url, persist_models=False
                    )
                    result["provider"] = pname
                    await ws.send_text(Event(
                        type=resp_type,
                        request_id=req_id,
                        payload=result,
                    ).to_json())

            elif msg.type == EventType.PROVIDER_SAVE_REQUEST:
                pname = msg.payload.get("provider", "")
                key = msg.payload.get("api_key", "")
                base_url = msg.payload.get("base_url", "")
                is_default = msg.payload.get("is_default", False)
                default_model = msg.payload.get("default_model", "")
                models = msg.payload.get("models", [])
                display_name = msg.payload.get("display_name", "")
                provider_type = msg.payload.get("provider_type", "")
                try:
                    if not isinstance(pname, str) or not pname.strip():
                        raise ValueError("Provider name is required.")
                    is_custom_provider = (
                        pname == "custom_openai"
                        or pname.startswith("custom_")
                        or provider_type == "openai_compatible"
                    )
                    if is_custom_provider:
                        if display_name:
                            engine_state.user_config.custom_provider_names[pname] = display_name
                        if provider_type:
                            engine_state.user_config.custom_provider_types[pname] = provider_type
                        engine_state.user_config.custom_base_urls[pname] = base_url
                    elif base_url:
                        engine_state.user_config.custom_base_urls[pname] = base_url

                    # Save config before fetching models so custom names/URLs are persisted
                    # even if the model fetch raises an exception.
                    save_config(engine_state.user_config)

                    # A new provider derived from the "custom_openai" template leaves
                    # throwaway discovery data on that template (written during Test /
                    # Refresh). Clear it unless the template is itself a real provider.
                    if is_custom_provider and pname != "custom_openai":
                        template_in_use = (
                            "custom_openai" in set(secret_store.list_providers())
                            or "custom_openai" in engine_state.user_config.custom_base_urls
                            or "custom_openai" in engine_state.user_config.custom_provider_types
                        )
                        if not template_in_use:
                            engine_state.user_config.provider_models.pop("custom_openai", None)
                            engine_state.model_registry.remove_provider_models("custom_openai")
                            engine_state.health_manager.remove_provider("custom_openai")

                    if key:
                        secret_store.save_provider(pname, key)
                        engine_state.health_manager.reset_provider(pname)
                        if not models:
                            models = await fetch_available_models(pname, key, base_url=base_url)
                        if models:
                            engine_state.user_config.provider_models[pname] = models
                            engine_state.model_registry.update_provider_models(pname, models)
                    elif models:
                        engine_state.user_config.provider_models[pname] = models
                        engine_state.model_registry.update_provider_models(pname, models)

                    if default_model:
                        if is_default or engine_state.user_config.default_provider == pname:
                            engine_state.user_config.default_provider = pname
                            engine_state.user_config.default_model = default_model
                            engine_state.active_provider = pname
                            engine_state.active_model = default_model
                    elif is_default and pname:
                        engine_state.user_config.default_provider = pname
                        cur_models = engine_state.user_config.provider_models.get(pname, [])
                        if cur_models:
                            engine_state.user_config.default_model = cur_models[0]
                            engine_state.active_model = cur_models[0]
                        engine_state.active_provider = pname

                    save_config(engine_state.user_config)
                    engine_state.audit_logger.log_event("PROVIDER_SAVED", {"provider": pname})
                    await ws.send_text(Event(
                        type=EventType.PROVIDER_SAVE_RESPONSE,
                        request_id=req_id,
                        payload={
                            "provider": pname,
                            "success": True,
                            "models": engine_state.user_config.provider_models.get(pname, []),
                        },
                    ).to_json())
                except Exception as e:
                    await ws.send_text(Event(
                        type=EventType.PROVIDER_SAVE_RESPONSE,
                        request_id=req_id,
                        payload={
                            "provider": pname,
                            "success": False,
                            "error": f"{type(e).__name__}: {e}",
                        },
                    ).to_json())

            elif msg.type == EventType.PROVIDER_REMOVE_REQUEST:
                pname = msg.payload.get("provider", "")
                deleted = secret_store.delete_provider(pname)
                engine_state.user_config.provider_models.pop(pname, None)
                engine_state.user_config.custom_base_urls.pop(pname, None)
                engine_state.user_config.custom_provider_names.pop(pname, None)
                engine_state.user_config.custom_provider_types.pop(pname, None)
                engine_state.model_registry.remove_provider_models(pname)
                engine_state.health_manager.remove_provider(pname)
                if pname in engine_state.configured_providers:
                    engine_state.configured_providers.remove(pname)
                if engine_state.user_config.default_provider == pname:
                    remaining = [p for p in secret_store.list_providers() if p != pname]
                    new_default = remaining[0] if remaining else "google_gemini"
                    engine_state.user_config.default_provider = new_default
                    new_models = engine_state.user_config.provider_models.get(new_default, [])
                    engine_state.user_config.default_model = new_models[0] if new_models else ""
                    engine_state.active_provider = new_default
                    engine_state.active_model = engine_state.user_config.default_model
                save_config(engine_state.user_config)
                engine_state.audit_logger.log_event("PROVIDER_REMOVED", {"provider": pname})
                await ws.send_text(Event(
                    type=EventType.PROVIDER_REMOVE_RESPONSE,
                    request_id=req_id,
                    payload={"provider": pname, "deleted": deleted, "success": True},
                ).to_json())

            elif msg.type == EventType.PROVIDER_REVEAL_KEY_REQUEST:
                pname = msg.payload.get("provider", "")
                try:
                    decrypted_key = secret_store.load_provider(pname)
                    await ws.send_text(Event(
                        type=EventType.PROVIDER_REVEAL_KEY_RESPONSE,
                        request_id=req_id,
                        payload={"provider": pname, "api_key": decrypted_key, "success": True},
                    ).to_json())
                except Exception as e:
                    await ws.send_text(Event(
                        type=EventType.PROVIDER_REVEAL_KEY_RESPONSE,
                        request_id=req_id,
                        payload={"provider": pname, "success": False, "error": str(e)},
                    ).to_json())

            elif msg.type == EventType.MODEL_SET_DEFAULT:
                pname = msg.payload.get("provider", engine_state.user_config.default_provider)
                model = msg.payload.get("model", "")
                if model:
                    engine_state.user_config.default_provider = pname
                    engine_state.user_config.default_model = model
                    engine_state.active_provider = pname
                    engine_state.active_model = model
                    save_config(engine_state.user_config)
                await ws.send_text(Event(
                    type=EventType.MODEL_DEFAULT_CHANGED,
                    request_id=req_id,
                    payload={"provider": pname, "model": engine_state.user_config.default_model},
                ).to_json())

            # -----------------------------------------------------------
            # Session Events
            # -----------------------------------------------------------
            elif msg.type == EventType.SESSION_LIST_REQUEST:
                sessions = session_store.list_sessions()
                await ws.send_text(Event(
                    type=EventType.SESSION_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"sessions": sessions},
                ).to_json())

            elif msg.type == EventType.SESSION_GET_REQUEST:
                sess_id = msg.payload.get("session_id")
                session_data = session_store.get_session(sess_id) if sess_id else None
                await ws.send_text(Event(
                    type=EventType.SESSION_GET_RESPONSE,
                    request_id=req_id,
                    payload={"session": session_data},
                ).to_json())

            elif msg.type == EventType.SESSION_CREATE_REQUEST:
                title = msg.payload.get("title", "New Chat")
                sess_id = session_store.create_session(title)
                await ws.send_text(Event(
                    type=EventType.SESSION_CREATE_RESPONSE,
                    request_id=req_id,
                    payload={"session_id": sess_id},
                ).to_json())

            elif msg.type == EventType.SESSION_DELETE_REQUEST:
                sess_id = msg.payload.get("session_id")
                deleted = session_store.delete_session(sess_id) if sess_id else False
                await ws.send_text(Event(
                    type=EventType.SESSION_DELETE_RESPONSE,
                    request_id=req_id,
                    payload={"session_id": sess_id, "deleted": deleted},
                ).to_json())

            # -----------------------------------------------------------
            # Local Models (Ollama) & Task Download Events
            # -----------------------------------------------------------
            elif msg.type == EventType.LOCAL_MODEL_LIST_REQUEST:
                models = await engine_state.ollama_manager.list_models()
                await ws.send_text(Event(
                    type=EventType.LOCAL_MODEL_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"models": models},
                ).to_json())

            elif msg.type == EventType.LOCAL_MODEL_DOWNLOAD_START:
                model_name = msg.payload.get("model", "") or msg.payload.get("repo_id", "")
                dest_dir = msg.payload.get("destination_dir")
                if task_runner and not task_runner.done():
                    task_runner.cancel()

                async def execute_local_download():
                    async for dl_ev in engine_state.ollama_manager.download_model_task(
                        model_name=model_name,
                        destination_dir=dest_dir,
                        request_id=req_id,
                    ):
                        await ws.send_text(dl_ev.to_json())

                dl_task = asyncio.create_task(execute_local_download())
                active_downloads[model_name] = dl_task
                task_runner = dl_task

            elif msg.type == EventType.LOCAL_MODEL_DOWNLOAD_CANCEL:
                cancel_model = msg.payload.get("model", "")
                dl = active_downloads.pop(cancel_model, None)
                if dl and not dl.done():
                    dl.cancel()
                    logger.info(f"Download cancelled for model: {cancel_model}")

            elif msg.type == EventType.LOCAL_MODEL_DELETE_REQUEST:
                model_name = msg.payload.get("model", "")
                deleted = await engine_state.ollama_manager.delete_model(model_name)
                await ws.send_text(Event(
                    type=EventType.LOCAL_MODEL_DELETE_RESPONSE,
                    request_id=req_id,
                    payload={"model": model_name, "deleted": deleted},
                ).to_json())

            # -----------------------------------------------------------
            # Capabilities Check Scheduler Events
            # -----------------------------------------------------------
            elif msg.type == EventType.CAPABILITY_CHECK_SET_SCHEDULE:
                interval = msg.payload.get("schedule", "weekly")
                method = msg.payload.get("method", "Both")
                engine_state.scheduler_manager.set_schedule(interval, method)
                status_sum = engine_state.scheduler_manager.get_status_summary()
                await ws.send_text(Event(
                    type=EventType.CAPABILITY_CHECK_HISTORY_RESPONSE,
                    request_id=req_id,
                    payload={
                        "summary": status_sum,
                        "history": engine_state.scheduler_manager.get_history(),
                    },
                ).to_json())

            elif msg.type == EventType.CAPABILITY_CHECK_RUN_NOW:
                method = msg.payload.get("method")
                run_res = await engine_state.scheduler_manager.run_now(method=method)
                status_sum = engine_state.scheduler_manager.get_status_summary()
                await ws.send_text(Event(
                    type=EventType.CAPABILITY_CHECK_HISTORY_RESPONSE,
                    request_id=req_id,
                    payload={
                        "latest_run": run_res,
                        "summary": status_sum,
                        "history": engine_state.scheduler_manager.get_history(),
                    },
                ).to_json())

            elif msg.type == EventType.CAPABILITY_CHECK_GET_HISTORY:
                status_sum = engine_state.scheduler_manager.get_status_summary()
                history = engine_state.scheduler_manager.get_history()
                await ws.send_text(Event(
                    type=EventType.CAPABILITY_CHECK_HISTORY_RESPONSE,
                    request_id=req_id,
                    payload={"summary": status_sum, "history": history},
                ).to_json())

            elif msg.type == EventType.REFRESH_MODELS_REQUEST:
                target_provider = msg.payload.get("provider")
                stored = set(secret_store.list_providers())
                cfg = engine_state.user_config
                
                providers_to_refresh = set(stored)
                for p in cfg.custom_provider_types:
                    if cfg.custom_provider_types[p] == "openai_compatible":
                        providers_to_refresh.add(p)
                providers_to_refresh.add("custom_openai")
                
                if target_provider:
                    providers_to_refresh = [target_provider]
                else:
                    providers_to_refresh = list(providers_to_refresh)

                refreshed_models: Dict[str, List[str]] = {}

                for pname in providers_to_refresh:
                    try:
                        payload_key = msg.payload.get("api_key")
                        payload_url = msg.payload.get("base_url")
                        
                        if payload_key is not None:
                            key = payload_key
                        else:
                            key = secret_store.load_provider(pname) if pname in stored else ""
                            
                        if payload_url is not None:
                            base_url = payload_url
                        else:
                            base_url = cfg.custom_base_urls.get(pname)

                        is_custom = pname == "custom_openai" or cfg.custom_provider_types.get(pname) == "openai_compatible"
                        if key or is_custom:
                            disc_models = await fetch_available_models(
                                provider_name=pname,
                                api_key=key,
                                base_url=base_url,
                                filter_reachability=True,
                                force_reachability=True,
                            )
                            # Do not persist discovery data for unsaved/template
                            # providers; models still flow back to the UI response.
                            if disc_models:
                                refreshed_models[pname] = disc_models
                            is_registered = (
                                pname in stored
                                or pname in cfg.custom_base_urls
                                or pname in cfg.custom_provider_types
                            )
                            # Only persist if it's a background auto-refresh (no target_provider).
                            # If target_provider is set, it's a manual UI click and should not save.
                            if disc_models and is_registered and not target_provider:
                                cfg.provider_models[pname] = disc_models
                                engine_state.model_registry.update_provider_models(pname, disc_models)
                                save_config(cfg)
                    except Exception as ref_err:
                        logger.error(f"Error during refresh models for {pname}: {ref_err}")

                updated_providers = await _build_provider_list_async(secret_store)
                # Surface freshly discovered models for the target provider even if
                # it is an unsaved template (whose data we intentionally don't persist).
                for prov in updated_providers:
                    if prov.get("name") in refreshed_models:
                        prov["models"] = refreshed_models[prov["name"]]
                await ws.send_text(Event(
                    type=EventType.REFRESH_MODELS_RESPONSE,
                    request_id=req_id,
                    payload={
                        "providers": updated_providers,
                        "default_provider": cfg.default_provider,
                        "default_model": cfg.default_model,
                    },
                ).to_json())

            # -----------------------------------------------------------
            # Tools, MCP, Plugins, Memory Events
            # -----------------------------------------------------------
            elif msg.type == EventType.TOOL_LIST_REQUEST:
                all_tools = create_file_tools() + create_web_tools()
                defs = []
                for t in all_tools:
                    policy = engine_state.user_config.tool_policies.get(t.name, "Require Approval")
                    d = t.to_definition()
                    d["policy"] = policy
                    defs.append(d)
                await ws.send_text(Event(
                    type=EventType.TOOL_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"tools": defs},
                ).to_json())

            elif msg.type == EventType.TOOL_POLICY_SET_REQUEST:
                tool_name = msg.payload.get("tool_name")
                policy = msg.payload.get("policy")
                if tool_name and policy:
                    engine_state.user_config.tool_policies[tool_name] = policy
                    save_config(engine_state.user_config)
                await ws.send_text(Event(
                    type=EventType.TOOL_POLICY_SET_RESPONSE,
                    request_id=req_id,
                    payload={"success": True, "tool_name": tool_name, "policy": policy},
                ).to_json())

            elif msg.type == EventType.MCP_SERVER_LIST_REQUEST:
                from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
                servers = GLOBAL_MCP_REGISTRY.list_servers()
                await ws.send_text(Event(
                    type=EventType.MCP_SERVER_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"servers": servers},
                ).to_json())

            elif msg.type == EventType.MCP_SERVER_ADD_REQUEST:
                from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
                name = msg.payload.get("name")
                config = msg.payload.get("config", {})
                if name and config:
                    GLOBAL_MCP_REGISTRY.add_server(name, config)
                await ws.send_text(Event(
                    type=EventType.MCP_SERVER_ADD_RESPONSE,
                    request_id=req_id,
                    payload={"success": True, "name": name, "config": config},
                ).to_json())

            elif msg.type == EventType.MCP_SERVER_REMOVE_REQUEST:
                from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
                name = msg.payload.get("name")
                if name:
                    GLOBAL_MCP_REGISTRY.remove_server(name)
                await ws.send_text(Event(
                    type=EventType.MCP_SERVER_REMOVE_RESPONSE,
                    request_id=req_id,
                    payload={"success": True, "name": name},
                ).to_json())

            elif msg.type == EventType.PLUGIN_LIST_REQUEST:
                plugins_dict = engine_state.plugin_installer.get_all()
                plugins_list = list(plugins_dict.values()) if plugins_dict else []
                await ws.send_text(Event(
                    type=EventType.PLUGIN_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"plugins": plugins_list},
                ).to_json())

            elif msg.type == EventType.PLUGIN_INSTALL_REQUEST:
                source = msg.payload.get("source")
                try:
                    data = engine_state.plugin_installer.prepare_install(source)
                    pending_plugin_installs[req_id] = data
                    await ws.send_text(Event(
                        type=EventType.PLUGIN_APPROVAL_REQUEST,
                        request_id=req_id,
                        payload={
                            "plugin_name": data["manifest"].get("name", "Unknown"),
                            "plugin_details": data,
                        },
                    ).to_json())
                except Exception as e:
                    await ws.send_text(Event(
                        type=EventType.PLUGIN_INSTALL_RESPONSE,
                        request_id=req_id,
                        payload={"success": False, "error": str(e)},
                    ).to_json())

            elif msg.type == EventType.PLUGIN_APPROVAL_GRANTED:
                data = pending_plugin_installs.pop(req_id, None)
                if data:
                    try:
                        engine_state.plugin_installer.install(data)
                        engine_state.audit_logger.log_event("PLUGIN_INSTALLED", {"manifest": data["manifest"]})
                        await ws.send_text(Event(
                            type=EventType.PLUGIN_INSTALL_RESPONSE,
                            request_id=req_id,
                            payload={"success": True},
                        ).to_json())
                    except Exception as e:
                        await ws.send_text(Event(
                            type=EventType.PLUGIN_INSTALL_RESPONSE,
                            request_id=req_id,
                            payload={"success": False, "error": str(e)},
                        ).to_json())

            elif msg.type == EventType.PLUGIN_APPROVAL_REJECTED:
                pending_plugin_installs.pop(req_id, None)
                await ws.send_text(Event(
                    type=EventType.PLUGIN_INSTALL_RESPONSE,
                    request_id=req_id,
                    payload={"success": False, "error": "Install rejected by user."},
                ).to_json())

            elif msg.type == EventType.PLUGIN_UNINSTALL_REQUEST:
                name = msg.payload.get("name")
                try:
                    engine_state.plugin_installer.uninstall(name)
                    await ws.send_text(Event(
                        type=EventType.PLUGIN_UNINSTALL_RESPONSE,
                        request_id=req_id,
                        payload={"success": True, "name": name},
                    ).to_json())
                except Exception as e:
                    await ws.send_text(Event(
                        type=EventType.PLUGIN_UNINSTALL_RESPONSE,
                        request_id=req_id,
                        payload={"success": False, "error": str(e)},
                    ).to_json())
                    
            elif msg.type == EventType.PLUGIN_TOGGLE_REQUEST:
                name = msg.payload.get("name")
                enabled = msg.payload.get("enabled", True)
                try:
                    engine_state.plugin_installer.toggle(name, enabled)
                    await ws.send_text(Event(
                        type=EventType.PLUGIN_TOGGLE_RESPONSE,
                        request_id=req_id,
                        payload={"success": True, "name": name, "enabled": enabled},
                    ).to_json())
                except Exception as e:
                    await ws.send_text(Event(
                        type=EventType.PLUGIN_TOGGLE_RESPONSE,
                        request_id=req_id,
                        payload={"success": False, "error": str(e)},
                    ).to_json())

            elif msg.type == EventType.MEMORY_EPISODES_REQUEST:
                from aether_engine.memory.episodic import get_all_episodes
                episodes = get_all_episodes(limit=msg.payload.get("limit", 50))
                await ws.send_text(Event(
                    type=EventType.MEMORY_EPISODES_RESPONSE,
                    request_id=req_id,
                    payload={"episodes": episodes},
                ).to_json())

            elif msg.type == EventType.MEMORY_EPISODE_DELETE_REQUEST:
                from aether_engine.memory.episodic import delete_episode, get_all_episodes
                task_id = msg.payload.get("task_id")
                if task_id:
                    delete_episode(task_id)
                episodes = get_all_episodes(limit=msg.payload.get("limit", 50))
                await ws.send_text(Event(
                    type=EventType.MEMORY_EPISODE_DELETE_RESPONSE,
                    request_id=req_id,
                    payload={"success": True, "task_id": task_id, "episodes": episodes},
                ).to_json())

            elif msg.type == EventType.MEMORY_EPISODES_CLEAR_REQUEST:
                from aether_engine.memory.episodic import clear_all_episodes
                clear_all_episodes()
                await ws.send_text(Event(
                    type=EventType.MEMORY_EPISODES_CLEAR_RESPONSE,
                    request_id=req_id,
                    payload={"success": True, "episodes": []},
                ).to_json())
            
            elif msg.type == EventType.MEMORY_GRAPH_REQUEST:
                from aether_engine.memory.knowledge_graph import get_all_facts
                facts = get_all_facts(limit=msg.payload.get("limit", 100))
                await ws.send_text(Event(
                    type=EventType.MEMORY_GRAPH_RESPONSE,
                    request_id=req_id,
                    payload={"facts": facts},
                ).to_json())

            elif msg.type == EventType.MEMORY_FACT_DELETE_REQUEST:
                from aether_engine.memory.knowledge_graph import delete_fact, get_all_facts
                fact_id = msg.payload.get("fact_id")
                if fact_id:
                    delete_fact(fact_id)
                facts = get_all_facts(limit=msg.payload.get("limit", 100))
                await ws.send_text(Event(
                    type=EventType.MEMORY_FACT_DELETE_RESPONSE,
                    request_id=req_id,
                    payload={"success": True, "fact_id": fact_id, "facts": facts},
                ).to_json())

            elif msg.type == EventType.MEMORY_ENTITY_DELETE_REQUEST:
                from aether_engine.memory.knowledge_graph import delete_entity, get_all_facts
                entity_name = msg.payload.get("entity_name")
                if entity_name:
                    delete_entity(entity_name)
                facts = get_all_facts(limit=msg.payload.get("limit", 100))
                await ws.send_text(Event(
                    type=EventType.MEMORY_ENTITY_DELETE_RESPONSE,
                    request_id=req_id,
                    payload={"success": True, "entity_name": entity_name, "facts": facts},
                ).to_json())

            elif msg.type == EventType.MEMORY_GRAPH_CLEAR_REQUEST:
                from aether_engine.memory.knowledge_graph import clear_all_facts
                clear_all_facts()
                await ws.send_text(Event(
                    type=EventType.MEMORY_GRAPH_CLEAR_RESPONSE,
                    request_id=req_id,
                    payload={"success": True, "facts": []},
                ).to_json())

            elif msg.type == EventType.WORKSPACE_LIST_REQUEST:
                roots = engine_state.user_config.workspace_roots or []
                workspaces = []
                for r in roots:
                    p = Path(r)
                    workspaces.append({
                        "path": r,
                        "name": p.name,
                        "is_indexed": False,  # No real indexing backend yet
                    })
                await ws.send_text(Event(
                    type=EventType.WORKSPACE_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"workspaces": workspaces},
                ).to_json())

            elif msg.type == EventType.WORKSPACE_ADD_REQUEST:
                ws_path = msg.payload.get("path", "").strip()
                if ws_path and ws_path not in engine_state.user_config.workspace_roots:
                    engine_state.user_config.workspace_roots.append(ws_path)
                    save_config(engine_state.user_config)
                    p = Path(ws_path)
                    await ws.send_text(Event(
                        type=EventType.WORKSPACE_ADD_RESPONSE,
                        request_id=req_id,
                        payload={"success": True, "workspace": {"path": ws_path, "name": p.name, "is_indexed": False}},
                    ).to_json())
                else:
                    await ws.send_text(Event(
                        type=EventType.WORKSPACE_ADD_RESPONSE,
                        request_id=req_id,
                        payload={"success": False, "error": "Path empty or already exists"},
                    ).to_json())

            elif msg.type == EventType.WORKSPACE_REMOVE_REQUEST:
                ws_path = msg.payload.get("path", "").strip()
                if ws_path in engine_state.user_config.workspace_roots:
                    engine_state.user_config.workspace_roots.remove(ws_path)
                    save_config(engine_state.user_config)
                    await ws.send_text(Event(
                        type=EventType.WORKSPACE_REMOVE_RESPONSE,
                        request_id=req_id,
                        payload={"success": True, "path": ws_path},
                    ).to_json())
                else:
                    await ws.send_text(Event(
                        type=EventType.WORKSPACE_REMOVE_RESPONSE,
                        request_id=req_id,
                        payload={"success": False, "error": "Path not found"},
                    ).to_json())

    except WebSocketDisconnect:
        logger.info(f"Client disconnected from {ip}:{port}")
        if task_runner and not task_runner.done():
            task_runner.cancel()
        engine_state.audit_logger.log_event("CLIENT_DISCONNECTED", {"client_ip": ip})
    except Exception as e:
        logger.error(f"WebSocket session error: {e}")
        if task_runner and not task_runner.done():
            task_runner.cancel()


# --- PLUGIN ENDPOINTS ---

class InstallRequest(BaseModel):
    source: str

class ConfirmRequest(BaseModel):
    prepare_data: Dict[str, Any]

class ToggleRequest(BaseModel):
    enabled: bool

@app.get("/plugins")
def list_plugins():
    return engine_state.plugin_installer.get_all()

@app.post("/plugins/install")
def prepare_plugin(req: InstallRequest):
    try:
        data = engine_state.plugin_installer.prepare_install(req.source)
        return data
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/plugins/confirm")
def confirm_plugin(req: ConfirmRequest):
    try:
        engine_state.plugin_installer.install(req.prepare_data)
        engine_state.audit_logger.log_event("PLUGIN_INSTALLED", {"manifest": req.prepare_data["manifest"]})
        return {"status": "success"}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/plugins/{name}")
def delete_plugin(name: str):
    engine_state.plugin_installer.uninstall(name)
    return {"status": "success"}

@app.patch("/plugins/{name}/toggle")
def toggle_plugin(name: str, req: ToggleRequest):
    try:
        engine_state.plugin_installer.toggle(name, req.enabled)
        return {"status": "success"}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=str(e))

# --- AGENT NODES ENDPOINTS ---
disabled_nodes = set()

@app.get("/agents/nodes")
def list_agent_nodes():
    # Return fake list for UI
    from aether_engine.routing.capability_router import CapabilityRouter
    
    # Just hardcoded ones based on capability router
    nodes = [
        {"name": "research", "model": engine_state.user_config.default_model, "enabled": "research" not in disabled_nodes},
        {"name": "coding", "model": engine_state.user_config.default_model, "enabled": "coding" not in disabled_nodes},
        {"name": "general", "model": engine_state.user_config.default_model, "enabled": "general" not in disabled_nodes},
    ]
    return nodes

@app.patch("/agents/nodes/{name}/toggle")
def toggle_agent_node(name: str, req: ToggleRequest):
    if req.enabled:
        disabled_nodes.discard(name)
    else:
        disabled_nodes.add(name)
    return {"status": "success"}
