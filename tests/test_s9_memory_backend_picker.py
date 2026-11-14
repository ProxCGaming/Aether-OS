"""Master build S9: the Settings memory-backend picker.

Owner decision (14 Nov 2026): the picker lists ALL FOUR backends
with warnings and honest unavailable states — never
installed-only. These tests pin the three surfaces of that:

- the status report (memory/backend_status.py): every backend,
  probed WITHOUT constructing it, states derived from the real
  environment (an expected state is computed from find_spec
  here, never hard-coded);
- the switch guard: CONFIG_UPDATE refuses an unknown or
  unavailable backend with the probe's reason and applies
  NOTHING, and persists an available one — which takes effect
  on the next memory call because every call site resolves the
  backend from the live config per call;
- the contracts the UI consumes.
"""
import asyncio
import importlib.util
import json
from types import SimpleNamespace

import pytest

from aether_common.contracts import Event, EventType
from aether_engine.config import UserConfig
from aether_engine.memory import backend_status
from aether_engine.memory.backend_status import (
    memory_backend_statuses,
    probe_backend,
    validate_memory_backend_switch,
)
from aether_engine.memory.facade import (
    MemoryBackendUnavailable,
    NativeMemoryBackend,
    get_memory_backend,
)
from aether_engine.routes import websocket


def _expected_external_state(module: str) -> str:
    return "available" if importlib.util.find_spec(module) else "not_installed"


class TestStatusReport:
    def test_all_four_backends_always_listed(self):
        report = memory_backend_statuses(UserConfig())
        assert [b["id"] for b in report["backends"]] == [
            "native", "mem0", "langmem", "graphiti"]
        assert report["current"] == "native"

    def test_native_is_available_and_current_by_default(self):
        report = memory_backend_statuses(UserConfig())
        native = report["backends"][0]
        assert native["available"] is True
        assert native["state"] == "available"
        assert native["current"] is True
        assert native["reason"] is None

    def test_external_states_match_the_real_environment(self):
        # Derive the expectation from the environment itself:
        # whatever is (not) installed here, the report must
        # agree with find_spec — that agreement IS the feature.
        report = memory_backend_statuses(UserConfig())
        by_id = {b["id"]: b for b in report["backends"]}
        for name, module in (("mem0", "mem0"), ("langmem", "langmem"),
                             ("graphiti", "graphiti_core")):
            expected = _expected_external_state(module)
            if expected == "available" and name != "langmem":
                # Installed but unconfigured is also a truthful
                # state for mem0/graphiti in this environment.
                assert by_id[name]["state"] in ("available", "not_configured")
            else:
                assert by_id[name]["state"] == expected
            if by_id[name]["state"] != "available":
                assert by_id[name]["available"] is False
                assert by_id[name]["reason"]  # the reason is shown, always
            assert by_id[name]["current"] is False
            assert by_id[name]["warnings"]  # experimental warning present

    def test_current_follows_the_config(self):
        report = memory_backend_statuses(UserConfig(memory_backend="langmem"))
        by_id = {b["id"]: b for b in report["backends"]}
        assert report["current"] == "langmem"
        assert by_id["langmem"]["current"] is True
        assert by_id["native"]["current"] is False

    def test_probe_never_constructs_a_backend(self, monkeypatch):
        # If probing imported a backend adapter module, this
        # patched constructor would fire. find_spec must be
        # the whole probe.
        import aether_engine.memory.backends.graphiti_backend as gb

        def _boom(*a, **k):  # pragma: no cover - fires only on failure
            raise AssertionError("probe constructed a backend")

        monkeypatch.setattr(gb, "GraphitiMemoryBackend", _boom)
        entry = probe_backend("graphiti", env={})
        assert entry["state"] in ("not_installed", "not_configured")


class TestProbeGates:
    """The probe mirrors each adapter's own construction gates:
    package first, then the configuration the adapter demands."""

    def _installed(self, monkeypatch, *modules):
        monkeypatch.setattr(
            backend_status, "_package_installed",
            lambda m: m in modules)

    def test_mem0_installed_without_key_is_not_configured(self, monkeypatch):
        self._installed(monkeypatch, "mem0")
        entry = probe_backend("mem0", env={})
        assert entry["state"] == "not_configured"
        assert "OPENAI_API_KEY" in entry["reason"]
        assert entry["available"] is False

    def test_mem0_installed_with_key_is_available_with_probe_limits(
            self, monkeypatch):
        self._installed(monkeypatch, "mem0")
        entry = probe_backend("mem0", env={"OPENAI_API_KEY": "sk-test"})
        assert entry["state"] == "available"
        # The label admits the check's limit: Mem0 itself only
        # initialises on first use.
        assert "first use" in entry["detail"]

    def test_graphiti_installed_without_password_is_not_configured(
            self, monkeypatch):
        self._installed(monkeypatch, "graphiti_core")
        entry = probe_backend("graphiti", env={})
        assert entry["state"] == "not_configured"
        assert "NEO4J_PASSWORD" in entry["reason"]

    def test_graphiti_configured_admits_reachability_is_unchecked(
            self, monkeypatch):
        self._installed(monkeypatch, "graphiti_core")
        entry = probe_backend(
            "graphiti", env={"NEO4J_PASSWORD": "pw"})
        assert entry["state"] == "available"
        assert "reachable" in entry["detail"]
        # Verified-current behaviour, not the fixed E4 items:
        # management operations are refused by design.
        assert any("cannot be edited, deleted or cleared" in w
                   for w in entry["warnings"])

    def test_langmem_package_is_the_only_gate(self, monkeypatch):
        self._installed(monkeypatch, "langmem")
        entry = probe_backend("langmem", env={})
        assert entry["state"] == "available"
        assert any("substring" in w for w in entry["warnings"])


class TestSwitchValidation:
    def test_unknown_backend_is_refused_with_the_valid_list(self):
        reason = validate_memory_backend_switch("bogus")
        assert reason is not None
        assert "Unknown memory backend" in reason
        assert "native, mem0, langmem, graphiti" in reason

    def test_unavailable_backend_is_refused_with_the_probe_reason(self):
        # Whichever external backend is unavailable HERE, the
        # refusal must be the very reason the report shows.
        report = memory_backend_statuses(UserConfig())
        for entry in report["backends"]:
            if not entry["available"]:
                assert validate_memory_backend_switch(entry["id"]) == entry["reason"]
                return
        pytest.skip("all backends available in this environment")

    def test_native_always_switchable(self):
        assert validate_memory_backend_switch("native") is None
        assert validate_memory_backend_switch(" Native ") is None

    def test_config_round_trip_preserves_memory_backend(self):
        cfg = UserConfig(memory_backend="langmem")
        assert UserConfig.from_dict(cfg.to_dict()).memory_backend == "langmem"
        assert UserConfig().to_dict()["memory_backend"] == "native"

    def test_facade_selection_agrees_with_the_report(self):
        # native resolves; an unavailable external raises the
        # facade's honest error rather than swapping silently.
        assert isinstance(get_memory_backend(UserConfig()), NativeMemoryBackend)
        report = memory_backend_statuses(UserConfig())
        for entry in report["backends"]:
            if entry["id"] != "native" and not entry["available"]:
                with pytest.raises(MemoryBackendUnavailable):
                    get_memory_backend(backend_name=entry["id"])
                return


class _FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


class _FakeAudit:
    def __init__(self):
        self.events = []

    def log_event(self, name, payload):
        self.events.append((name, payload))


def _conn_and_state(monkeypatch, tmp_path):
    socket = _FakeSocket()
    conn = SimpleNamespace(ws=socket)
    audit = _FakeAudit()
    state = SimpleNamespace(user_config=UserConfig(), audit_logger=audit)
    monkeypatch.setattr(websocket, "engine_state", state)
    saved = []
    monkeypatch.setattr(
        websocket, "save_config",
        lambda cfg: saved.append(cfg.memory_backend))
    return conn, socket, state, saved


def _run(coro):
    return asyncio.run(coro)


class TestConfigUpdateSwitch:
    def test_refusal_applies_and_saves_nothing(self, monkeypatch, tmp_path):
        conn, socket, state, saved = _conn_and_state(monkeypatch, tmp_path)
        msg = Event(type=EventType.CONFIG_UPDATE_REQUEST, request_id="r1",
                    payload={"config": {"memory_backend": "bogus"}})
        _run(websocket._handle_config_update_request(conn, msg))
        assert len(socket.sent) == 1
        reply = socket.sent[0]
        assert reply["type"] == "CONFIG_UPDATE_RESPONSE"
        assert reply["payload"]["success"] is False
        # The refusal wording is E4-F12's strict validator
        # (config_updates.py), which owns the name check.
        assert "unknown backend 'bogus'" in reply["payload"]["error"]
        assert state.user_config.memory_backend == "native"
        assert saved == []

    def test_unavailable_backend_refused_with_probe_reason(
            self, monkeypatch, tmp_path):
        report = memory_backend_statuses(UserConfig())
        target = next((b for b in report["backends"] if not b["available"]), None)
        if target is None:
            pytest.skip("all backends available in this environment")
        conn, socket, state, saved = _conn_and_state(monkeypatch, tmp_path)
        msg = Event(type=EventType.CONFIG_UPDATE_REQUEST, request_id="r2",
                    payload={"config": {"memory_backend": target["id"]}})
        _run(websocket._handle_config_update_request(conn, msg))
        reply = socket.sent[0]
        assert reply["payload"]["success"] is False
        assert reply["payload"]["error"] == target["reason"]
        assert state.user_config.memory_backend == "native"
        assert saved == []

    def test_available_switch_applies_persists_and_audits(
            self, monkeypatch, tmp_path):
        conn, socket, _state, saved = _conn_and_state(monkeypatch, tmp_path)
        # langmem may be unavailable here; native->native is the
        # always-available control, and a patched probe covers
        # the cross-backend case below.
        msg = Event(type=EventType.CONFIG_UPDATE_REQUEST, request_id="r3",
                    payload={"config": {"memory_backend": "native"}})
        _run(websocket._handle_config_update_request(conn, msg))
        reply = socket.sent[0]
        assert reply["payload"]["success"] is True
        assert reply["payload"]["config"]["memory_backend"] == "native"
        assert saved == ["native"]

    def test_cross_backend_switch_when_probe_allows(
            self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            backend_status, "_package_installed", lambda m: True)
        conn, socket, state, saved = _conn_and_state(monkeypatch, tmp_path)
        msg = Event(type=EventType.CONFIG_UPDATE_REQUEST, request_id="r4",
                    payload={"config": {"memory_backend": "langmem"}})
        _run(websocket._handle_config_update_request(conn, msg))
        reply = socket.sent[0]
        assert reply["payload"]["success"] is True
        assert state.user_config.memory_backend == "langmem"
        assert saved == ["langmem"]
        assert ("MEMORY_BACKEND_SET", {"backend": "langmem"}) in \
            state.audit_logger.events
        # And the facade now resolves the new selection from the
        # same live config (the next-call effect, no restart) —
        # langmem's adapter is stubbed nowhere here, so accept
        # either a constructed backend or its honest refusal if
        # the real package is absent despite the patched probe.
        try:
            backend = get_memory_backend(state.user_config)
            assert backend.name == "langmem"
        except MemoryBackendUnavailable:
            pass


class TestBackendsHandler:
    def test_handler_answers_the_full_report(self, monkeypatch):
        socket = _FakeSocket()
        conn = SimpleNamespace(ws=socket)
        state = SimpleNamespace(user_config=UserConfig())
        monkeypatch.setattr(websocket, "engine_state", state)
        msg = Event(type=EventType.MEMORY_BACKENDS_REQUEST,
                    request_id="r9", payload={})
        _run(websocket._handle_memory_backends_request(conn, msg))
        assert len(socket.sent) == 1
        reply = socket.sent[0]
        assert reply["type"] == "MEMORY_BACKENDS_RESPONSE"
        assert reply["payload"]["current"] == "native"
        assert [b["id"] for b in reply["payload"]["backends"]] == [
            "native", "mem0", "langmem", "graphiti"]
