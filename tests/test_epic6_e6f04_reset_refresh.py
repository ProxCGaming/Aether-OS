"""E6-F04 — a successful CONFIG_RESET saves defaults but must also
refresh the LIVE state that mirrors the config.

The reset handler replaced and saved ``engine_state.user_config`` and
stopped there. But ``active_provider`` / ``active_model`` /
``configured_providers`` are independent fields that only
``EngineState.initialize()`` derives (via startup validation), and the
skill manager's workspace roots are applied only there too. So after
a reset:

  * HELLO kept reporting the PRE-reset provider/model even though the
    saved config held the defaults;
  * the skill catalogue kept serving skills from workspace roots the
    reset had just removed (an explicit root refresh removed them);
  * ``configured_providers`` kept entries that only the pre-reset
    config's shape had made configured (a keyless custom provider).

These tests drive the real socket with real isolated persistence:
a tmp config file, a tmp secret store holding keys for both cloud
providers, a tmp skill manager, and a pre-reset config whose default
is openai / gpt-4o-mini with one workspace root carrying a skill and
one keyless custom provider. Disk contents after the reset are a
control — they were already correct and must stay exactly so.
"""
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

import aether_engine.config as config_module
import aether_engine.skills.manager as skills_module
import aether_engine.state as state_module
from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig, load_config, save_config
from aether_engine.memory import session_store
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.registry import ModelEntry, ModelRegistry
from aether_engine.secrets.dpapi import BaseProtector, SecretDecryptionError
from aether_engine.secrets.storage import SecretStore
from aether_engine.skills.manager import SkillManager

CUSTOM = "custom_local"
WS_SKILL = "ws-only-skill"
GLOBAL_SKILL = "global-keeper"


class DummyProtector(BaseProtector):
    """The test_secret_store.py seam: reversible, no OS keyring."""

    def protect(self, data: bytes) -> bytes:
        return b"ENC:" + data

    def unprotect(self, data: bytes) -> bytes:
        if not data.startswith(b"ENC:"):
            raise SecretDecryptionError("Invalid header")
        return data[4:]


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: {name} description\n---\n\nBody of {name}.\n"


def _registry() -> ModelRegistry:
    return ModelRegistry([
        ModelEntry(provider="openai", id="gpt-4o-mini", label="GPT-4o mini",
                   capabilities=["chat"], priority=1),
        ModelEntry(provider="google_gemini", id="gemini-3.5-flash",
                   label="Gemini 3.5 Flash", capabilities=["chat"], priority=1),
        ModelEntry(provider=CUSTOM, id="llama3:8b", label="Llama 3 8B (local)",
                   capabilities=["chat"], priority=1),
    ])


@pytest.fixture
def reset_env(tmp_path, monkeypatch):
    """Isolated engine state initialised from a non-default config.

    Audit log path is set BEFORE initialize_auth() per the fixture
    law; the config lives in a tmp CONFIG_PATH file because
    initialize() reloads user_config from disk.
    """
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"

    # Skill fixtures: one workspace-only skill, one global skill.
    ws_root = tmp_path / "wsroot"
    ws_skill_dir = ws_root / ".agents" / "skills" / WS_SKILL
    ws_skill_dir.mkdir(parents=True)
    (ws_skill_dir / "SKILL.md").write_text(_skill_md(WS_SKILL), encoding="utf-8")
    global_dir = tmp_path / "global_skills"
    global_skill_dir = global_dir / GLOBAL_SKILL
    global_skill_dir.mkdir(parents=True)
    (global_skill_dir / "SKILL.md").write_text(
        _skill_md(GLOBAL_SKILL), encoding="utf-8")
    manager = SkillManager(global_dir=global_dir, workspace_dirs=[])
    monkeypatch.setattr(skills_module, "GLOBAL_SKILL_MANAGER", manager)

    # Secret store: keys for both cloud providers (secrets survive a
    # reset by design — the audit verified the fixture key held).
    store = SecretStore(db_path=tmp_path / "secrets.db",
                        protector=DummyProtector())
    store.save_provider("openai", "sk-test-openai")
    store.save_provider("google_gemini", "sk-test-gemini")
    monkeypatch.setattr(state_module, "SecretStore", lambda *a, **k: store)
    monkeypatch.setattr(ws_module, "SecretStore", lambda *a, **k: store)

    # Pre-reset config: openai default, one workspace root, one
    # keyless custom provider, non-default advanced values.
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    cfg = UserConfig(
        default_provider="openai",
        default_model="gpt-4o-mini",
        workspace_roots=[str(ws_root)],
        provider_models={CUSTOM: ["llama3:8b"]},
        custom_base_urls={CUSTOM: "http://127.0.0.1:8080/v1"},
        custom_provider_types={CUSTOM: "openai_compatible"},
        disable_fallbacks=True,
        reasoning_effort="high",
    )
    save_config(cfg, config_path)

    monkeypatch.setattr(engine_state, "model_registry", _registry())
    monkeypatch.setattr(engine_state, "health_manager", ProviderHealthManager())
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))

    engine_state.initialize_auth()
    return {
        "tmp_path": tmp_path,
        "config_path": config_path,
        "store": store,
        "manager": manager,
        "ws_root": ws_root,
    }


def _recv_until(ws, event_type, max_events=12):
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        if ev.type == event_type:
            return ev
    raise AssertionError(f"no {event_type} received")


def _connect():
    return TestClient(app)


class TestResetRefreshesLiveState:
    def test_pre_reset_state_is_the_configured_one(self, reset_env):
        # Control: initialisation derives the live fields from the
        # pre-reset config — HELLO reports openai / gpt-4o-mini, the
        # catalogue serves the workspace skill, and the keyless
        # custom provider is configured.
        assert engine_state.active_provider == "openai"
        assert engine_state.active_model == "gpt-4o-mini"
        assert CUSTOM in engine_state.configured_providers
        names = {s.name for s in reset_env["manager"].list_all()}
        assert WS_SKILL in names and GLOBAL_SKILL in names

        with _connect() as client, client.websocket_connect(
            "/ws/tasks",
            headers={"Authorization": f"Bearer {engine_state.auth_token}"},
        ) as ws:
            hello = Event.from_json(ws.receive_text())
            assert hello.type == EventType.HELLO
            assert hello.payload["active_provider"] == "openai"
            assert hello.payload["active_model"] == "gpt-4o-mini"

    def test_reset_refreshes_active_fields_roots_and_catalogue(self, reset_env):
        manager = reset_env["manager"]
        with _connect() as client, client.websocket_connect(
            "/ws/tasks",
            headers={"Authorization": f"Bearer {engine_state.auth_token}"},
        ) as ws:
            hello = Event.from_json(ws.receive_text())
            assert hello.payload["active_provider"] == "openai"

            ws.send_text(Event(
                type=EventType.CONFIG_RESET_REQUEST, payload={}).to_json())
            resp = _recv_until(ws, EventType.CONFIG_RESET_RESPONSE)
            assert resp.payload["success"] is True
            assert resp.payload["config"]["default_provider"] == "google_gemini"
            assert resp.payload["config"]["workspace_roots"] == []

            # The live fields follow the reset config — no restart.
            assert engine_state.active_provider == "google_gemini"
            assert engine_state.active_model == "gemini-3.5-flash"
            # The keyless custom was configured only by the pre-reset
            # config's shape; the reset derivation drops it, while
            # both keyed providers remain configured.
            assert CUSTOM not in engine_state.configured_providers
            assert "openai" in engine_state.configured_providers
            assert "google_gemini" in engine_state.configured_providers

            # The catalogue no longer serves the removed root's
            # skill; the global (kept) skill survives.
            ws.send_text(Event(
                type=EventType.SKILL_LIST_REQUEST, payload={}).to_json())
            skills = _recv_until(ws, EventType.SKILL_LIST_RESPONSE)
            names = {s["name"] for s in skills.payload["skills"]}
            assert WS_SKILL not in names
            assert GLOBAL_SKILL in names
            assert WS_SKILL not in manager.catalogue()

        # A fresh connection's HELLO reports the reset values.
        with _connect() as client, client.websocket_connect(
            "/ws/tasks",
            headers={"Authorization": f"Bearer {engine_state.auth_token}"},
        ) as ws:
            hello = Event.from_json(ws.receive_text())
            assert hello.payload["active_provider"] == "google_gemini"
            assert hello.payload["active_model"] == "gemini-3.5-flash"

    def test_reset_disk_and_secrets_unchanged_in_scope(self, reset_env):
        # Controls the audit verified hold and must keep holding:
        # the saved file is exactly the fresh defaults, and the
        # secret store is untouched by a reset.
        with _connect() as client, client.websocket_connect(
            "/ws/tasks",
            headers={"Authorization": f"Bearer {engine_state.auth_token}"},
        ) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(Event(
                type=EventType.CONFIG_RESET_REQUEST, payload={}).to_json())
            resp = _recv_until(ws, EventType.CONFIG_RESET_RESPONSE)
            assert resp.payload["success"] is True

        on_disk = load_config(reset_env["config_path"])
        assert on_disk == UserConfig()
        assert set(reset_env["store"].list_providers()) == {
            "openai", "google_gemini"}
