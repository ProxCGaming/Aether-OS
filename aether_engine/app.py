"""FastAPI Engine application with authenticated loopback WebSocket, provider health, intelligent routing, safe fallbacks, local model task downloads, and capability check scheduling."""
import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from aether_common.contracts import SCHEMA_VERSION
from aether_engine.config import save_config
from aether_engine.providers.discovery import fetch_available_models
from aether_engine.routes.websocket import router as ws_router
from aether_engine.secrets.storage import SecretStore
from aether_engine.state import engine_state
from aether_engine.version import APP_VERSION

logger = logging.getLogger("aether_engine.app")

async def lifespan(_app: FastAPI):
    # Epic 10, E10-04: unhandled exceptions land in ~/.aether/logs/crash.log.
    from aether_engine.diagnostics import install_crash_logging

    install_crash_logging()
    engine_state.initialize()

    # Epic 9: warm the deferred litellm import in a background thread.
    # The import costs ~4s; paying it here (overlapped with the rest
    # of startup) keeps engine start fast AND keeps the first real
    # provider call from paying it either.
    try:
        import threading

        from aether_engine.providers.litellm_provider import warm_litellm

        threading.Thread(target=warm_litellm, daemon=True, name="litellm-warmup").start()
    except Exception as e:  # noqa: BLE001 — startup boundary: warm-up is best-effort; the lazy import still happens on first use
        logger.warning(f"LiteLLM warm-up failed to start: {e}")
    logger.info(
        f"AETHER Engine initialized. Active: {engine_state.active_provider}/{engine_state.active_model}. "
        f"Auth token written to {engine_state.token_path}"
    )

    # Issue 004: auto-connect enabled MCP servers in the background.
    # start_auto_connect() returns immediately — it never blocks startup —
    # and each server connects in its own owner task, so one dead server
    # cannot block startup or the others. Local import so tests that
    # patch aether_engine.mcp.registry.GLOBAL_MCP_REGISTRY get the patch.
    try:
        from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY

        GLOBAL_MCP_REGISTRY.start_auto_connect()
    except Exception as e:  # noqa: BLE001 — startup boundary: MCP auto-connect is best-effort and must never block engine startup
        logger.warning(f"MCP auto-connect failed to start: {e}")

    # Epic 4, E4-04: backfill embeddings for episodes stored while
    # the embedding model was unavailable. Detached and bounded —
    # startup never waits on it, and failures are audit-logged by
    # the episodic store itself. Native backend only: external
    # backends own their embedding story.
    try:
        if (engine_state.user_config.memory_backend or "native") == "native":
            import asyncio

            from aether_engine.memory.episodic import backfill_episodic_embeddings

            asyncio.create_task(backfill_episodic_embeddings(limit=200))
    except Exception as e:  # noqa: BLE001 — startup boundary: backfill is best-effort and must never block engine startup
        logger.warning(f"Episodic embedding backfill failed to start: {e}")

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
                disc = await fetch_available_models(pname, key, base_url=base_url)
                if disc.models:
                    cfg.provider_models[pname] = disc.models
                    cfg.provider_excluded_models[pname] = disc.excluded
                    engine_state.model_registry.update_provider_models(pname, disc.models)
                    if cfg.default_provider == pname and (
                        cfg.default_model not in disc.models or "lite" in cfg.default_model
                    ):
                        cfg.default_model = disc.models[0]
                        engine_state.active_model = disc.models[0]
                    save_config(cfg)
        except Exception as e:  # noqa: BLE001 — per-provider isolation: one provider's discovery failure must not abort startup discovery for the rest
            logger.warning(f"Failed startup model discovery for {pname}: {e}")

    yield
    # Issue 004: close any owner-task MCP sessions before shutdown.
    try:
        from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY

        await GLOBAL_MCP_REGISTRY.stop_auto_connect()
    except Exception as e:  # noqa: BLE001 — shutdown cleanup boundary: a failed MCP stop must not prevent engine shutdown
        logger.warning(f"MCP auto-connect stop failed: {e}")
    engine_state.shutdown()
    engine_state.audit_logger.log_event("ENGINE_STOPPED")
    logger.info("AETHER Engine stopped.")


app = FastAPI(title="AETHER Engine", version=APP_VERSION, lifespan=lifespan)
app.include_router(ws_router)

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "schema_version": SCHEMA_VERSION})
