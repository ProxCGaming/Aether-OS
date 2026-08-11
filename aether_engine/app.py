"""FastAPI Engine application with authenticated loopback WebSocket, provider health, intelligent routing, safe fallbacks, local model task downloads, and capability check scheduling."""
import asyncio
import json
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
import time
from typing import Any, Dict, List, Optional
from pathlib import Path

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
from aether_engine.orchestration.simple import run_task
from aether_engine.providers.discovery import fetch_available_models
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
from aether_engine.tools.registry import ToolRegistry, create_current_time_tool, create_file_tools
from aether_engine.routing.task_class_router import infer_task_class
from aether_engine.workers.job_object import WorkerJobObject

logger = logging.getLogger("aether_engine")


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
        self.active_provider: str = "google_gemini"
        self.active_model: str = "gemini-2.5-flash"
        self.configured_providers: List[str] = []

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
    for pname in stored:
        try:
            key = secret_store.load_provider(pname)
            if key:
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
    return result


async def _validate_provider_key(provider_name: str, api_key: str, base_url: Optional[str] = None) -> dict:
    """Validate a provider key and discover its available models in real-time."""
    if not base_url:
        base_url = engine_state.user_config.custom_base_urls.get(provider_name)
    discovered_models = await fetch_available_models(provider_name, api_key, base_url=base_url)

    # Pick the first discovered model (or default) for the test ping
    test_model = discovered_models[0] if discovered_models else ""
    result = await validate_api_key(provider_name, api_key, model=test_model, base_url=base_url)

    # Attach discovered models to the result dictionary
    result["models"] = discovered_models

    if result["status"] == "connected":
        latency = result.get("latency_ms", 100.0)
        engine_state.health_manager.record_success(provider_name, latency)
        if discovered_models:
            engine_state.user_config.provider_models[provider_name] = discovered_models
            engine_state.model_registry.update_provider_models(provider_name, discovered_models)
            save_config(engine_state.user_config)
    elif result["status"] == "invalid_key":
        engine_state.health_manager.record_failure(provider_name, result["message"], is_retriable=False)
    else:
        engine_state.health_manager.record_failure(provider_name, result["message"], is_retriable=True)
    return result


def _create_provider_instance(provider_name: str, model: str, secret_store: SecretStore):
    """Factory for LLM provider instances — all providers route through LiteLLM."""
    api_key = secret_store.load_provider(provider_name)
    base_url = engine_state.user_config.custom_base_urls.get(provider_name)
    return LiteLLMProvider(api_key=api_key, model=model, provider_name=provider_name, base_url=base_url)


async def _execute_tool_in_worker(tool_name: str, args: Dict[str, Any], task_class: str) -> Any:
    """Run an approved risky tool in a dedicated worker subprocess and return its result."""
    payload = json.dumps({"tool_name": tool_name, "args": args})
    
    limits = engine_state.user_config.task_classes.get(task_class, engine_state.user_config.task_classes.get("standard", {"time_cap_seconds": 1800, "memory_cap_mb": 4096}))
    time_cap = limits["time_cap_seconds"]
    mem_cap = limits["memory_cap_mb"]
    
    job_obj = None
    try:
        job_obj = WorkerJobObject(time_cap, mem_cap)
    except Exception as e:
        logger.warning(f"Failed to create Job Object: {e}")

    start_time = time.time()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "aether_worker",
        "--payload",
        payload,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    
    if job_obj and proc.pid:
        try:
            job_obj.assign_process(proc.pid)
        except Exception:
            pass

    try:
        stdout, stderr = await proc.communicate()
    except Exception as e:
        logger.error(f"Worker process error: {e}")
        engine_state.audit_logger.log_event("WORKER_EXECUTION_TERMINATED", {
            "tool": tool_name,
            "task_class": task_class,
            "reason": str(e),
            "wall_clock_seconds": round(time.time() - start_time, 2)
        })
        raise RuntimeError(f"Worker terminated: {e}")

    wall_clock = time.time() - start_time
    if proc.returncode != 0:
        err_msg = stderr.decode(errors="ignore") or stdout.decode(errors="ignore") or f"worker failed (code {proc.returncode})"
        engine_state.audit_logger.log_event("WORKER_EXECUTION_TERMINATED", {
            "tool": tool_name,
            "task_class": task_class,
            "exit_code": proc.returncode,
            "reason": "Limit breach or internal error",
            "wall_clock_seconds": round(wall_clock, 2)
        })
        raise RuntimeError(err_msg)

    engine_state.audit_logger.log_event("WORKER_EXECUTION_COMPLETED", {
        "tool": tool_name,
        "task_class": task_class,
        "wall_clock_seconds": round(wall_clock, 2)
    })

    text = stdout.decode(errors="ignore").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    return parsed


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

    task_runner: Optional[asyncio.Task] = None
    secret_store = SecretStore()
    approval_queues: Dict[str, asyncio.Queue] = {}

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
                for tool in create_file_tools():
                    tools.register(tool)

                workspace_roots = [str(Path.home() / "AetherWorkspace")]

                async def approval_handler(tool_name: str, args: Dict[str, Any], request_id: Optional[str] = None) -> Any:
                    approval_key = request_id or req_id or f"approval-{tool_name}"
                    queue: asyncio.Queue = asyncio.Queue()
                    
                    task_class = infer_task_class(tool_name, args)
                    
                    approval_queues[approval_key] = queue
                    await ws.send_text(Event(
                        type=EventType.TOOL_APPROVAL_REQUEST,
                        request_id=request_id,
                        payload={
                            "tool_name": tool_name,
                            "args": args,
                            "task_id": req_id,
                            "approval_key": approval_key,
                            "task_class": task_class,
                            "workspace_roots": workspace_roots,
                        },
                    ).to_json())
                    try:
                        decision = await asyncio.wait_for(queue.get(), timeout=30.0)
                    except asyncio.TimeoutError:
                        return False
                    finally:
                        approval_queues.pop(approval_key, None)
                    if not decision:
                        return False
                    
                    final_task_class = decision if isinstance(decision, str) else task_class
                    return await _execute_tool_in_worker(tool_name, args, final_task_class)

                async def execute_task_with_fallback(primary_provider, p_name: str, p_model: str):
                    task_generator = run_task(
                        prompt=prompt,
                        provider=primary_provider,
                        tools=tools,
                        request_id=req_id,
                        approval_handler=approval_handler,
                        workspace_roots=workspace_roots,
                    )

                    first_event = True
                    primary_failed = False
                    failure_event = None

                    async for ev in task_generator:
                        if ev.type == EventType.TASK_CREATED:
                            ev.payload["routing"] = {
                                "provider": decision.provider,
                                "model": decision.model,
                                "reason": decision.reason,
                                "capabilities": decision.capabilities,
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

                        if ev.type == EventType.TASK_FAILED and first_event and ev.payload.get("retriable"):
                            primary_failed = True
                            failure_event = ev
                            break
                        first_event = False

                        if ev.type == EventType.TASK_COMPLETED:
                            latency = ev.payload.get("latency_ms", 100.0)
                            engine_state.health_manager.record_success(p_name, latency)

                        await ws.send_text(ev.to_json())

                    if primary_failed and failure_event:
                        err_msg = failure_event.payload.get("error", "Unknown error")
                        engine_state.health_manager.record_failure(p_name, err_msg, is_retriable=True)

                        fallbacks = get_fallback_candidates(
                            failed_provider=p_name,
                            configured_providers=engine_state.configured_providers,
                            health_manager=engine_state.health_manager,
                            registry=engine_state.model_registry,
                        )

                        if not fallbacks:
                            await ws.send_text(failure_event.to_json())
                            return

                        fallback_candidate = fallbacks[0]
                        fb_provider = fallback_candidate.provider
                        fb_model = fallback_candidate.id

                        engine_state.audit_logger.log_event("FALLBACK_TRIGGERED", {
                            "from_provider": p_name,
                            "to_provider": fb_provider,
                            "to_model": fb_model,
                            "reason": err_msg,
                        })

                        await ws.send_text(Event(
                            type=EventType.FALLBACK_STARTED,
                            request_id=req_id,
                            payload={
                                "from_provider": p_name,
                                "to_provider": fb_provider,
                                "to_model": fb_model,
                                "reason": f"{p_name} failed ({err_msg}) — retrying with {fb_provider} ({fb_model})",
                            },
                        ).to_json())

                        try:
                            alt_provider_inst = _create_provider_instance(fb_provider, fb_model, secret_store)
                            async for alt_ev in run_task(prompt=prompt, provider=alt_provider_inst, tools=tools, request_id=req_id, approval_handler=approval_handler, workspace_roots=workspace_roots):
                                if alt_ev.type == EventType.TASK_COMPLETED:
                                    latency = alt_ev.payload.get("latency_ms", 100.0)
                                    engine_state.health_manager.record_success(fb_provider, latency)
                                    engine_state.audit_logger.log_event("FALLBACK_COMPLETED", {
                                        "provider": fb_provider,
                                        "model": fb_model,
                                        "latency_ms": latency,
                                    })
                                    await ws.send_text(Event(
                                        type=EventType.FALLBACK_COMPLETED,
                                        request_id=req_id,
                                        payload={"provider": fb_provider, "model": fb_model},
                                    ).to_json())
                                await ws.send_text(alt_ev.to_json())
                        except Exception as fb_err:
                            engine_state.audit_logger.log_event("FALLBACK_FAILED", {
                                "from_provider": p_name,
                                "to_provider": fb_provider,
                                "error": str(fb_err),
                            })
                            await ws.send_text(Event(
                                type=EventType.FALLBACK_FAILED,
                                request_id=req_id,
                                payload={"error": str(fb_err)},
                            ).to_json())
                            await ws.send_text(failure_event.to_json())

                task_runner = asyncio.create_task(
                    execute_task_with_fallback(provider_instance, decision.provider, decision.model)
                )

            elif msg.type == EventType.CANCEL_TASK:
                if task_runner and not task_runner.done():
                    task_runner.cancel()
                    logger.info("Cancellation requested for active task runner.")
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
                # Now handled by LangGraph's native resume
                # We resume the graph with the user's decision
                approval_key = msg.payload.get("approval_key")
                decision = (msg.type == EventType.TOOL_APPROVAL_GRANTED)
                
                # In a real LangGraph setup:
                # engine_state.graph.ainvoke(Command(resume={"approved": decision}), config={"configurable": {"thread_id": approval_key}})
                
                # Fallback scaffold for now:
                queue = approval_queues.get(approval_key)
                if queue is not None:
                    if msg.type == EventType.TOOL_APPROVAL_GRANTED:
                        override = msg.payload.get("override_class")
                        await queue.put(override if override else True)
                    else:
                        await queue.put(False)
                    approval_queues.pop(approval_key, None)

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
                if not key:
                    await ws.send_text(Event(
                        type=resp_type,
                        request_id=req_id,
                        payload={"provider": pname, "status": "invalid_key", "message": "API key cannot be empty."},
                    ).to_json())
                else:
                    result = await _validate_provider_key(pname, key, base_url=base_url)
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
                try:
                    if base_url or pname in engine_state.user_config.custom_base_urls or pname == "custom_openai":
                        engine_state.user_config.custom_base_urls[pname] = base_url
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
                        payload={"provider": pname, "success": False, "error": str(e)},
                    ).to_json())

            elif msg.type == EventType.PROVIDER_REMOVE_REQUEST:
                pname = msg.payload.get("provider", "")
                deleted = secret_store.delete_provider(pname)
                if pname in engine_state.user_config.provider_models:
                    del engine_state.user_config.provider_models[pname]
                if pname in engine_state.user_config.custom_base_urls:
                    del engine_state.user_config.custom_base_urls[pname]
                if engine_state.user_config.default_provider == pname:
                    engine_state.user_config.default_provider = "google_gemini"
                    engine_state.user_config.default_model = "gemini-2.5-flash"
                save_config(engine_state.user_config)
                engine_state.audit_logger.log_event("PROVIDER_REMOVED", {"provider": pname})
                await ws.send_text(Event(
                    type=EventType.PROVIDER_REMOVE_RESPONSE,
                    request_id=req_id,
                    payload={"provider": pname, "deleted": deleted},
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
                model_name = msg.payload.get("model", "")
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

                task_runner = asyncio.create_task(execute_local_download())

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
                providers_to_refresh = [target_provider] if (target_provider and target_provider in stored) else list(stored)
                cfg = engine_state.user_config

                for pname in providers_to_refresh:
                    try:
                        key = secret_store.load_provider(pname)
                        if key:
                            base_url = cfg.custom_base_urls.get(pname)
                            disc_models = await fetch_available_models(
                                provider_name=pname,
                                api_key=key,
                                base_url=base_url,
                                filter_reachability=True,
                                force_reachability=True,
                            )
                            if disc_models:
                                cfg.provider_models[pname] = disc_models
                                engine_state.model_registry.update_provider_models(pname, disc_models)
                                save_config(cfg)
                    except Exception as ref_err:
                        logger.error(f"Error during refresh models for {pname}: {ref_err}")

                updated_providers = await _build_provider_list_async(secret_store)
                await ws.send_text(Event(
                    type=EventType.REFRESH_MODELS_RESPONSE,
                    request_id=req_id,
                    payload={
                        "providers": updated_providers,
                        "default_provider": cfg.default_provider,
                        "default_model": cfg.default_model,
                    },
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
