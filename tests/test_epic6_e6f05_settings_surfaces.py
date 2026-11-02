"""E6-F05 — the Settings surfaces handed over by Epics 1 and 2.

Epic 1 handed Epic 6 the Settings UI for spawn policy
(``supervisor_spawn_allowlist`` / ``supervisor_delegation_cap``),
prewalk (``prewalk_agents``), agent output schemas
(``agent_output_schemas``), compaction thresholds
(``compaction_trigger_messages`` / ``compaction_trigger_chars`` /
``compaction_keep_recent``) and layered config; Epic 2 handed off
path-rule editing (``path_rules``). The engine mechanisms existed,
but Settings had no controls for any of them and the CONFIG_UPDATE
whitelist did not accept those writes — users had to hand-edit
config files.

These tests drive the real socket:

  * every handed-off field round-trips through CONFIG_UPDATE /
    CONFIG_GET and persists to disk;
  * malformed writes are REFUSED with an error reply (never silently
    dropped, coerced, or half-applied);
  * each written field demonstrably reaches its live consumer — the
    supervisor spawn-policy hook, the compaction planner, the tool
    policy resolver, the structured-output reader, the prewalk
    membership the specialist nodes test, and ``config_layers``'
    project-layer merge for the layered-config editor.
"""
import json
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from starlette.testclient import TestClient

import aether_engine.config as config_module
import aether_engine.state as state_module
from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig, load_config, save_config
from aether_engine.config_layers import effective_user_config, load_project_layer
from aether_engine.langgraph.compaction import compaction_config_from, plan_compaction
from aether_engine.langgraph.structured import output_schema_for, parse_and_validate
from aether_engine.langgraph.supervisor_model import _spawn_policy_denial
from aether_engine.memory import session_store
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.registry import ModelRegistry
from aether_engine.secrets.dpapi import BaseProtector, SecretDecryptionError
from aether_engine.secrets.storage import SecretStore
from aether_engine.tools.policy import resolve_tool_policy
from aether_engine.tools.registry import Tool, ToolRisk

WRITE_TOOL = Tool(name="write_file", description="", risk=ToolRisk.WRITE)


class DummyProtector(BaseProtector):
    """The test_secret_store.py seam: reversible, no OS keyring."""

    def protect(self, data: bytes) -> bytes:
        return b"ENC:" + data

    def unprotect(self, data: bytes) -> bytes:
        if not data.startswith(b"ENC:"):
            raise SecretDecryptionError("Invalid header")
        return data[4:]


@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    """Isolated engine state: tmp config file, tmp secret store.

    Audit log path is set BEFORE initialize_auth() per the fixture
    law; the config lives in a tmp CONFIG_PATH file because
    initialize() reloads user_config from disk.
    """
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"

    store = SecretStore(db_path=tmp_path / "secrets.db",
                        protector=DummyProtector())
    monkeypatch.setattr(state_module, "SecretStore", lambda *a, **k: store)
    monkeypatch.setattr(ws_module, "SecretStore", lambda *a, **k: store)

    ws_root = tmp_path / "wsroot"
    ws_root.mkdir()

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    save_config(UserConfig(workspace_roots=[str(ws_root)]), config_path)

    monkeypatch.setattr(engine_state, "model_registry", ModelRegistry([]))
    monkeypatch.setattr(engine_state, "health_manager", ProviderHealthManager())
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))

    engine_state.initialize_auth()
    return {"tmp_path": tmp_path, "config_path": config_path,
            "ws_root": ws_root, "store": store}


def _recv_until(ws, event_type, max_events=12):
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        if ev.type == event_type:
            return ev
    raise AssertionError(f"no {event_type} received")


def _connect():
    return TestClient(app)


class _Socket:
    """Small context manager pairing a TestClient + websocket."""

    def __enter__(self):
        self._client = TestClient(app)
        self._client.__enter__()
        self._cm = self._client.websocket_connect(
            "/ws/tasks",
            headers={"Authorization": f"Bearer {engine_state.auth_token}"})
        self.ws = self._cm.__enter__()
        hello = Event.from_json(self.ws.receive_text())
        assert hello.type == EventType.HELLO
        return self.ws

    def __exit__(self, *exc):
        self._cm.__exit__(*exc)
        self._client.__exit__(*exc)
        return False


def _update(ws, config):
    ws.send_text(Event(
        type=EventType.CONFIG_UPDATE_REQUEST,
        payload={"config": config}).to_json())
    return _recv_until(ws, EventType.CONFIG_UPDATE_RESPONSE)


def _get(ws):
    ws.send_text(Event(type=EventType.CONFIG_GET_REQUEST, payload={}).to_json())
    return _recv_until(ws, EventType.CONFIG_GET_RESPONSE)


VALID_UPDATE = {
    "supervisor_spawn_allowlist": ["planner", "coder"],
    "supervisor_delegation_cap": 3,
    "prewalk_agents": ["planner"],
    "agent_output_schemas": {
        "coder": {"type": "object", "required": ["summary"],
                  "properties": {"summary": {"type": "string"}}},
    },
    "compaction_trigger_messages": 5,
    "compaction_trigger_chars": 900,
    "compaction_keep_recent": 2,
    "path_rules": [{"prefix": "/tmp/projects", "verdict": "Always Allow"}],
}


class TestHandedOffFieldsRoundTrip:
    def test_all_fields_round_trip_and_persist(self, settings_env):
        with _Socket() as ws:
            resp = _update(ws, dict(VALID_UPDATE))
            assert resp.payload["success"] is True
            echoed = resp.payload["config"]
            for key, value in VALID_UPDATE.items():
                assert echoed[key] == value, key
            got = _get(ws)
            for key, value in VALID_UPDATE.items():
                assert got.payload["config"][key] == value, key
        for key, value in VALID_UPDATE.items():
            assert getattr(engine_state.user_config, key) == value, key
        on_disk = load_config(settings_env["config_path"])
        for key, value in VALID_UPDATE.items():
            assert getattr(on_disk, key) == value, key

    def test_allowlist_none_and_empty_are_accepted(self, settings_env):
        with _Socket() as ws:
            resp = _update(ws, {"supervisor_spawn_allowlist": ["coder"]})
            assert resp.payload["success"] is True
            resp = _update(ws, {"supervisor_spawn_allowlist": None})
            assert resp.payload["success"] is True
            assert resp.payload["config"]["supervisor_spawn_allowlist"] is None
            resp = _update(ws, {"supervisor_spawn_allowlist": []})
            assert resp.payload["success"] is True
            assert resp.payload["config"]["supervisor_spawn_allowlist"] == []

    def test_legacy_fields_still_apply_alongside(self, settings_env):
        with _Socket() as ws:
            resp = _update(ws, {"disable_fallbacks": True,
                                "supervisor_delegation_cap": 4})
            assert resp.payload["success"] is True
            assert resp.payload["config"]["disable_fallbacks"] is True
            assert resp.payload["config"]["supervisor_delegation_cap"] == 4


REFUSALS = [
    # (config patch, field name expected in the error)
    ({"supervisor_spawn_allowlist": "coder"}, "supervisor_spawn_allowlist"),
    ({"supervisor_spawn_allowlist": ["coder", 3]}, "supervisor_spawn_allowlist"),
    ({"supervisor_spawn_allowlist": [""]}, "supervisor_spawn_allowlist"),
    ({"supervisor_spawn_allowlist": [True]}, "supervisor_spawn_allowlist"),
    ({"supervisor_delegation_cap": "eight"}, "supervisor_delegation_cap"),
    ({"supervisor_delegation_cap": True}, "supervisor_delegation_cap"),
    ({"supervisor_delegation_cap": -1}, "supervisor_delegation_cap"),
    ({"supervisor_delegation_cap": 2.5}, "supervisor_delegation_cap"),
    ({"prewalk_agents": "planner"}, "prewalk_agents"),
    ({"prewalk_agents": [None]}, "prewalk_agents"),
    ({"agent_output_schemas": []}, "agent_output_schemas"),
    ({"agent_output_schemas": {"coder": "object"}}, "agent_output_schemas"),
    ({"agent_output_schemas": {"coder": {"type": "banana"}}}, "agent_output_schemas"),
    ({"agent_output_schemas": {"coder": {"required": "summary"}}}, "agent_output_schemas"),
    ({"agent_output_schemas": {"coder": {"properties": []}}}, "agent_output_schemas"),
    ({"compaction_trigger_messages": 0}, "compaction_trigger_messages"),
    ({"compaction_trigger_messages": "40"}, "compaction_trigger_messages"),
    ({"compaction_trigger_chars": 0}, "compaction_trigger_chars"),
    ({"compaction_trigger_chars": -5}, "compaction_trigger_chars"),
    ({"compaction_keep_recent": 0}, "compaction_keep_recent"),
    ({"compaction_keep_recent": True}, "compaction_keep_recent"),
    ({"path_rules": {}}, "path_rules"),
    ({"path_rules": [{"verdict": "Deny"}]}, "path_rules"),
    ({"path_rules": [{"prefix": "", "verdict": "Deny"}]}, "path_rules"),
    ({"path_rules": [{"prefix": "/x", "verdict": "Sometimes"}]}, "path_rules"),
    ({"path_rules": [{"prefix": "/x", "verdict": "Deny", "tools": "write_file"}]}, "path_rules"),
    ({"path_rules": [{"prefix": "/x", "verdict": "Deny", "risks": ["fly"]}]}, "path_rules"),
    ({"path_rules": [{"prefix": "/x", "verdict": "Deny", "bogus": 1}]}, "path_rules"),
]


class TestHandedOffValidation:
    @pytest.mark.parametrize("patch,field", REFUSALS)
    def test_malformed_writes_are_refused_not_coerced(self, settings_env, patch, field):
        before = load_config(settings_env["config_path"])
        with _Socket() as ws:
            resp = _update(ws, patch)
        assert resp.payload["success"] is False
        assert field in resp.payload["error"]
        # Nothing was applied in memory or persisted to disk.
        current = getattr(engine_state.user_config, field)
        assert current == getattr(before, field)
        on_disk = load_config(settings_env["config_path"])
        assert getattr(on_disk, field) == getattr(before, field)

    def test_one_bad_field_refuses_the_whole_update(self, settings_env):
        with _Socket() as ws:
            resp = _update(ws, {"supervisor_delegation_cap": 3,
                                "path_rules": [{"verdict": "Deny"}]})
        assert resp.payload["success"] is False
        # The valid field riding along was NOT applied either.
        assert engine_state.user_config.supervisor_delegation_cap == 8


def _handoff_state(target):
    return {"messages": [
        HumanMessage(content="build it"),
        AIMessage(content="routing", tool_calls=[
            {"name": f"transfer_to_{target}", "args": {}, "id": f"call-{target}"}]),
    ]}


class TestConsumerEffects:
    def test_spawn_policy_written_via_settings_binds_the_hook(self, settings_env):
        with _Socket() as ws:
            assert _update(ws, {"supervisor_spawn_allowlist": ["planner"]}).payload["success"]
        cfg = {"configurable": {"user_config": engine_state.user_config}}
        denial = _spawn_policy_denial(_handoff_state("coder"), "coder", cfg)
        assert denial is not None
        assert "blocked by the spawn policy" in denial["messages"][0].content
        assert _spawn_policy_denial(_handoff_state("planner"), "planner", cfg) is None

    def test_delegation_cap_written_via_settings_binds_the_hook(self, settings_env):
        with _Socket() as ws:
            assert _update(ws, {"supervisor_delegation_cap": 0}).payload["success"]
        cfg = {"configurable": {"user_config": engine_state.user_config}}
        denial = _spawn_policy_denial(_handoff_state("planner"), "planner", cfg)
        assert denial is not None
        assert "delegation cap" in denial["messages"][0].content

    def test_compaction_thresholds_written_via_settings_drive_the_planner(self, settings_env):
        history = [{"role": "user" if i % 2 == 0 else "assistant",
                    "content": f"turn-{i} lorem"} for i in range(10)]
        default_cfg = compaction_config_from(UserConfig())
        assert plan_compaction(history, 0, default_cfg) is None
        with _Socket() as ws:
            assert _update(ws, {"compaction_trigger_messages": 5,
                                "compaction_trigger_chars": 100000,
                                "compaction_keep_recent": 2}).payload["success"]
        live_cfg = compaction_config_from(engine_state.user_config)
        assert live_cfg.trigger_messages == 5
        assert live_cfg.keep_recent == 2
        plan = plan_compaction(history, 0, live_cfg)
        assert plan is not None
        assert plan.folded_count > 0

    def test_output_schema_written_via_settings_reaches_the_reader(self, settings_env):
        schema = {"type": "object", "required": ["summary"],
                  "properties": {"summary": {"type": "string"}}}
        with _Socket() as ws:
            assert _update(ws, {"agent_output_schemas": {"coder": schema}}).payload["success"]
        run_cfg = {"configurable": {"user_config": engine_state.user_config}}
        assert output_schema_for(run_cfg, "coder") == schema
        assert output_schema_for(run_cfg, "planner") is None
        _, errors = parse_and_validate('{"summary": "done"}', schema)
        assert errors == []
        _, errors = parse_and_validate('{"other": 1}', schema)
        assert errors  # the schema genuinely binds

    def test_prewalk_written_via_settings_is_what_nodes_test(self, settings_env):
        with _Socket() as ws:
            assert _update(ws, {"prewalk_agents": ["planner"]}).payload["success"]
        # The specialist nodes' exact predicate (nodes/planner.py,
        # coder.py, researcher.py): membership in user_config.prewalk_agents.
        uc = engine_state.user_config
        assert "planner" in (getattr(uc, "prewalk_agents", None) or [])
        assert "coder" not in (getattr(uc, "prewalk_agents", None) or [])

    def test_path_rule_written_via_settings_changes_policy_decisions(self, settings_env, tmp_path):
        projects = tmp_path / "projects"
        args = {"path": str(projects / "main.py")}
        assert resolve_tool_policy(
            WRITE_TOOL, {}, args=args,
            path_rules=engine_state.user_config.path_rules) == "ask"
        with _Socket() as ws:
            assert _update(ws, {"path_rules": [
                {"prefix": str(projects), "verdict": "Always Allow"}]}).payload["success"]
        assert resolve_tool_policy(
            WRITE_TOOL, {}, args=args,
            path_rules=engine_state.user_config.path_rules) == "allow"
        with _Socket() as ws:
            assert _update(ws, {"path_rules": [
                {"prefix": str(tmp_path), "verdict": "Deny"}]}).payload["success"]
        assert resolve_tool_policy(
            WRITE_TOOL, {}, args=args,
            path_rules=engine_state.user_config.path_rules) == "deny"


def _project_get(ws, payload=None):
    ws.send_text(Event(
        type=EventType.PROJECT_CONFIG_GET_REQUEST,
        payload=payload or {}).to_json())
    return _recv_until(ws, EventType.PROJECT_CONFIG_GET_RESPONSE)


def _project_update(ws, payload):
    ws.send_text(Event(
        type=EventType.PROJECT_CONFIG_UPDATE_REQUEST,
        payload=payload).to_json())
    return _recv_until(ws, EventType.PROJECT_CONFIG_UPDATE_RESPONSE)


class TestProjectLayerEditor:
    def test_get_without_a_file(self, settings_env):
        with _Socket() as ws:
            resp = _project_get(ws)
        assert resp.payload["success"] is True
        assert resp.payload["workspace_root"] == str(settings_env["ws_root"])
        assert resp.payload["exists"] is False
        assert resp.payload["config"] is None
        assert resp.payload["parse_error"] is None
        # The editor must be able to warn about global-only keys.
        assert "path_rules" in resp.payload["forbidden_keys"]
        assert "supervisor_spawn_allowlist" in resp.payload["forbidden_keys"]

    def test_update_writes_the_layer_the_merge_consumes(self, settings_env):
        layer = {"compaction_trigger_messages": 7, "reasoning_effort": "high"}
        with _Socket() as ws:
            resp = _project_update(ws, {"content": json.dumps(layer)})
            assert resp.payload["success"] is True
            got = _project_get(ws)
        assert got.payload["exists"] is True
        assert got.payload["config"] == layer
        root = str(settings_env["ws_root"])
        assert load_project_layer(root) == layer
        effective = effective_user_config(engine_state.user_config, root)
        assert effective.compaction_trigger_messages == 7
        assert effective.reasoning_effort == "high"

    def test_forbidden_keys_in_the_layer_stay_global_only(self, settings_env):
        layer = {"path_rules": [{"prefix": "/x", "verdict": "Deny"}]}
        with _Socket() as ws:
            assert _project_update(
                ws, {"content": json.dumps(layer)}).payload["success"]
        effective = effective_user_config(
            engine_state.user_config, str(settings_env["ws_root"]))
        # config_layers strips it: the global (empty) rules stand.
        assert effective.path_rules == []

    def test_update_refuses_invalid_json_and_non_dict(self, settings_env):
        root = settings_env["ws_root"]
        with _Socket() as ws:
            resp = _project_update(ws, {"content": "{not json"})
            assert resp.payload["success"] is False
            resp = _project_update(ws, {"content": "[1, 2]"})
            assert resp.payload["success"] is False
        assert not (root / ".aether" / "config.json").exists()

    def test_update_refuses_an_unknown_workspace(self, settings_env, tmp_path):
        stranger = tmp_path / "not-a-workspace"
        stranger.mkdir()
        with _Socket() as ws:
            resp = _project_update(ws, {
                "workspace_root": str(stranger), "content": "{}"})
        assert resp.payload["success"] is False
        assert not (stranger / ".aether" / "config.json").exists()

    def test_get_reports_a_corrupt_file_honestly(self, settings_env):
        cfg_dir = settings_env["ws_root"] / ".aether"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_text("{broken", encoding="utf-8")
        with _Socket() as ws:
            resp = _project_get(ws)
        assert resp.payload["success"] is True
        assert resp.payload["exists"] is True
        assert resp.payload["config"] is None
        assert resp.payload["parse_error"]
        assert resp.payload["content"] == "{broken"

    def test_get_without_any_workspace(self, settings_env):
        # The app lifespan re-runs initialize() when the TestClient
        # starts, reloading the config from disk — so the empty roots
        # must be the persisted state, not just an in-memory edit.
        save_config(UserConfig(workspace_roots=[]), settings_env["config_path"])
        engine_state.user_config = load_config(settings_env["config_path"])
        with _Socket() as ws:
            resp = _project_get(ws)
        assert resp.payload["success"] is False
        assert "workspace" in resp.payload["error"].lower()
