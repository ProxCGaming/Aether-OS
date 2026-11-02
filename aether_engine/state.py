import logging
from pathlib import Path

from aether_common.auth import (
    DEFAULT_TOKEN_PATH,
    generate_token,
    write_token,
)
from aether_engine.audit import AuditLogger
from aether_engine.config import (
    UserConfig,
    load_config,
)
from aether_engine.models.local_models import OllamaManager
from aether_engine.routing.health import (
    GLOBAL_HEALTH_MANAGER,
)
from aether_engine.routing.policy import (
    GLOBAL_ROUTING_POLICY,
)
from aether_engine.routing.registry import (
    GLOBAL_MODEL_REGISTRY,
)
from aether_engine.routing.startup import (
    validate_startup_configuration,
)
from aether_engine.scheduler.capability_jobs import CapabilitySchedulerManager
from aether_engine.secrets.storage import SecretStore

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
        self.plugin_installer = PluginInstaller(Path.home() / ".aether" / "plugins", audit_logger=self.audit_logger)
        self.active_provider: str = "google_gemini"
        self.active_model: str = "gemini-2.5-flash"
        self.configured_providers: list[str] = []
        self.disabled_nodes: set[str] = set()

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
