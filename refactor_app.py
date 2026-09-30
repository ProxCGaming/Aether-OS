import os

app_file = 'e:/AI projects/AETHER/aether_engine/app.py'
with open(app_file, 'r', encoding='utf-8') as f:
    lines = f.readlines()

def get_lines(start, end):
    # start and end are 1-indexed inclusive
    return "".join(lines[start-1:end])

imports_head = """import asyncio
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
"""

state_py = imports_head + """
logger = logging.getLogger("aether_engine.state")

""" + get_lines(86, 149) + "\nengine_state = EngineState()\n"

core_py = imports_head + """
from aether_engine.state import engine_state
logger = logging.getLogger("aether_engine.orchestration.core")

""" + get_lines(211, 287) + "\n" + get_lines(290, 341) + "\n" + get_lines(347, 410) + "\n" + get_lines(413, 487)

websocket_py = imports_head + """
from aether_engine.state import engine_state
from aether_engine.orchestration.core import _build_provider_list_async, _build_provider_list, _validate_provider_key, _create_provider_instance
logger = logging.getLogger("aether_engine.routes.websocket")

router = APIRouter()

""" + get_lines(491, 500) + "\n" + get_lines(507, 511) + "\n" + get_lines(515, 1704).replace('@app.websocket("/ws")', '@router.websocket("/ws")') + "\n" + get_lines(1709, 1777).replace('@app.get', '@router.get').replace('@app.post', '@router.post').replace('@app.delete', '@router.delete')

new_app_py = """\"\"\"FastAPI Engine application with authenticated loopback WebSocket, provider health, intelligent routing, safe fallbacks, local model task downloads, and capability check scheduling.\"\"\"
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

""" + get_lines(159, 197) + """

app = FastAPI(title="AETHER Engine", version="0.3.5", lifespan=lifespan)
app.include_router(ws_router)

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "schema_version": SCHEMA_VERSION})
"""

import os
os.makedirs('e:/AI projects/AETHER/aether_engine/orchestration', exist_ok=True)
os.makedirs('e:/AI projects/AETHER/aether_engine/routes', exist_ok=True)

with open('e:/AI projects/AETHER/aether_engine/state.py', 'w', encoding='utf-8') as f:
    f.write(state_py)

with open('e:/AI projects/AETHER/aether_engine/orchestration/core.py', 'w', encoding='utf-8') as f:
    f.write(core_py)

with open('e:/AI projects/AETHER/aether_engine/routes/websocket.py', 'w', encoding='utf-8') as f:
    f.write(websocket_py)

with open(app_file, 'w', encoding='utf-8') as f:
    f.write(new_app_py)

print("Modularization complete!")
