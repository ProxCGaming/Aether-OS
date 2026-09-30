"""FastAPI Engine application with authenticated loopback WebSocket, provider health, intelligent routing, safe fallbacks, local model task downloads, and capability check scheduling."""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from aether_common.contracts import SCHEMA_VERSION
from aether_engine.state import engine_state
from aether_engine.secrets.storage import SecretStore
from aether_engine.providers.discovery import fetch_available_models
from aether_engine.config import save_config
from aether_engine.routes.websocket import router as ws_router

logger = logging.getLogger("aether_engine.app")

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
app.include_router(ws_router)

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "schema_version": SCHEMA_VERSION})
