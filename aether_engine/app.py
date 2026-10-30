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

logger = logging.getLogger("aether_engine.app")

async def lifespan(_app: FastAPI):
    engine_state.initialize()
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


app = FastAPI(title="AETHER Engine", version="0.3.5", lifespan=lifespan)
app.include_router(ws_router)

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "schema_version": SCHEMA_VERSION})
