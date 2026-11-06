"""Wave 2 (Builder J) — E1 routes remainder + lane N-05/N-19/N-43/N-44
(websocket.py half).

Failing-first tests for:
- E1-02: attachment filename used unsanitised in a filesystem path.
- E1-04: artifact GET does synchronous file IO on the event loop.
- E1-05: MEMORY_FACT_UPDATE_REQUEST has no exception guard.
- E1-06: pending_plugin_installs never expires, is not cleaned on
  disconnect, and an unknown-id grant gets no reply.
- E1-08: PLUGIN_INSTALL_REQUEST runs prepare_install on the loop.
- E1-09: approval resume cancels the live task_runner but never
  awaits it before starting the resume pump.
- E1-10: save_config failures in inline handlers kill the session.
- E1-11: memory read handlers run sync backend calls on the loop.
- E1-12: TOOL_POLICY_SET_REQUEST reports success:true when nothing
  was saved.
- E1-15: artifact GET maps .bmp/.ico to application/octet-stream.
- N-05: no WebSocket send lock (per-connection send serialisation).
- N-19: disconnect cleanup misses per-connection background work
  (observable slice: this connection's pending plugin installs).
- N-43: per-connection handlers mutate module-global seams, never
  restored (observable slice: search/browser-policy loaders).
- N-44: CANCEL_TASK replies a hard-coded task_id; /health reports
  "ok" unconditionally.
"""

from __future__ import annotations

import asyncio
import base64
import json
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.memory import session_store
from aether_engine.routes import websocket as ws_module

# ---------------------------------------------------------------------------
# Harness (house pattern: tmp stores, recorded save_config, fresh token)
# ---------------------------------------------------------------------------


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    saves: list = []
    monkeypatch.setattr(ws_module, "save_config", lambda cfg: saves.append(cfg))
    engine_state.initialize()
    return SimpleNamespace(tmp_path=tmp_path, saves=saves)


def _connect(client):
    # TestClient startup runs the lifespan, which regenerates the
    # auth token — read it fresh, as the phase-10.1 suite does.
    return client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"})


# The media analyzer fans its progress events out to EVERY live
# connection (media/analyzer.py subscribers). The E1-02 test below
# registers a real attachment, whose analysis can still be running
# when a later test in this file connects — those unsolicited
# frames are not replies and are skipped by the receiver.
_UNSOLICITED = {EventType.MEDIA_ANALYSIS_PROGRESS, EventType.MEDIA_ANALYZED}


def _recv_event(ws, timeout=10.0):
    """Receive one Event, failing (never hanging) if the server goes
    quiet — copied from test_phase10_1_plugin_install.py. Unsolicited
    media-analysis broadcasts are skipped (see _UNSOLICITED)."""
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise AssertionError(
                f"No WebSocket message within {timeout}s — the session "
                "was probably dropped or the reply never sent")
        q: queue.Queue = queue.Queue()

        def _do(q_=q):
            try:
                q_.put(("ok", ws.receive_text()))
            except Exception as e:  # noqa: BLE001 — relay to the test thread
                q_.put(("err", e))

        threading.Thread(target=_do, daemon=True).start()
        try:
            kind, value = q.get(timeout=remaining)
        except queue.Empty:
            raise AssertionError(
                f"No WebSocket message within {timeout}s — the session "
                "was probably dropped or the reply never sent")
        if kind == "err":
            raise value
        ev = Event.from_json(value)
        if ev.type in _UNSOLICITED:
            continue
        return ev


def _hello(ws):
    ev = _recv_event(ws)
    assert ev.type == EventType.HELLO
    return ev


def _send(ws, type_, payload=None, req_id="r1"):
    ws.send_text(Event(type=type_, request_id=req_id,
                       payload=payload or {}).to_json())


def _plugin_source(tmp_path: Path) -> Path:
    src = tmp_path / "source_plugin"
    src.mkdir(exist_ok=True)
    (src / "manifest.json").write_text(json.dumps(
        {"name": "Wave2TestPlugin", "version": "1.0"}))
    (src / "README.md").write_text("test plugin")
    return src


@pytest.fixture
def plugin_env(ws_env, monkeypatch):
    from aether_engine.plugins.installer import PluginInstaller

    orig = engine_state.plugin_installer
    engine_state.plugin_installer = PluginInstaller(
        ws_env.tmp_path / "plugins")
    ws_module.pending_plugin_installs.clear()
    yield ws_env
    engine_state.plugin_installer = orig
    ws_module.pending_plugin_installs.clear()


# ---------------------------------------------------------------------------
# E1-02 — attachment filename sanitisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attachment_name_with_separators_is_sanitised(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Hermetic media store + analysis scheduling (the pf2 house
    # pattern): this test pins the attachment WRITE path, and the
    # session-global store/analyzer are shared with every other
    # suite in a full run.
    from aether_engine.media.store import MediaStore

    media_store = MediaStore(db_path=tmp_path / "media.db")
    media_store.init_db()
    monkeypatch.setattr(
        "aether_engine.media.store.GLOBAL_MEDIA_STORE", media_store)
    monkeypatch.setattr(
        "aether_engine.media.analyzer.schedule_media_analysis",
        lambda media_id: None)
    data_url = "data:image/png;base64," + base64.b64encode(b"\x89PNG").decode()
    await ws_module._register_message_attachments(
        "sess-1", "msg-1",
        [{"name": "sub/dir/evil.png", "data": data_url},
         {"name": "..\\..\\win.png", "data": data_url}],
        None)
    attach_dir = tmp_path / ".aether" / "attachments" / "sess-1"
    assert len(list(attach_dir.glob("*_evil.png"))) == 1, (
        "a filename containing '/' must be reduced to its basename "
        "and written inside the session attachments dir")
    assert len(list(attach_dir.glob("*_win.png"))) == 1, (
        "a filename containing Windows separators must be sanitised "
        "the same way")
    assert not (attach_dir / "sub").exists()


# ---------------------------------------------------------------------------
# E1-04 / E1-15 — artifact GET: off-loop reads + image MIME types
# ---------------------------------------------------------------------------


def _seed_artifact(tmp_path, monkeypatch, name: str, data: bytes):
    from aether_engine.artifacts.store import ArtifactStore

    test_store = ArtifactStore(db_path=tmp_path / "artifacts.db")
    monkeypatch.setattr(
        "aether_engine.artifacts.store.GLOBAL_ARTIFACT_STORE", test_store)
    f = tmp_path / name
    f.write_bytes(data)
    return test_store.record_artifact("sess_1", "task_1", "file", name, str(f))


@pytest.mark.asyncio
async def test_artifact_get_reads_file_off_the_event_loop(
        tmp_path, monkeypatch):
    art = _seed_artifact(tmp_path, monkeypatch, "notes.png",
                         b"\x89PNG\r\n\x1a\npayload")
    idents: list[int] = []
    real_read_bytes = Path.read_bytes

    def spy_read(self, *a, **k):
        idents.append(threading.get_ident())
        return real_read_bytes(self, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", spy_read)
    resp = await ws_module.handle_artifact_get(art["id"])
    assert resp["exists"] is True
    assert resp["content"].startswith("data:image/png;base64,")
    assert idents, "the artifact file was never read"
    assert all(i != threading.get_ident() for i in idents), (
        "artifact bytes were read on the event-loop thread — the "
        "read must run off-loop (asyncio.to_thread)")


@pytest.mark.asyncio
@pytest.mark.parametrize("name,mime", [
    ("pic.bmp", "image/bmp"), ("icon.ico", "image/x-icon")])
async def test_artifact_get_maps_bmp_and_ico_to_image_mimes(
        tmp_path, monkeypatch, name, mime):
    art = _seed_artifact(tmp_path, monkeypatch, name, b"BM-fake-image-bytes")
    resp = await ws_module.handle_artifact_get(art["id"])
    assert resp["error"] is None
    assert resp["content"].startswith(f"data:{mime};base64,"), (
        f"{name} previews must be served as {mime}, got: "
        f"{(resp['content'] or '')[:60]}")


# ---------------------------------------------------------------------------
# E1-10 — save_config failures are answered, never fatal to the session
# ---------------------------------------------------------------------------


def test_save_config_failure_is_answered_and_session_survives(
        ws_env, monkeypatch):
    def boom(cfg):
        raise OSError("disk full")

    monkeypatch.setattr(ws_module, "save_config", boom)
    with TestClient(app) as client, _connect(client) as ws:
        _hello(ws)
        _send(ws, EventType.REASONING_EFFORT_SET_REQUEST,
              {"effort": "high"}, "eff-1")
        ev = _recv_event(ws)
        assert ev.type == EventType.REASONING_EFFORT_SET_RESPONSE
        assert ev.payload["success"] is False
        assert "disk full" in ev.payload["error"]

        _send(ws, EventType.DAILY_BUDGET_SET_REQUEST,
              {"daily_budget_usd": 5}, "bud-1")
        ev = _recv_event(ws)
        assert ev.type == EventType.DAILY_BUDGET_SET_RESPONSE
        assert ev.payload["ok"] is False
        assert "disk full" in ev.payload["error"]

        # The session — and any task it might be running — survives.
        _send(ws, EventType.PING, {}, "ping-1")
        ev = _recv_event(ws)
        assert ev.type == EventType.PONG


# ---------------------------------------------------------------------------
# E1-12 — TOOL_POLICY_SET honesty
# ---------------------------------------------------------------------------


def test_tool_policy_set_reports_failure_when_nothing_is_saved(ws_env):
    with TestClient(app) as client, _connect(client) as ws:
        _hello(ws)
        _send(ws, EventType.TOOL_POLICY_SET_REQUEST, {}, "tp-1")
        ev = _recv_event(ws)
        assert ev.type == EventType.TOOL_POLICY_SET_RESPONSE
        assert ev.payload["success"] is False, (
            "a request missing tool_name/policy persists nothing and "
            "must not report success")

        _send(ws, EventType.TOOL_POLICY_SET_REQUEST,
              {"tool_name": "read_file"}, "tp-2")
        ev = _recv_event(ws)
        assert ev.payload["success"] is False

        _send(ws, EventType.TOOL_POLICY_SET_REQUEST,
              {"tool_name": "read_file", "policy": "Always Allow"}, "tp-3")
        ev = _recv_event(ws)
        assert ev.payload["success"] is True
        assert ev.payload["policy"] == "Always Allow"
        assert engine_state.user_config.tool_policies["read_file"] == "Always Allow"


# ---------------------------------------------------------------------------
# E1-05 — MEMORY_FACT_UPDATE guard; E1-11 — memory reads off the loop
# ---------------------------------------------------------------------------


class _StubBackend:
    name = "stub"

    def __init__(self):
        self.calls: list[str] = []

    def list_episodes(self, limit=50):
        self.calls.append("list_episodes")
        time.sleep(0.6)
        return []

    def embedding_stats(self):
        return {}

    def list_facts(self, limit=100, include_history=False):
        self.calls.append("list_facts")
        time.sleep(0.6)
        return []

    def search_facts(self, query, limit=100, include_history=False):
        return self.list_facts(limit=limit, include_history=include_history)

    def update_fact(self, fact_id, fact_text):
        raise RuntimeError("store exploded")


@pytest.fixture
def stub_backend_env(ws_env, monkeypatch):
    backend = _StubBackend()
    monkeypatch.setattr(
        ws_module, "backend_or_none", lambda cfg, context=None: backend)
    return backend


def test_memory_fact_update_backend_error_is_answered(stub_backend_env):
    with TestClient(app) as client, _connect(client) as ws:
        _hello(ws)
        _send(ws, EventType.MEMORY_FACT_UPDATE_REQUEST,
              {"fact_id": "f1", "fact_text": "new text"}, "mf-1")
        ev = _recv_event(ws)
        assert ev.type == EventType.MEMORY_FACT_UPDATE_RESPONSE
        assert ev.payload["success"] is False
        assert "store exploded" in ev.payload["error"]
        # The connection survives a raising backend.
        _send(ws, EventType.PING, {}, "ping-1")
        assert _recv_event(ws).type == EventType.PONG


def test_memory_read_handlers_answer_from_background(stub_backend_env):
    """The read handlers' backend calls are slow (0.6 s). Answered
    from a background task (the RUX-B-08 pattern), a PING sent right
    behind the request is answered BEFORE the read response; inline
    on the receive loop, the read response comes first."""
    with TestClient(app) as client, _connect(client) as ws:
        _hello(ws)
        _send(ws, EventType.MEMORY_EPISODES_REQUEST, {}, "me-1")
        _send(ws, EventType.PING, {}, "ping-1")
        first = _recv_event(ws)
        second = _recv_event(ws)
        assert first.type == EventType.PONG, (
            "MEMORY_EPISODES was answered inline on the receive loop; "
            "the PING queued behind it")
        assert second.type == EventType.MEMORY_EPISODES_RESPONSE

        _send(ws, EventType.MEMORY_GRAPH_REQUEST, {}, "mg-1")
        _send(ws, EventType.PING, {}, "ping-2")
        first = _recv_event(ws)
        second = _recv_event(ws)
        assert first.type == EventType.PONG, (
            "MEMORY_GRAPH was answered inline on the receive loop")
        assert second.type == EventType.MEMORY_GRAPH_RESPONSE


# ---------------------------------------------------------------------------
# E1-06 / E1-08 — plugin installs: off-loop prepare, expiry, replies,
# disconnect cleanup (N-19 slice)
# ---------------------------------------------------------------------------


def test_plugin_prepare_runs_off_the_loop(plugin_env, monkeypatch):
    calls: list = []
    real_to_thread = asyncio.to_thread

    async def spy(func, /, *args, **kwargs):
        calls.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(ws_module.asyncio, "to_thread", spy)
    src = _plugin_source(plugin_env.tmp_path)
    with TestClient(app) as client, _connect(client) as ws:
        _hello(ws)
        _send(ws, EventType.PLUGIN_INSTALL_REQUEST,
              {"source": str(src)}, "prep-1")
        ev = _recv_event(ws)
        assert ev.type == EventType.PLUGIN_APPROVAL_REQUEST
    assert engine_state.plugin_installer.prepare_install in calls, (
        "prepare_install must run via asyncio.to_thread, not "
        "synchronously on the receive loop")


def test_plugin_grant_with_unknown_id_gets_a_reply(plugin_env):
    with TestClient(app) as client, _connect(client) as ws:
        _hello(ws)
        _send(ws, EventType.PLUGIN_APPROVAL_GRANTED, {}, "ghost-grant")
        _send(ws, EventType.PING, {}, "ping-1")
        first = _recv_event(ws)
        assert first.type == EventType.PLUGIN_INSTALL_RESPONSE, (
            "a grant naming an unknown/expired install id must be "
            "answered, not met with silence")
        assert first.payload["success"] is False
        assert _recv_event(ws).type == EventType.PONG


def test_plugin_pending_install_is_dropped_on_disconnect(plugin_env):
    src = _plugin_source(plugin_env.tmp_path)
    with TestClient(app) as client:
        with _connect(client) as ws:
            _hello(ws)
            _send(ws, EventType.PLUGIN_INSTALL_REQUEST,
                  {"source": str(src)}, "prep-1")
            ev = _recv_event(ws)
            assert ev.type == EventType.PLUGIN_APPROVAL_REQUEST
            assert "prep-1" in ws_module.pending_plugin_installs
        # Socket closed: this connection's unanswered install must
        # not outlive it.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if "prep-1" not in ws_module.pending_plugin_installs:
                break
            time.sleep(0.05)
        assert "prep-1" not in ws_module.pending_plugin_installs, (
            "pending plugin install leaked past its connection's "
            "disconnect")


def test_plugin_pending_install_expires(plugin_env, monkeypatch):
    monkeypatch.setattr(ws_module, "PLUGIN_INSTALL_TTL_SECONDS", 0)
    src = _plugin_source(plugin_env.tmp_path)
    with TestClient(app) as client, _connect(client) as ws:
        _hello(ws)
        _send(ws, EventType.PLUGIN_INSTALL_REQUEST,
              {"source": str(src)}, "exp-a")
        assert _recv_event(ws).type == EventType.PLUGIN_APPROVAL_REQUEST
        # Preparing a second install sweeps the (now expired) first.
        _send(ws, EventType.PLUGIN_INSTALL_REQUEST,
              {"source": str(src)}, "exp-b")
        assert _recv_event(ws).type == EventType.PLUGIN_APPROVAL_REQUEST
        assert "exp-a" not in ws_module.pending_plugin_installs
        # Granting the expired one is answered as unknown.
        _send(ws, EventType.PLUGIN_APPROVAL_GRANTED, {}, "exp-a")
        _send(ws, EventType.PING, {}, "ping-1")
        first = _recv_event(ws)
        assert first.type == EventType.PLUGIN_INSTALL_RESPONSE
        assert first.payload["success"] is False


# ---------------------------------------------------------------------------
# E1-09 + N-44 — approval resume awaits the displaced runner;
# CANCEL_TASK names the real task
# ---------------------------------------------------------------------------


@pytest.fixture
def resume_env(ws_env, monkeypatch):
    from aether_engine.tools.registry import ToolRegistry

    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        ws_module, "get_persisted_run_config", AsyncMock(return_value={}))
    monkeypatch.setattr(
        ws_module, "_create_provider_instance", lambda *a, **k: object())
    monkeypatch.setattr(
        ws_module, "build_tool_registry", lambda **k: ToolRegistry())
    return ws_env


def _blocking_gen(cleaned: threading.Event, task_id: str):
    async def gen():
        yield Event(type=EventType.TASK_CREATED,
                    payload={"task_id": task_id, "path": "graph",
                             "prompt": "x"})
        try:
            await asyncio.sleep(3600)
        finally:
            # Slow unwind: cancellation cleanup takes real time, so
            # a resume pump started without awaiting the old runner
            # observably overtakes it.
            await asyncio.sleep(0.3)
            cleaned.set()

    return gen()


def _instant_gen(task_id: str):
    async def gen():
        yield Event(type=EventType.TASK_CREATED,
                    payload={"task_id": task_id, "path": "graph",
                             "prompt": "x"})
        yield Event(type=EventType.TASK_COMPLETED,
                    payload={"task_id": task_id})

    return gen()


def _install_resume_fakes(monkeypatch, gens: list):
    def fake_resume(**kwargs):
        return gens.pop(0)

    monkeypatch.setattr(ws_module, "resume_langgraph_task", fake_resume)


def _recv_until(ws, type_, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ev = _recv_event(ws, timeout=max(1.0, deadline - time.time()))
        if ev.type == type_:
            return ev
    raise AssertionError(f"never received {type_}")


def test_approval_resume_awaits_the_displaced_runner(
        resume_env, monkeypatch):
    cleaned = threading.Event()
    _install_resume_fakes(monkeypatch, [
        _blocking_gen(cleaned, "task-old-1"),
        _instant_gen("task-new-2"),
    ])
    with TestClient(app) as client, _connect(client) as ws:
        _hello(ws)
        _send(ws, EventType.TOOL_APPROVAL_GRANTED,
              {"approval_key": "thread-1"}, "g1")
        ev = _recv_until(ws, EventType.TASK_CREATED)
        assert ev.payload["task_id"] == "task-old-1"

        _send(ws, EventType.TOOL_APPROVAL_GRANTED,
              {"approval_key": "thread-2"}, "g2")
        ev = _recv_until(ws, EventType.TASK_CREATED)
        assert ev.payload["task_id"] == "task-new-2"
        assert cleaned.is_set(), (
            "the resume pump started before the displaced task's "
            "runner finished unwinding — the resume path cancels "
            "the live runner but never awaits it")


def test_cancel_task_names_the_real_task_id(resume_env, monkeypatch):
    cleaned = threading.Event()
    _install_resume_fakes(monkeypatch, [_blocking_gen(cleaned, "task-old-1")])
    with TestClient(app) as client, _connect(client) as ws:
        _hello(ws)
        _send(ws, EventType.TOOL_APPROVAL_GRANTED,
              {"approval_key": "thread-1"}, "g1")
        ev = _recv_until(ws, EventType.TASK_CREATED)
        assert ev.payload["task_id"] == "task-old-1"

        _send(ws, EventType.CANCEL_TASK, {}, "c1")
        ev = _recv_until(ws, EventType.TASK_CANCELLED)
        assert ev.payload["task_id"] == "task-old-1", (
            "CANCEL_TASK must name the task it actually cancelled, "
            "not a hard-coded 'unknown'")


# ---------------------------------------------------------------------------
# N-05 — send serialisation unit
# ---------------------------------------------------------------------------


def test_send_locked_socket_serialises_concurrent_sends():
    from aether_engine.routes.websocket import _SendLockedWebSocket

    class FakeWS:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.sent: list[str] = []

        async def send_text(self, data):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.02)
            self.sent.append(data)
            self.active -= 1

    async def main():
        fake = FakeWS()
        wrapped = _SendLockedWebSocket(fake)
        await asyncio.gather(*(wrapped.send_text(f"m{i}") for i in range(8)))
        return fake

    fake = asyncio.run(main())
    assert fake.max_active == 1, (
        "concurrent send_text calls interleaved on one socket")
    assert sorted(fake.sent) == [f"m{i}" for i in range(8)]


# ---------------------------------------------------------------------------
# N-43 — seam loaders restored on disconnect
# ---------------------------------------------------------------------------


def test_connection_restores_module_global_seam_loaders(ws_env):
    from aether_engine.browser import policy as policy_module
    from aether_engine.browser.policy import (
        set_browser_policy_loader,
    )
    from aether_engine.tools import web_search

    sentinel_key = lambda name: "sentinel-key"
    sentinel_cfg = lambda: ("sentinel", True)
    sentinel_policy = lambda: "sentinel-policy"
    orig_key, orig_cfg = web_search._key_loader, web_search._config_loader
    orig_policy = policy_module._policy_loader
    web_search.set_search_key_loader(sentinel_key)
    web_search.set_search_config_loader(sentinel_cfg)
    set_browser_policy_loader(sentinel_policy)
    try:
        with TestClient(app) as client:
            with _connect(client) as ws:
                _hello(ws)
                # The seam setters run during connection setup,
                # before the receive loop starts answering — a PONG
                # proves the loop (and therefore the setters) ran.
                _send(ws, EventType.PING, {}, "ping-1")
                assert _recv_event(ws).type == EventType.PONG
                # While connected, the engine's loaders are installed.
                assert web_search._key_loader is not sentinel_key
            deadline = time.time() + 3.0
            while time.time() < deadline:
                if (web_search._key_loader is sentinel_key
                        and web_search._config_loader is sentinel_cfg
                        and policy_module._policy_loader is sentinel_policy):
                    break
                time.sleep(0.05)
            assert web_search._key_loader is sentinel_key, (
                "the search key loader a connection overwrote was "
                "never restored on disconnect")
            assert web_search._config_loader is sentinel_cfg
            assert policy_module._policy_loader is sentinel_policy
    finally:
        web_search.set_search_key_loader(orig_key)
        web_search.set_search_config_loader(orig_cfg)
        set_browser_policy_loader(orig_policy)


# ---------------------------------------------------------------------------
# N-44 — /health reports the real initialisation state
# ---------------------------------------------------------------------------


def test_health_reflects_initialisation_state(ws_env, monkeypatch):
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    # An engine whose state never finished initialising must not
    # report "ok": /health is the shell's readiness probe. The flag
    # is cleared and initialize() is neutralised so the lifespan
    # (if this TestClient version runs it for a bare request)
    # cannot silently re-initialise the state mid-assertion.
    was = engine_state.initialized
    try:
        engine_state.initialized = False
        monkeypatch.setattr(engine_state, "initialize", lambda: "")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["status"] != "ok"
    finally:
        engine_state.initialized = was
