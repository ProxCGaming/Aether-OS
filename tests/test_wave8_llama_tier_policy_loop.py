"""Wave 8 tier policy — the background policy loop.

Wave 8's tier policy ran exactly one pass per router start
(``LlamaRouterManager.start_router`` calls ``apply_tier_policy``
after a successful spawn) and never again: mid-session, the
daily tier's idle unload (DAILY_MODEL_IDLE_TTL_S = 600s) had no
one left to fire it. The executor call-sites slice wired the
per-job hooks (note_model_used / note_job_completed — heavy
models unload at job completion through those), and flagged
this loop as the remaining piece.

The loop lives in the app lifespan beside the engine's other
background tasks (module handle, start at lifespan entry, cancel
+ await at exit — the discovery/backfill discipline). It runs
passes on the ONE shared manager
(``EngineState.get_llama_router_manager``), because the tier
bookkeeping is per-instance and the executor hooks write to that
same instance. Passes are gated on the live
``llamacpp_enabled`` flag, read fresh every pass, so the feature
toggles without an engine restart.

Everything here is scripted: a stub manager behind the shared
accessor, an httpx MockTransport router for the one real-manager
test (the Wave 8 FakeRouter pattern), and injectable
millisecond intervals — no network, no llama-server, no real
sleeping.
"""
import asyncio
import importlib
import logging
import time

import httpx
import pytest

# importlib, not `from aether_engine import app`: the package
# __init__ re-exports the FastAPI instance under the name `app`,
# shadowing the module this slice's loop lives in.
engine_app = importlib.import_module("aether_engine.app")
from aether_engine.models import llama_router
from aether_engine.models.llama_presets import DAILY_MODEL_IDLE_TTL_S
from aether_engine.models.llama_router import LlamaRouterManager
from aether_engine.state import engine_state

GIB = 1024 ** 3
DAILY_MODEL = "local/qwen2.5-7b-q4.gguf"
TINY_INTERVAL_S = 0.01


# ------------------------------------------------------------------
# Harness
# ------------------------------------------------------------------


class StubManager:
    """A scriptable stand-in for the shared router manager."""

    def __init__(self):
        self.calls = 0
        self.errors: dict = {}
        self.raise_times = 0
        self.block: asyncio.Event | None = None
        self.in_flight = 0
        self.max_in_flight = 0

    async def apply_tier_policy(self, **kwargs):
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.raise_times > 0:
                self.raise_times -= 1
                raise RuntimeError("pass exploded")
            if self.block is not None:
                await self.block.wait()
            return {"load": [], "unload": [],
                    "errors": dict(self.errors)}
        finally:
            self.in_flight -= 1


@pytest.fixture
def stub_env(monkeypatch):
    """Install a StubManager behind the shared accessor and
    guarantee the loop is stopped + the handle cleared after
    the test, whatever it did. Yields (manager, accessor_calls).
    """
    manager = StubManager()
    accessor_calls: list = []

    def _accessor(secret_store=None):
        accessor_calls.append(secret_store)
        return manager

    monkeypatch.setattr(
        engine_state, "get_llama_router_manager", _accessor)
    yield manager, accessor_calls
    # Teardown is synchronous: cancel any leaked loop task
    # directly (tests that exercise start/stop assert the
    # graceful path themselves before this runs).
    task = engine_app._tier_policy_task
    if task is not None:
        task.cancel()
        engine_app._tier_policy_task = None


def _set_enabled(monkeypatch, enabled: bool):
    monkeypatch.setattr(
        engine_state.user_config, "llamacpp_enabled", enabled)


async def _wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met within timeout")


# ------------------------------------------------------------------
# The interval is derived from the policy's own clock
# ------------------------------------------------------------------


def test_interval_derives_from_daily_idle_ttl():
    assert engine_app.TIER_POLICY_INTERVAL_S == DAILY_MODEL_IDLE_TTL_S / 10
    assert 30.0 <= engine_app.TIER_POLICY_INTERVAL_S <= 120.0


# ------------------------------------------------------------------
# Passes run on the shared manager, only while enabled
# ------------------------------------------------------------------


async def test_loop_runs_passes_on_shared_manager_when_enabled(
        stub_env, monkeypatch):
    manager, accessor_calls = stub_env
    _set_enabled(monkeypatch, True)
    task = engine_app._start_tier_policy_loop(TINY_INTERVAL_S)
    await _wait_until(lambda: manager.calls >= 3)
    await engine_app._stop_tier_policy_loop()
    assert task.done()
    # Every pass resolved the manager through the shared
    # accessor — the instance the executor hooks write to.
    assert len(accessor_calls) >= 3


async def test_loop_does_nothing_when_disabled(stub_env, monkeypatch):
    manager, accessor_calls = stub_env
    _set_enabled(monkeypatch, False)
    engine_app._start_tier_policy_loop(TINY_INTERVAL_S)
    await asyncio.sleep(0.1)
    await engine_app._stop_tier_policy_loop()
    assert manager.calls == 0
    # While disabled the manager is never even resolved.
    assert accessor_calls == []


async def test_live_toggle_starts_and_stops_passes(
        stub_env, monkeypatch):
    manager, _ = stub_env
    _set_enabled(monkeypatch, False)
    engine_app._start_tier_policy_loop(TINY_INTERVAL_S)
    await asyncio.sleep(0.05)
    assert manager.calls == 0
    # Toggling on mid-run: passes begin without a restart.
    _set_enabled(monkeypatch, True)
    await _wait_until(lambda: manager.calls >= 2)
    # Toggling off: passes stop, again without a restart.
    _set_enabled(monkeypatch, False)
    await asyncio.sleep(0.05)  # let any in-flight pass land
    frozen = manager.calls
    await asyncio.sleep(0.1)
    assert manager.calls == frozen
    await engine_app._stop_tier_policy_loop()


# ------------------------------------------------------------------
# Loop resilience + lifecycle discipline
# ------------------------------------------------------------------


async def test_failing_pass_does_not_kill_loop(stub_env, monkeypatch):
    manager, _ = stub_env
    manager.raise_times = 2
    _set_enabled(monkeypatch, True)
    task = engine_app._start_tier_policy_loop(TINY_INTERVAL_S)
    await _wait_until(lambda: manager.calls >= 4)
    assert not task.done()
    await engine_app._stop_tier_policy_loop()


async def test_passes_never_overlap(stub_env, monkeypatch):
    manager, _ = stub_env
    manager.block = asyncio.Event()  # passes park until released
    _set_enabled(monkeypatch, True)
    engine_app._start_tier_policy_loop(TINY_INTERVAL_S)
    await _wait_until(lambda: manager.calls >= 1)
    await asyncio.sleep(0.1)  # many ticks elapse mid-pass
    # The running pass is still the only one: ticks that came
    # due during it were skipped, never queued behind it.
    assert manager.calls == 1
    assert manager.max_in_flight == 1
    manager.block.set()
    await engine_app._stop_tier_policy_loop()


async def test_double_start_is_a_noop(stub_env, monkeypatch):
    _set_enabled(monkeypatch, True)
    first = engine_app._start_tier_policy_loop(TINY_INTERVAL_S)
    second = engine_app._start_tier_policy_loop(TINY_INTERVAL_S)
    assert second is first
    await engine_app._stop_tier_policy_loop()


async def test_stop_actually_stops(stub_env, monkeypatch):
    manager, _ = stub_env
    _set_enabled(monkeypatch, True)
    task = engine_app._start_tier_policy_loop(TINY_INTERVAL_S)
    await _wait_until(lambda: manager.calls >= 2)
    await engine_app._stop_tier_policy_loop()
    assert engine_app._tier_policy_task is None
    assert task.done()
    frozen = manager.calls
    await asyncio.sleep(0.1)
    assert manager.calls == frozen


# ------------------------------------------------------------------
# Pass-level reporting
# ------------------------------------------------------------------


async def test_per_model_errors_logged_once_per_pass(
        stub_env, monkeypatch, caplog):
    manager, _ = stub_env
    manager.errors = {DAILY_MODEL: "unload refused"}
    _set_enabled(monkeypatch, True)
    with caplog.at_level(logging.WARNING, logger="aether_engine.app"):
        await engine_app._run_tier_policy_pass()
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert DAILY_MODEL in warnings[0].getMessage()


async def test_pass_against_down_router_is_quiet(monkeypatch, caplog):
    # A real manager pointed at a port nothing answers: the
    # pass is a no-op and logs no warning — an absent router is
    # a state, not a failure.
    real_manager = LlamaRouterManager(
        router_url="http://127.0.0.1:9")
    monkeypatch.setattr(
        engine_state, "get_llama_router_manager",
        lambda secret_store=None: real_manager)
    _set_enabled(monkeypatch, True)
    with caplog.at_level(logging.WARNING, logger="aether_engine.app"):
        await engine_app._run_tier_policy_pass()
    assert caplog.records == []


# ------------------------------------------------------------------
# The real policy, end to end through one pass
# ------------------------------------------------------------------


class FakeRouter:
    """A scripted llama.cpp router behind httpx.MockTransport
    (the Wave 8 router-test pattern)."""

    def __init__(self, entries=None):
        self.entries = entries if entries is not None else []
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json as _json

        body = None
        if request.content:
            try:
                body = _json.loads(request.content)
            except ValueError:
                body = None
        self.requests.append({
            "method": request.method,
            "path": request.url.path,
            "json": body,
        })
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/props":
            return httpx.Response(200, json={"version": "test-build-1"})
        if path == "/models":
            return httpx.Response(200, json={"data": self.entries})
        if path in ("/models/load", "/models/unload"):
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404, json={})

    def posts(self, path: str) -> list[dict]:
        return [r for r in self.requests
                if r["method"] == "POST" and r["path"] == path]


@pytest.fixture
def fake_router(monkeypatch):
    router = FakeRouter()
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(router.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llama_router.httpx, "AsyncClient", factory)
    # Deterministic fit verdicts: a 16 GiB RAM machine, no GPU.
    monkeypatch.setattr(
        llama_router, "machine_capability_report",
        lambda: {"machine": {"ram": {"total_bytes": 16 * GIB},
                             "gpu": {}}})
    return router


async def test_pass_unloads_stale_daily_through_real_manager(
        fake_router, monkeypatch):
    fake_router.entries = [
        {"id": DAILY_MODEL, "status": {"value": "loaded"}}]
    real_manager = LlamaRouterManager(
        router_url="http://127.0.0.1:9931")
    real_manager._last_used[DAILY_MODEL] = (
        time.monotonic() - DAILY_MODEL_IDLE_TTL_S - 60)
    monkeypatch.setattr(
        engine_state, "get_llama_router_manager",
        lambda secret_store=None: real_manager)
    _set_enabled(monkeypatch, True)
    await engine_app._run_tier_policy_pass()
    unloads = fake_router.posts("/models/unload")
    assert len(unloads) == 1
    assert unloads[0]["json"] == {"model": DAILY_MODEL}
