"""Wave 8 tier policy — the executor call sites for the router
manager's hooks (the integration step Full Furnish's report §7
item 4 assigned to the routing programme).

``LlamaRouterManager.note_model_used`` / ``note_job_completed``
landed with Wave 8's tier policy but had no call sites: the
manager's bookkeeping is per-instance in-memory, and managers
were constructed on demand, so a note taken on one instance was
invisible to every other. This slice:

  * gives the run path ONE shared manager
    (``EngineState.get_llama_router_manager``), keyed on the
    llamacpp config fields it is built from and rebuilt when
    they change;
  * extends the E5-F07 serving note to carry the serving MODEL
    (the provider layer's firing points always knew it);
  * wires the task pump (``execute_task_generator`` — the one
    seam fresh runs AND resume runs both stream through) to
    note use when the serving evidence names the local router,
    and to fire job-completed, detached, on terminal events.

Everything here is scripted: generators are hand-fed event
streams that write serving notes exactly where LiteLLMProvider
writes them, and the one real-manager test runs the router
behind httpx MockTransport (the Wave 8 FakeRouter pattern). No
network, no llama-server.
"""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from aether_common.contracts import Event, EventType
from aether_engine.models import llama_router
from aether_engine.models.llama_router import LlamaRouterManager
from aether_engine.providers.serving import (
    begin_serving_tracking,
    end_serving_tracking,
    note_serving_provider,
    serving_model,
    serving_provider,
)
from aether_engine.routes import ws_task_exec
from aether_engine.state import engine_state

# Router-style model ids (what the router's /models reports and
# the registry carries): the preset patterns fnmatch these —
# *qwen2.5-32b* is TIER_HEAVY, *qwen2.5-7b* is TIER_DAILY.
HEAVY_MODEL = "qwen2.5-32b-instruct"
DAILY_MODEL = "qwen2.5-7b-instruct"


# ------------------------------------------------------------------
# Harness (the wave-6 fake-conn pattern, extended with the
# background-task collection the pump's spawn_background needs)
# ------------------------------------------------------------------

class _FakeWs:
    def __init__(self):
        self.frames = []

    async def send_text(self, raw):
        self.frames.append(json.loads(raw))


class _FakeAuditLogger:
    def log_task_transition(self, *args, **kwargs):
        pass

    def log_event(self, *args, **kwargs):
        pass


def _fake_conn():
    conn = SimpleNamespace(
        ws=_FakeWs(),
        secret_store=object(),
        audit_logger=_FakeAuditLogger(),
        active_task_id=None,
        background=[],
    )

    def _spawn(coro):
        task = asyncio.create_task(coro)
        conn.background.append(task)
        return task

    conn.spawn_background = _spawn
    return conn


async def _drain_background(conn):
    if conn.background:
        await asyncio.gather(*conn.background, return_exceptions=True)


def _ev(type_, **payload):
    return Event(type=type_, request_id="req-1", payload=payload)


async def _scripted_task(notes, terminal):
    """A task generator that behaves like a served run: it writes
    each (provider, model) serving note where the provider layer
    would — after the call that produced output, before the next
    event — then emits the terminal event."""
    yield _ev(EventType.TASK_CREATED, task_id="task-1",
              path="langgraph", prompt="do the thing")
    for provider, model in notes:
        note_serving_provider(provider, model)
        yield _ev(EventType.TOOL_ACTIVITY, task_id="task-1")
    yield terminal


class RecordingManager:
    """The LlamaRouterManager surface the hooks call, recording."""

    def __init__(self, *, raises=False):
        self.used: list[str] = []
        self.completed: list[str] = []
        self._raises = raises

    def note_model_used(self, model_id):
        if self._raises:
            raise RuntimeError("manager exploded")
        self.used.append(model_id)

    async def note_job_completed(self, model_id):
        if self._raises:
            raise RuntimeError("manager exploded")
        self.completed.append(model_id)
        return {"success": True, "error": None}


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    """Point the global audit logger at tmp and install a
    recording manager behind the shared accessor; yields
    (manager, accessor_calls)."""
    monkeypatch.setattr(
        engine_state.audit_logger, "log_path", tmp_path / "audit.log")
    manager = RecordingManager()
    accessor_calls: list = []

    def _accessor(secret_store=None):
        accessor_calls.append(secret_store)
        return manager

    monkeypatch.setattr(
        engine_state, "get_llama_router_manager", _accessor)
    return manager, accessor_calls


def _completed_event():
    return _ev(EventType.TASK_COMPLETED, task_id="task-1",
               latency_ms=12.0)


# ------------------------------------------------------------------
# The serving note carries the model
# ------------------------------------------------------------------

def test_serving_note_carries_the_model():
    token = begin_serving_tracking()
    try:
        note_serving_provider("llamacpp", HEAVY_MODEL)
        assert serving_provider() == "llamacpp"
        assert serving_model() == HEAVY_MODEL
        # A provider-only note (the pre-extension shape) keeps
        # the model from the previous note — last note wins per
        # field, exactly like the provider field behaves.
        note_serving_provider("llamacpp")
        assert serving_model() == HEAVY_MODEL
    finally:
        end_serving_tracking(token)


# ------------------------------------------------------------------
# Pump hooks
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llamacpp_task_fires_used_then_completed(hook_env):
    manager, _calls = hook_env
    conn = _fake_conn()
    gen = _scripted_task(
        [("llamacpp", HEAVY_MODEL)], _completed_event())
    await ws_task_exec.execute_task_generator(
        conn, gen, "llamacpp", HEAVY_MODEL, "req-1")
    await _drain_background(conn)
    assert manager.used == [HEAVY_MODEL]
    assert manager.completed == [HEAVY_MODEL]
    assert conn.ws.frames[-1]["type"] == "TASK_COMPLETED"


@pytest.mark.asyncio
async def test_llamacpp_failed_task_still_completes_the_job(hook_env):
    manager, _calls = hook_env
    conn = _fake_conn()
    gen = _scripted_task(
        [("llamacpp", DAILY_MODEL)],
        _ev(EventType.TASK_FAILED, task_id="task-1",
            error="router died", retriable=False))
    await ws_task_exec.execute_task_generator(
        conn, gen, "llamacpp", DAILY_MODEL, "req-1")
    await _drain_background(conn)
    assert manager.used == [DAILY_MODEL]
    assert manager.completed == [DAILY_MODEL]


@pytest.mark.asyncio
async def test_interrupted_task_does_not_complete_the_job(hook_env):
    """TASK_INTERRUPTED is a pause (pending approval), not an
    ending: the job is still in flight and must not be reported
    completed — it completes on the resume pump's terminal
    event instead."""
    manager, _calls = hook_env
    conn = _fake_conn()
    gen = _scripted_task(
        [("llamacpp", HEAVY_MODEL)],
        _ev(EventType.TASK_INTERRUPTED, task_id="task-1"))
    await ws_task_exec.execute_task_generator(
        conn, gen, "llamacpp", HEAVY_MODEL, "req-1")
    await _drain_background(conn)
    assert manager.used == [HEAVY_MODEL]
    assert manager.completed == []


@pytest.mark.asyncio
async def test_cloud_task_never_touches_the_manager(hook_env):
    manager, accessor_calls = hook_env
    conn = _fake_conn()
    gen = _scripted_task(
        [("google_gemini", "gemini-main")], _completed_event())
    await ws_task_exec.execute_task_generator(
        conn, gen, "google_gemini", "gemini-main", "req-1")
    await _drain_background(conn)
    assert manager.used == []
    assert manager.completed == []
    # Not even resolved: the provider check gates the accessor.
    assert accessor_calls == []


@pytest.mark.asyncio
async def test_raising_manager_does_not_affect_the_task(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        engine_state.audit_logger, "log_path", tmp_path / "audit.log")
    manager = RecordingManager(raises=True)
    monkeypatch.setattr(
        engine_state, "get_llama_router_manager",
        lambda secret_store=None: manager)
    conn = _fake_conn()
    gen = _scripted_task(
        [("llamacpp", HEAVY_MODEL)], _completed_event())
    # Neither hook may surface: the pump returns normally and
    # the client still receives the completion.
    await ws_task_exec.execute_task_generator(
        conn, gen, "llamacpp", HEAVY_MODEL, "req-1")
    await _drain_background(conn)
    assert conn.ws.frames[-1]["type"] == "TASK_COMPLETED"


# ------------------------------------------------------------------
# The real manager: a heavy completion reaches the router unload
# ------------------------------------------------------------------

class FakeRouter:
    """The Wave 8 scripted router (httpx MockTransport)."""

    def __init__(self):
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = None
        self.requests.append({
            "method": request.method,
            "path": request.url.path,
            "json": body,
        })
        if request.url.path == "/models/unload":
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
    return router


@pytest.mark.asyncio
async def test_heavy_completion_reaches_router_unload(
        hook_env, fake_router, monkeypatch):
    real_manager = LlamaRouterManager(
        router_url="http://127.0.0.1:9931")
    monkeypatch.setattr(
        engine_state, "get_llama_router_manager",
        lambda secret_store=None: real_manager)
    conn = _fake_conn()
    gen = _scripted_task(
        [("llamacpp", HEAVY_MODEL)], _completed_event())
    await ws_task_exec.execute_task_generator(
        conn, gen, "llamacpp", HEAVY_MODEL, "req-1")
    await _drain_background(conn)
    unloads = fake_router.posts("/models/unload")
    assert len(unloads) == 1
    assert unloads[0]["json"] == {"model": HEAVY_MODEL}
    # The manager marked the heavy model mid-job on use.
    # (unload pops the bookkeeping, so the request is the proof.)


@pytest.mark.asyncio
async def test_daily_completion_does_not_unload(
        hook_env, fake_router, monkeypatch):
    real_manager = LlamaRouterManager(
        router_url="http://127.0.0.1:9931")
    monkeypatch.setattr(
        engine_state, "get_llama_router_manager",
        lambda secret_store=None: real_manager)
    conn = _fake_conn()
    gen = _scripted_task(
        [("llamacpp", DAILY_MODEL)], _completed_event())
    await ws_task_exec.execute_task_generator(
        conn, gen, "llamacpp", DAILY_MODEL, "req-1")
    await _drain_background(conn)
    assert fake_router.posts("/models/unload") == []
    # …but the use WAS recorded on the shared instance.
    assert DAILY_MODEL in real_manager._last_used


# ------------------------------------------------------------------
# The shared manager: identity + config-keyed rebuild
# ------------------------------------------------------------------

def test_shared_manager_identity_and_rebuild(monkeypatch):
    cfg = engine_state.user_config
    monkeypatch.setattr(engine_state, "llama_router_manager", None)
    monkeypatch.setattr(engine_state, "_llama_router_manager_key", None)
    monkeypatch.setattr(engine_state, "_llama_router_secret_store", None)
    monkeypatch.setattr(cfg, "llamacpp_binary_path", "/opt/llama/llama-server")
    monkeypatch.setattr(cfg, "llamacpp_models_dir", "/models/a")

    first = engine_state.get_llama_router_manager()
    assert isinstance(first, LlamaRouterManager)
    assert first.binary_path == "/opt/llama/llama-server"
    # Same config → the same instance (bookkeeping survives).
    assert engine_state.get_llama_router_manager() is first

    # A llamacpp-relevant change rebuilds on next access.
    monkeypatch.setattr(cfg, "llamacpp_models_dir", "/models/b")
    rebuilt = engine_state.get_llama_router_manager()
    assert rebuilt is not first
    assert rebuilt.models_dir == "/models/b"
    assert engine_state.get_llama_router_manager() is rebuilt
