"""Unified logging (5 Nov 2026) — engine half.

Two promises, pinned here:

- The WS receive loop traces incoming message TYPES (INFO for the
  allowlist, DEBUG otherwise; payloads never), and MODEL_SET_DEFAULT
  records the provider/model it actually applied — the record whose
  absence made the 5 Nov QA model-switch failure undiagnosable.
- build_diagnostics (bundle v2) ships a redacted crash.log tail and
  a 200-line audit tail; a missing crash log is an empty tail, not
  an error.

The WS harness follows the ws_env pattern of
test_rux_wave1_ws_discovery.py.
"""
import json
import logging
from contextlib import ExitStack
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

import aether_engine.config as config_module
import aether_engine.diagnostics as diagnostics_module
from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig, save_config
from aether_engine.diagnostics import build_diagnostics
from aether_engine.memory import session_store
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.policy import RoutingPolicy
from aether_engine.routing.registry import ModelEntry, ModelRegistry

WS_LOGGER = "aether_engine.routes.websocket"


# ---------------------------------------------------------------------------
# _log_ws_message_type — level routing
# ---------------------------------------------------------------------------

class TestWsMessageTypeTracing:
    @pytest.mark.parametrize("msg_type", [
        EventType.MODEL_SET_DEFAULT,
        EventType.TASK_PROGRESS,
        EventType.PROVIDER_LIST_REQUEST,
        EventType.DIAGNOSTICS_EXPORT_REQUEST,
        EventType.CONFIG_GET_REQUEST,
        EventType.ENVIRONMENT_DIAGNOSTIC_RUN_REQUEST,
    ])
    def test_allowlisted_types_log_at_info(self, caplog, msg_type):
        with caplog.at_level(logging.DEBUG, logger=WS_LOGGER):
            ws_module._log_ws_message_type(msg_type)
        records = [r for r in caplog.records if r.name == WS_LOGGER]
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        assert records[0].getMessage() == f"WS message received: {msg_type.value}"

    @pytest.mark.parametrize("msg_type", [
        EventType.SESSION_LIST_REQUEST,
        EventType.START_TASK,
        "SOME_FUTURE_TYPE",
    ])
    def test_other_types_log_at_debug(self, caplog, msg_type):
        with caplog.at_level(logging.DEBUG, logger=WS_LOGGER):
            ws_module._log_ws_message_type(msg_type)
        records = [r for r in caplog.records if r.name == WS_LOGGER]
        assert len(records) == 1
        assert records[0].levelno == logging.DEBUG
        name = msg_type.value if isinstance(msg_type, EventType) else msg_type
        assert records[0].getMessage() == f"WS message received: {name}"


# ---------------------------------------------------------------------------
# Receive loop — tracing + MODEL_SET_DEFAULT applied value, end to end
# ---------------------------------------------------------------------------

def _registry() -> ModelRegistry:
    return ModelRegistry([
        ModelEntry(provider="google_gemini", id="gemini-main", label="Gemini Main",
                   capabilities=["chat", "code", "tools"], priority=1),
        ModelEntry(provider="openai", id="gpt-main", label="GPT Main",
                   capabilities=["chat"], priority=1),
    ])


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    cfg = UserConfig(
        default_provider="google_gemini", default_model="gemini-main",
        workspace_roots=[str(tmp_path)])
    save_config(cfg, config_path)
    engine_state.initialize()
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        ws_module, "classify_intent", AsyncMock(return_value=None))
    registry = _registry()
    monkeypatch.setattr(engine_state, "model_registry", registry)
    monkeypatch.setattr(
        engine_state, "routing_policy", RoutingPolicy(registry=registry))
    monkeypatch.setattr(engine_state, "health_manager", ProviderHealthManager())
    monkeypatch.setattr(engine_state, "user_config", cfg)
    yield {"tmp_path": tmp_path, "config": cfg, "config_path": config_path}


def _connect_ws(stack):
    client = stack.enter_context(TestClient(app))
    return stack.enter_context(client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"},
    ))


def _collect_until(ws, wanted, limit=15):
    events = {}
    for _ in range(limit):
        ev = Event.from_json(ws.receive_text())
        events.setdefault(ev.type, ev)
        if all(t in events for t in wanted):
            break
    return events


class TestReceiveLoopTracing:
    def test_model_set_default_traced_and_applied_value_logged(self, ws_env, caplog):
        with ExitStack() as stack:
            ws = _connect_ws(stack)
            with caplog.at_level(logging.DEBUG, logger=WS_LOGGER):
                ws.send_text(Event(
                    type=EventType.MODEL_SET_DEFAULT,
                    payload={"provider": "openai", "model": "gpt-main"},
                ).to_json())
                events = _collect_until(ws, [EventType.MODEL_DEFAULT_CHANGED])
        assert events[EventType.MODEL_DEFAULT_CHANGED].payload["model"] == "gpt-main"

        messages = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == WS_LOGGER]
        assert (logging.INFO, "WS message received: MODEL_SET_DEFAULT") in messages
        assert (logging.INFO, "MODEL_SET_DEFAULT applied: openai/gpt-main") in messages

    def test_non_allowlisted_type_traced_at_debug(self, ws_env, caplog):
        with ExitStack() as stack:
            ws = _connect_ws(stack)
            with caplog.at_level(logging.DEBUG, logger=WS_LOGGER):
                ws.send_text(Event(
                    type=EventType.SESSION_LIST_REQUEST, payload={},
                ).to_json())
                _collect_until(ws, [EventType.SESSION_LIST_RESPONSE])
        traced = [r for r in caplog.records
                  if r.name == WS_LOGGER and "SESSION_LIST_REQUEST" in r.getMessage()]
        assert len(traced) == 1
        assert traced[0].levelno == logging.DEBUG


# ---------------------------------------------------------------------------
# build_diagnostics — bundle v2
# ---------------------------------------------------------------------------

def _bundle(tmp_path, **overrides):
    audit = tmp_path / "audit.log"
    if not audit.exists():
        audit.write_text('{"type": "ENGINE_EVENT"}\n')
    kwargs = {
        "user_config": UserConfig(),
        "health": {"gemini": {"status": "ok"}},
        "kg_count": 1,
        "episodic_count": 2,
        "audit_log_path": audit,
        "providers": ["gemini"],
        "crash_log_path": tmp_path / "crash.log",
    }
    kwargs.update(overrides)
    return build_diagnostics(**kwargs)


class TestDiagnosticsBundleV2:
    def test_crash_tail_present_and_capped_at_200(self, tmp_path):
        crash = tmp_path / "crash.log"
        crash.write_text("".join(f"crash line {i}\n" for i in range(250)))
        bundle = _bundle(tmp_path)
        assert len(bundle["crash_tail"]) == 200
        assert bundle["crash_tail"][0] == "crash line 50"
        assert bundle["crash_tail"][-1] == "crash line 249"

    def test_crash_tail_is_redacted(self, tmp_path):
        crash = tmp_path / "crash.log"
        crash.write_text("Traceback: provider blew up with key sk-abcdef1234567890 in args\n")
        bundle = _bundle(tmp_path)
        assert bundle["crash_tail"] == [
            "Traceback: provider blew up with key [REDACTED] in args"
        ]
        assert "sk-" not in json.dumps(bundle)

    def test_missing_crash_log_is_an_empty_tail(self, tmp_path):
        bundle = _bundle(tmp_path)
        assert bundle["crash_tail"] == []

    def test_default_crash_path_resolves_under_logs_dir(self, tmp_path, monkeypatch):
        # No crash_log_path argument: the bundle reads
        # logs_dir()/crash.log (the install_crash_logging home).
        monkeypatch.setattr(diagnostics_module, "logs_dir", lambda: tmp_path)
        (tmp_path / "crash.log").write_text("=== UNHANDLED ===\nValueError: nope\n")
        audit = tmp_path / "audit.log"
        audit.write_text('{"type": "ENGINE_EVENT"}\n')
        bundle = build_diagnostics(
            user_config=UserConfig(), health={}, kg_count=0, episodic_count=0,
            audit_log_path=audit, providers=[])
        assert bundle["crash_tail"] == ["=== UNHANDLED ===", "ValueError: nope"]

    def test_audit_tail_grows_to_200(self, tmp_path):
        audit = tmp_path / "audit.log"
        audit.write_text("".join(f'{{"n": {i}}}\n' for i in range(250)))
        bundle = _bundle(tmp_path, audit_log_path=audit)
        assert len(bundle["audit_tail"]) == 200
        assert bundle["audit_tail"][0] == '{"n": 50}'
