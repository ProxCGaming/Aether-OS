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

logger = logging.getLogger("aether_engine.state")

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
                
        # Initialize skill manager roots
        from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER
        GLOBAL_SKILL_MANAGER.update_workspace_roots(self.user_config.workspace_roots or [])

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
