"""Wave 1, Slice 2b — RUX-B-03 / RUX-B-05: capability check and
environment diagnostics off the WS message/event loop.

- RUX-B-03: CAPABILITY_CHECK_RUN_NOW runs in a background task. An
  INSTANT message (MODEL_SET_DEFAULT) sent right behind a check whose
  run_now hangs must be answered BEFORE the check response. The run
  is bounded by CAPABILITY_CHECK_RUN_BUDGET_SECONDS: past the budget
  the response is a FAILED run record whose error_log is the timeout.
- RUX-B-05: ENVIRONMENT_DIAGNOSTIC_RUN_REQUEST runs the synchronous
  probes via asyncio.to_thread. The handler still awaits the result
  (the per-connection loop stays sequential), but the EVENT loop no
  longer freezes: while a slow diagnostic runs on one connection,
  /health and a second connection are answered immediately. The
  response payload is byte-for-byte the pre-fix shape.

Harness follows the slice-2a ws_env pattern (tmp CONFIG_PATH,
scripted registry/health manager, mocked SecretStore in the websocket
module's namespace), plus a CapabilitySchedulerManager on a tmp DB so
run history assertions are deterministic and the real
~/.aether/capabilities.db is never touched. No live network: run_now
and the diagnostics probes are scripted fakes, except one test that
runs the real diagnostics to pin the payload contract.
"""
import asyncio
import time
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

import aether_engine.config as config_module
from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig, save_config
from aether_engine.memory import session_store
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.policy import RoutingPolicy
from aether_engine.routing.registry import ModelEntry, ModelRegistry
from aether_engine.scheduler.capability_jobs import CapabilitySchedulerManager

PROVIDERS = ["google_gemini", "openai", "anthropic"]

RECORD_KEYS = {
    "id", "method", "started_at", "finished_at",
    "models_tested", "status", "cost_estimate", "error_log",
}


def _registry() -> ModelRegistry:
    return ModelRegistry([
        ModelEntry(provider="google_gemini", id="gemini-main", label="Gemini Main",
                   capabilities=["chat", "code", "tools"], priority=1),
        ModelEntry(provider="openai", id="gpt-main", label="GPT Main",
                   capabilities=["chat"], priority=1),
        ModelEntry(provider="anthropic", id="claude-main", label="Claude Main",
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
    engine_state.initialize_auth()
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
    # Fresh scheduler on a tmp DB: no recorded runs, so startup
    # misfire recovery stays idle and history is exactly what the
    # test itself records.
    mgr = CapabilitySchedulerManager(db_path=tmp_path / "capabilities.db")
    monkeypatch.setattr(engine_state, "scheduler_manager", mgr)
    yield {"tmp_path": tmp_path, "config": cfg, "config_path": config_path,
           "scheduler": mgr}


def _mock_store(stack, providers):
    mock_store = stack.enter_context(
        patch.object(ws_module, "SecretStore")).return_value
    mock_store.list_providers.return_value = providers
    mock_store.load_provider.return_value = "fake-key"
    return mock_store


def _connect_ws(stack):
    client = stack.enter_context(TestClient(app))
    return stack.enter_context(client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"},
    ))


def _send_instant_probe(ws):
    ws.send_text(Event(
        type=EventType.MODEL_SET_DEFAULT,
        payload={"provider": "google_gemini", "model": "gemini-2.5-pro"},
    ).to_json())


def _collect_until(ws, wanted, limit=12):
    """Read events until every type in `wanted` has been seen."""
    seen = []
    events = {}
    for _ in range(limit):
        ev = Event.from_json(ws.receive_text())
        seen.append(ev.type)
        events.setdefault(ev.type, ev)
        if all(t in events for t in wanted):
            break
    return seen, events


def _success_record(method="Option A"):
    now = time.time()
    return {
        "id": "run-1",
        "method": method,
        "started_at": now,
        "finished_at": now + 0.1,
        "models_tested": 3,
        "status": "SUCCESS",
        "cost_estimate": 0.018,
        "error_log": None,
    }


# ---------------------------------------------------------------------------
# RUX-B-03 — CAPABILITY_CHECK_RUN_NOW must not block the message loop
# ---------------------------------------------------------------------------


class TestCapabilityCheckOffLoop:
    def test_instant_message_answered_before_check_response(self, ws_env):
        async def slow_run_now(method=None):
            await asyncio.sleep(3)
            return _success_record(method or "Option A")

        monkeypatch_mgr = ws_env["scheduler"]
        stack = ExitStack()
        _mock_store(stack, PROVIDERS)
        with stack:
            ws = _connect_ws(stack)
            ws.receive_text()  # HELLO
            with patch.object(monkeypatch_mgr, "run_now",
                              AsyncMock(side_effect=slow_run_now)):
                ws.send_text(Event(
                    type=EventType.CAPABILITY_CHECK_RUN_NOW,
                    payload={"method": "Option A"},
                ).to_json())
                _send_instant_probe(ws)

                seen, events = _collect_until(ws, [
                    EventType.MODEL_DEFAULT_CHANGED,
                    EventType.CAPABILITY_CHECK_HISTORY_RESPONSE,
                ])

            assert EventType.MODEL_DEFAULT_CHANGED in seen
            assert EventType.CAPABILITY_CHECK_HISTORY_RESPONSE in seen
            # The whole point of RUX-B-03: the instant message is NOT
            # stuck behind the check's minutes of self-tests.
            assert seen.index(EventType.MODEL_DEFAULT_CHANGED) < seen.index(
                EventType.CAPABILITY_CHECK_HISTORY_RESPONSE)
            resp = events[EventType.CAPABILITY_CHECK_HISTORY_RESPONSE]
            assert resp.payload["latest_run"]["status"] == "SUCCESS"
            assert resp.payload["latest_run"]["models_tested"] == 3
            assert set(resp.payload["latest_run"]) == RECORD_KEYS
            assert "summary" in resp.payload
            assert "history" in resp.payload

    def test_run_over_budget_ends_as_failed_timeout_run(self, ws_env, monkeypatch):
        monkeypatch.setattr(
            ws_module, "CAPABILITY_CHECK_RUN_BUDGET_SECONDS", 0.3)

        async def hanging_run_now(method=None):
            await asyncio.sleep(10)
            return _success_record(method or "Option A")

        mgr = ws_env["scheduler"]
        stack = ExitStack()
        _mock_store(stack, PROVIDERS)
        with stack:
            ws = _connect_ws(stack)
            ws.receive_text()  # HELLO
            with patch.object(mgr, "run_now",
                              AsyncMock(side_effect=hanging_run_now)):
                start = time.monotonic()
                ws.send_text(Event(
                    type=EventType.CAPABILITY_CHECK_RUN_NOW,
                    payload={"method": "Option C"},
                ).to_json())
                _, events = _collect_until(
                    ws, [EventType.CAPABILITY_CHECK_HISTORY_RESPONSE])
                elapsed = time.monotonic() - start

        resp = events[EventType.CAPABILITY_CHECK_HISTORY_RESPONSE]
        # The budget cut the run off instead of hanging the socket.
        assert elapsed < 5.0
        latest = resp.payload["latest_run"]
        assert set(latest) == RECORD_KEYS
        assert latest["status"] == "FAILED"
        assert latest["method"] == "Option C"
        assert latest["models_tested"] == 0
        assert "timed out" in latest["error_log"]
        # Persisted like any other failed run: history and the
        # summary's last_run are the timeout run itself.
        assert resp.payload["history"][0]["id"] == latest["id"]
        assert resp.payload["history"][0]["status"] == "FAILED"
        assert resp.payload["summary"]["last_run"]["id"] == latest["id"]


# ---------------------------------------------------------------------------
# RUX-B-05 — ENVIRONMENT_DIAGNOSTIC_RUN must not freeze the event loop
# ---------------------------------------------------------------------------


CANNED_DIAG_LOGS = [
    {"target_name": "Python Environment", "status": "OK",
     "details": "v3.12.0", "timestamp": 1700000000.0},
    {"target_name": "Terminal Access", "status": "FAILED",
     "details": "Process exited with error", "timestamp": 1700000001.0},
    {"target_name": "SQLite Storage", "status": "OK",
     "details": "Read/Write access verified", "timestamp": 1700000002.0},
]


class TestEnvironmentDiagnosticOffLoop:
    def test_event_loop_stays_responsive_during_diagnostic(self, ws_env):
        def slow_diagnostics():
            time.sleep(3)
            return [dict(entry) for entry in CANNED_DIAG_LOGS]

        stack = ExitStack()
        _mock_store(stack, PROVIDERS)
        stack.enter_context(patch.object(
            ws_module, "run_environment_diagnostics", slow_diagnostics))
        with stack:
            client = stack.enter_context(TestClient(app))
            ws_a = stack.enter_context(client.websocket_connect(
                "/ws/tasks",
                headers={"Authorization": f"Bearer {engine_state.auth_token}"},
            ))
            ws_a.receive_text()  # HELLO
            ws_a.send_text(Event(
                type=EventType.ENVIRONMENT_DIAGNOSTIC_RUN_REQUEST,
            ).to_json())

            # While the 3 s diagnostic runs: /health answers at once.
            # On the pre-fix code the sync probes froze the event
            # loop, so this request waited out the whole diagnostic.
            start = time.monotonic()
            health = client.get("/health")
            health_elapsed = time.monotonic() - start
            assert health.status_code == 200
            assert health_elapsed < 2.0

            # …and a second connection is served immediately too.
            ws_b = stack.enter_context(client.websocket_connect(
                "/ws/tasks",
                headers={"Authorization": f"Bearer {engine_state.auth_token}"},
            ))
            ws_b.receive_text()  # HELLO
            start = time.monotonic()
            _send_instant_probe(ws_b)
            probe = Event.from_json(ws_b.receive_text())
            probe_elapsed = time.monotonic() - start
            assert probe.type == EventType.MODEL_DEFAULT_CHANGED
            assert probe_elapsed < 2.0

            # The first connection's response still lands, with the
            # payload exactly the pre-fix shape and content.
            _, events = _collect_until(
                ws_a, [EventType.ENVIRONMENT_DIAGNOSTIC_RESPONSE])
            resp = events[EventType.ENVIRONMENT_DIAGNOSTIC_RESPONSE]
            assert resp.payload == {
                "summary": {"failed_count": 1},
                "history": CANNED_DIAG_LOGS,
            }

    def test_real_diagnostics_payload_shape_unchanged(self, ws_env):
        stack = ExitStack()
        _mock_store(stack, PROVIDERS)
        with stack:
            ws = _connect_ws(stack)
            ws.receive_text()  # HELLO
            ws.send_text(Event(
                type=EventType.ENVIRONMENT_DIAGNOSTIC_RUN_REQUEST,
            ).to_json())
            _, events = _collect_until(
                ws, [EventType.ENVIRONMENT_DIAGNOSTIC_RESPONSE])

        resp = events[EventType.ENVIRONMENT_DIAGNOSTIC_RESPONSE]
        assert set(resp.payload) == {"summary", "history"}
        history = resp.payload["history"]
        assert [e["target_name"] for e in history] == [
            "Python Environment",
            "Terminal Access",
            "SQLite Storage",
            "Ollama Local Service",
            "Web Search API",
        ]
        for entry in history:
            assert entry["status"] in ("OK", "WARN", "FAILED")
            assert isinstance(entry["details"], str) and entry["details"]
            assert isinstance(entry["timestamp"], float)
        failed = sum(1 for e in history if e["status"] == "FAILED")
        assert resp.payload["summary"] == {"failed_count": failed}
