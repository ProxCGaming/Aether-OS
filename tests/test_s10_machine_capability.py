"""Master build S10: hardware capability detection + matrix.

The audit (G1) found no hardware probe and no capability
matrix at all — ``aether_engine/capability/`` is LLM benchmarks,
not machines. These tests pin the three surfaces of the new
``aether_engine/machine_capability.py``:

- the probes: individually failure-isolated — a broken probe
  contributes "unknown", and the report is always complete;
- the matrix: a PURE function over synthetic machine dicts
  (no I/O), so every verdict can be pinned exactly, including
  the degraded outcomes;
- the report plumbing: TTL cache (memory, then the persisted
  copy), re-probe on force/staleness, and the WS handler's
  response shape. The dispatch census lives in
  test_wave5_ws_task_exec_split.py.
"""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from aether_common.contracts import Event, EventType
from aether_engine import machine_capability as mc
from aether_engine.routes import websocket

_GIB = 1024 ** 3


def _machine(**overrides):
    """A complete, healthy synthetic machine; tests override
    the one slice they are judging."""
    machine = {
        "platform": {"system": "Linux", "release": "6.8",
                     "architecture": "x86_64", "python": "3.12.3"},
        "cpu": {"logical_cores": 8, "model": "Synthetic CPU"},
        "ram": {"total_bytes": 16 * _GIB},
        "disk": {"path": "/home/test/.aether",
                 "total_bytes": 500 * _GIB, "free_bytes": 200 * _GIB},
        "gpu": None,
        "microphone": {"state": "unknown", "detail": "n/a"},
        "network": {"state": "online", "reachable": ["api.openai.com"]},
        "ollama_reachable": True,
        "voice_dependencies": {
            "faster_whisper": True, "kokoro": True,
            "onnxruntime": True, "numpy": True, "aiortc": True},
        "browser": {"playwright_installed": True, "chromium_found": True},
    }
    machine.update(overrides)
    return machine


def _verdict(machine, feature_id):
    return next(f for f in mc.capability_matrix(machine)
                if f["id"] == feature_id)


class TestProbeIsolation:
    def test_gpu_probe_explosion_yields_unknown_and_full_report(
            self, monkeypatch):
        # The GPU probe's subprocess seam explodes (worse than
        # a missing binary): the field goes unknown and every
        # other section of the report is still present.
        def _boom(*a, **k):
            raise RuntimeError("subprocess layer is broken")

        monkeypatch.setattr(mc, "_run_command", _boom)
        machine = mc.probe_machine()
        assert machine["gpu"] is None
        for key in ("platform", "cpu", "ram", "disk", "gpu",
                    "microphone", "network", "ollama_reachable",
                    "voice_dependencies", "browser"):
            assert key in machine

    def test_network_probe_explosion_yields_unknown(self, monkeypatch):
        def _boom(*a, **k):
            raise ValueError("socket layer is broken")

        monkeypatch.setattr(mc, "_connect_error", _boom)
        machine = mc.probe_machine()
        assert machine["network"] == {"state": "unknown", "reachable": []}
        assert machine["ollama_reachable"] is None

    def test_real_probe_report_shape(self):
        # The real probes on THIS machine: values may be
        # measured or unknown, but the shape is contractual.
        report = mc.build_report()
        assert isinstance(report["probed_at"], float)
        machine = report["machine"]
        # S19: the probe gains a "desktop" section (the UIA
        # package gate for desktop observe).
        assert set(machine) == {
            "platform", "cpu", "ram", "disk", "gpu", "microphone",
            "network", "ollama_reachable", "voice_dependencies",
            "browser", "desktop"}
        assert [f["id"] for f in report["features"]] == [
            "local_voice", "local_models", "cloud_providers",
            "semantic_embeddings", "browser_automation",
            "desktop_control"]
        for feature in report["features"]:
            assert feature["status"] in (
                "available", "degraded", "unavailable", "unknown")
            assert feature["reason"]


class TestMatrix:
    def test_low_ram_light_cpu_machine_degrades_local_voice(self):
        machine = _machine(
            cpu={"logical_cores": 2, "model": "Weak CPU"},
            ram={"total_bytes": 4 * _GIB})
        verdict = _verdict(machine, "local_voice")
        assert verdict["status"] == "degraded"
        assert "2 logical CPU cores" in verdict["reason"]
        assert "4 GB of RAM" in verdict["reason"]

    def test_offline_network_kills_cloud_only(self):
        machine = _machine(network={"state": "offline", "reachable": []})
        assert _verdict(machine, "cloud_providers")["status"] == "unavailable"
        # Local features do not read the network verdict.
        assert _verdict(machine, "local_models")["status"] == "available"
        assert _verdict(machine, "semantic_embeddings")["status"] == "available"
        assert _verdict(machine, "local_voice")["status"] == "degraded"

    def test_all_unknown_machine_yields_all_unknown_verdicts(self):
        machine = {
            "platform": {"system": None, "release": None,
                         "architecture": None, "python": None},
            "cpu": {"logical_cores": None, "model": None},
            "ram": {"total_bytes": None},
            "disk": {"path": None, "total_bytes": None, "free_bytes": None},
            "gpu": None,
            "microphone": {"state": "unknown", "detail": "probe failed"},
            "network": {"state": "unknown", "reachable": []},
            "ollama_reachable": None,
            "voice_dependencies": {
                "faster_whisper": None, "kokoro": None,
                "onnxruntime": None, "numpy": None, "aiortc": None},
            "browser": {"playwright_installed": None,
                        "chromium_found": False},
        }
        features = mc.capability_matrix(machine)  # must not raise
        assert len(features) == 6
        assert all(f["status"] == "unknown" for f in features)
        assert all(f["reason"] for f in features)

    def test_missing_voice_dependencies_is_unavailable_and_named(self):
        machine = _machine(voice_dependencies={
            "faster_whisper": False, "kokoro": True,
            "onnxruntime": True, "numpy": True, "aiortc": False})
        verdict = _verdict(machine, "local_voice")
        assert verdict["status"] == "unavailable"
        assert "faster_whisper" in verdict["reason"]
        assert "aiortc" in verdict["reason"]
        assert "uv sync --extra voice" in verdict["reason"]

    def test_gpu_machine_has_available_voice_and_models(self):
        machine = _machine(gpu={"name": "NVIDIA GeForce RTX 4070",
                                "vendor": "nvidia",
                                "memory_bytes": 12 * _GIB})
        assert _verdict(machine, "local_voice")["status"] == "available"
        assert _verdict(machine, "local_models")["status"] == "available"

    def test_cpu_only_strong_machine_still_degrades_voice(self):
        # The measured VM reality: CPU-only STT at 'small' was
        # ~28x over budget there, so CPU-only is never waved
        # through as "available" unmeasured.
        verdict = _verdict(_machine(), "local_voice")
        assert verdict["status"] == "degraded"
        assert "5.6 s" in verdict["reason"]

    def test_ram_below_voice_floor_is_unavailable(self):
        machine = _machine(ram={"total_bytes": 1 * _GIB})
        assert _verdict(machine, "local_voice")["status"] == "unavailable"

    def test_absent_microphone_degrades_voice(self):
        machine = _machine(
            gpu={"name": "NVIDIA", "vendor": "nvidia", "memory_bytes": None},
            microphone={"state": "absent", "detail": "none found"})
        verdict = _verdict(machine, "local_voice")
        assert verdict["status"] == "degraded"
        assert "microphone" in verdict["reason"]

    def test_ollama_down_degrades_models_and_embeddings_only(self):
        machine = _machine(ollama_reachable=False)
        assert _verdict(machine, "local_models")["status"] == "unavailable"
        embeddings = _verdict(machine, "semantic_embeddings")
        assert embeddings["status"] == "unavailable"
        assert "keyword recall still works" in embeddings["reason"]
        assert _verdict(machine, "cloud_providers")["status"] == "available"

    def test_mid_ram_ollama_machine_degrades_local_models(self):
        machine = _machine(ram={"total_bytes": 8 * _GIB})
        verdict = _verdict(machine, "local_models")
        assert verdict["status"] == "degraded"
        assert "8 GB of RAM" in verdict["reason"]

    def test_online_network_reason_names_a_reached_endpoint(self):
        verdict = _verdict(_machine(), "cloud_providers")
        assert verdict["status"] == "available"
        assert "api.openai.com" in verdict["reason"]

    def test_playwright_without_chromium_is_degraded(self):
        machine = _machine(browser={"playwright_installed": True,
                                    "chromium_found": False})
        verdict = _verdict(machine, "browser_automation")
        assert verdict["status"] == "degraded"
        assert "playwright install chromium" in verdict["reason"]

    def test_playwright_absent_is_unavailable(self):
        machine = _machine(browser={"playwright_installed": False,
                                    "chromium_found": False})
        assert _verdict(machine, "browser_automation")["status"] == "unavailable"

    def test_desktop_control_is_a_platform_verdict(self):
        # S19: the verdict goes live for OBSERVE — on Windows it
        # now turns on the probed UIA package (available when
        # present, degraded naming the missing package when
        # absent, unknown when unprobed); the non-Windows and
        # unidentified-platform verdicts are unchanged from S10.
        # S20: actuation is built, so the available reason now
        # says Aether CAN click and type (behind the Computer
        # Use switches and the confirmation policy) where S19's
        # wording said it could not — the assertion below pins
        # the new sentence, not the old one.
        windows = _machine(platform={"system": "Windows", "release": "11",
                                     "architecture": "AMD64",
                                     "python": "3.12.3"})
        verdict = _verdict(windows, "desktop_control")
        assert verdict["status"] == "unknown"
        ready = _verdict(
            _machine(platform={"system": "Windows", "release": "11",
                               "architecture": "AMD64",
                               "python": "3.12.3"},
                     desktop={"uia_package": True}),
            "desktop_control")
        assert ready["status"] == "available"
        assert "click and type" in ready["reason"]
        assert "always confirm" in ready["reason"]
        missing = _verdict(
            _machine(platform={"system": "Windows", "release": "11",
                               "architecture": "AMD64",
                               "python": "3.12.3"},
                     desktop={"uia_package": False}),
            "desktop_control")
        assert missing["status"] == "degraded"
        assert "uiautomation" in missing["reason"]
        linux = _verdict(_machine(), "desktop_control")
        assert linux["status"] == "unavailable"
        assert "Windows UI Automation" in linux["reason"]


@pytest.fixture
def _report_env(tmp_path, monkeypatch):
    """Isolate the module cache + the persisted copy per test,
    and count real probes."""
    monkeypatch.setattr(mc, "_REPORT_PATH", tmp_path / "machine_capability.json")
    monkeypatch.setattr(mc, "_cached_report", None)
    probes = {"count": 0}
    real_build = mc.build_report

    def counting_build():
        probes["count"] += 1
        return real_build()

    monkeypatch.setattr(mc, "build_report", counting_build)
    return probes


class TestReportCache:
    def test_second_call_within_ttl_is_cached(self, _report_env):
        first = mc.machine_capability_report()
        assert first["cached"] is False
        second = mc.machine_capability_report()
        assert second["cached"] is True
        assert second["probed_at"] == first["probed_at"]
        assert _report_env["count"] == 1

    def test_force_reprobes(self, _report_env):
        mc.machine_capability_report()
        forced = mc.machine_capability_report(force=True)
        assert forced["cached"] is False
        assert _report_env["count"] == 2

    def test_stale_cache_reprobes(self, _report_env, monkeypatch):
        mc.machine_capability_report()
        stale = dict(mc._cached_report)
        stale["probed_at"] = time.time() - mc.REPORT_TTL_SECONDS - 1
        monkeypatch.setattr(mc, "_cached_report", stale)
        refreshed = mc.machine_capability_report()
        assert refreshed["cached"] is False
        assert _report_env["count"] == 2

    def test_fresh_probe_persists_and_survives_restart(
            self, _report_env, monkeypatch):
        first = mc.machine_capability_report()
        on_disk = json.loads(
            (mc._REPORT_PATH).read_text(encoding="utf-8"))
        assert on_disk["probed_at"] == first["probed_at"]
        # Simulate a restart: no in-memory cache, and a probe
        # that would explode if the persisted copy were not
        # served instead.
        monkeypatch.setattr(mc, "_cached_report", None)

        def _boom():
            raise AssertionError("re-probed despite a fresh persisted copy")

        monkeypatch.setattr(mc, "build_report", _boom)
        served = mc.machine_capability_report()
        assert served["cached"] is True
        assert served["probed_at"] == first["probed_at"]

    def test_stale_persisted_copy_is_not_served(
            self, _report_env, monkeypatch):
        mc.machine_capability_report()
        stale = json.loads(mc._REPORT_PATH.read_text(encoding="utf-8"))
        stale["probed_at"] = time.time() - mc.REPORT_TTL_SECONDS - 1
        mc._REPORT_PATH.write_text(json.dumps(stale), encoding="utf-8")
        monkeypatch.setattr(mc, "_cached_report", None)
        refreshed = mc.machine_capability_report()
        assert refreshed["cached"] is False
        assert _report_env["count"] == 2


class _FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


class TestHandler:
    def test_handler_answers_the_full_report(self, monkeypatch):
        fixture = {
            "probed_at": 1700000000.0,
            "machine": _machine(),
            "features": mc.capability_matrix(_machine()),
            "cached": False,
        }
        calls = []
        monkeypatch.setattr(
            mc, "machine_capability_report",
            lambda force=False: calls.append(force) or fixture)
        socket = _FakeSocket()
        conn = SimpleNamespace(ws=socket)
        msg = Event(type=EventType.MACHINE_CAPABILITY_REQUEST,
                    request_id="r1", payload={})
        asyncio.run(websocket._handle_machine_capability_request(conn, msg))
        assert len(socket.sent) == 1
        reply = socket.sent[0]
        assert reply["type"] == "MACHINE_CAPABILITY_RESPONSE"
        assert reply["request_id"] == "r1"
        assert reply["payload"]["probed_at"] == 1700000000.0
        assert [f["id"] for f in reply["payload"]["features"]] == [
            "local_voice", "local_models", "cloud_providers",
            "semantic_embeddings", "browser_automation",
            "desktop_control"]
        assert calls == [False]

    def test_handler_passes_force_through(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            mc, "machine_capability_report",
            lambda force=False: calls.append(force) or {
                "probed_at": 1.0, "machine": {}, "features": [],
                "cached": False})
        socket = _FakeSocket()
        conn = SimpleNamespace(ws=socket)
        msg = Event(type=EventType.MACHINE_CAPABILITY_REQUEST,
                    request_id="r2", payload={"force": True})
        asyncio.run(websocket._handle_machine_capability_request(conn, msg))
        assert calls == [True]

    def test_request_type_is_dispatched(self):
        assert websocket._DISPATCH[EventType.MACHINE_CAPABILITY_REQUEST] is \
            websocket._handle_machine_capability_request
