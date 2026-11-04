"""Wave 4, Slice 1 — RUX-B-06…B-10: engine S3 blockers off the WS loop.

Each remaining inline family from Lane B §3 now answers from a
background task (or a worker thread for synchronous bulk work),
following the Wave 1 pattern:

- B-06 PROVIDER_SAVE answers the save immediately; discovery runs
  afterwards and lands as a PROVIDER_LIST_RESPONSE push.
- B-07 START_TASK's pre-work (cancel wait, rewind commit, /mcp:
  slash run, intent classification) runs in a chained launch task.
- B-08 read-path lookups + exports run in tasks / on threads.
- B-09 voice session starts run in a chained task; dictation
  chunk/stop transcriptions run in tasks chained in arrival order.
- B-10 plugin install/uninstall/toggle run in tasks, with the
  synchronous file work on worker threads.

The responsiveness tests share one shape: a slow stubbed operation,
an INSTANT message (MODEL_SET_DEFAULT) sent right behind it, and an
assertion that the instant answer lands BEFORE the slow response —
impossible under the old serial receive loop. Contract tests pin the
response payloads the renderer already consumes.

Harness follows the ws_env pattern of test_rux_wave1_ws_discovery.py.
No live network: every slow operation is a scripted stub.
"""
import asyncio
import base64
import queue
import threading
import time
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

import aether_engine.config as config_module
import aether_engine.orchestration.core as core_module
from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig, save_config
from aether_engine.memory import session_store
from aether_engine.providers.discovery import DiscoveryError, DiscoveryResult
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.intent import IntentDecision
from aether_engine.routing.policy import RoutingPolicy
from aether_engine.routing.registry import ModelEntry, ModelRegistry

PROVIDERS = ["google_gemini", "openai", "anthropic"]


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
    monkeypatch.setattr(
        engine_state, "configured_providers", ["google_gemini"])
    failures = getattr(core_module, "_discovery_failures", None)
    if failures is not None:
        failures.clear()
    yield {"tmp_path": tmp_path, "config": cfg, "config_path": config_path}
    if failures is not None:
        failures.clear()


def _mock_store(stack, providers):
    mock_store = stack.enter_context(
        patch.object(ws_module, "SecretStore")).return_value
    mock_store.list_providers.return_value = providers
    mock_store.load_provider.return_value = "fake-key"
    return mock_store


def _connect_ws(stack):
    client = stack.enter_context(TestClient(app))
    ws = stack.enter_context(client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"},
    ))
    ws.receive_text()  # HELLO
    return ws


def _send(ws, type_, payload=None, request_id=None):
    ws.send_text(Event(
        type=type_, request_id=request_id, payload=payload or {}).to_json())


def _send_set_default(ws):
    _send(ws, EventType.MODEL_SET_DEFAULT,
          {"provider": "google_gemini", "model": "gemini-2.5-pro"})


def _receive_until(ws, wanted, limit=20):
    events = []
    for _ in range(limit):
        ev = Event.from_json(ws.receive_text())
        events.append(ev)
        if ev.type == wanted:
            return events
    raise AssertionError(
        f"{wanted} not received; got {[ev.type for ev in events]}")


def _collect_until(ws, wanted_types, limit=25):
    events = []
    remaining = set(wanted_types)
    for _ in range(limit):
        ev = Event.from_json(ws.receive_text())
        events.append(ev)
        remaining.discard(ev.type)
        if not remaining:
            return events
    raise AssertionError(
        f"never received {remaining}; got {[ev.type for ev in events]}")


def _types(events):
    return [ev.type for ev in events]


def _receive_with_timeout(ws, timeout=5.0):
    """One receive on a helper thread (the pattern of
    test_e7f05_plugin_identity_contract): TestClient receives block
    indefinitely, so a wedged handler would hang the suite instead
    of failing — bound it and turn the wedge into an assertion."""
    q: queue.Queue = queue.Queue()

    def _do():
        try:
            q.put(("ok", ws.receive_text()))
        except Exception as e:  # noqa: BLE001 — relayed to the test thread
            q.put(("err", e))

    threading.Thread(target=_do, daemon=True).start()
    try:
        kind, value = q.get(timeout=timeout)
    except queue.Empty:
        raise AssertionError(
            f"No WebSocket message within {timeout}s — the handler "
            "is wedged (the old inline behaviour).") from None
    if kind == "err":
        raise value
    return Event.from_json(value)


def _provider_entry(providers, name):
    return next(p for p in providers if p["name"] == name)


# ---------------------------------------------------------------------------
# RUX-B-06 — PROVIDER_SAVE: immediate answer, deferred discovery
# ---------------------------------------------------------------------------


class TestProviderSaveDiscoveryDeferred:
    def test_save_answers_before_discovery_then_list_push_lands(self, ws_env):
        async def slow_fetch(*args, **kwargs):
            await asyncio.sleep(2)
            return DiscoveryResult(
                models=["gpt-slow"],
                excluded=[{"id": "embed-x", "kind": "embedding"}],
                error=None)

        stack = ExitStack()
        _mock_store(stack, PROVIDERS)
        stack.enter_context(patch.object(
            ws_module, "fetch_available_models",
            AsyncMock(side_effect=slow_fetch)))
        with stack:
            ws = _connect_ws(stack)
            start = time.monotonic()
            _send(ws, EventType.PROVIDER_SAVE_REQUEST,
                  {"provider": "openai", "api_key": "sk-new"})
            save_ev = _receive_until(ws, EventType.PROVIDER_SAVE_RESPONSE)[-1]
            elapsed = time.monotonic() - start

            # The save itself is instant work: its answer must NOT
            # wait for the 2 s discovery (the old inline path did).
            assert elapsed < 1.5
            assert save_ev.payload == {
                "provider": "openai", "success": True,
                "models": [], "excluded_models": [],
            }

            _send_set_default(ws)
            events = _collect_until(ws, [
                EventType.MODEL_DEFAULT_CHANGED,
                EventType.PROVIDER_LIST_RESPONSE,
            ])
            order = _types(events)
            # Control traffic stays live while discovery runs, and
            # the discovered models land as a list push afterwards.
            assert order.index(EventType.MODEL_DEFAULT_CHANGED) < order.index(
                EventType.PROVIDER_LIST_RESPONSE)
            list_ev = next(
                ev for ev in events if ev.type == EventType.PROVIDER_LIST_RESPONSE)
            openai = _provider_entry(list_ev.payload["providers"], "openai")
            assert openai["models"] == ["gpt-slow"]
            assert openai["excluded_models"] == [
                {"id": "embed-x", "kind": "embedding"}]
            # The live config object (the app lifespan may have
            # reloaded it from disk at startup) carries the models.
            assert engine_state.user_config.provider_models["openai"] == ["gpt-slow"]

    def test_save_discovery_error_lands_on_the_list_entry(self, ws_env):
        stack = ExitStack()
        _mock_store(stack, PROVIDERS)
        stack.enter_context(patch.object(
            ws_module, "fetch_available_models",
            AsyncMock(return_value=DiscoveryResult(
                models=[], excluded=[],
                error=DiscoveryError(code=403, message="forbidden")))))
        with stack:
            ws = _connect_ws(stack)
            _send(ws, EventType.PROVIDER_SAVE_REQUEST,
                  {"provider": "openai", "api_key": "sk-x"})
            save_ev = _receive_until(ws, EventType.PROVIDER_SAVE_RESPONSE)[-1]
            # The save succeeded; the discovery outcome is no longer
            # folded into the save response — it arrives with the
            # list push, on the provider's entry.
            assert save_ev.payload["success"] is True
            assert "discovery_error" not in save_ev.payload

            list_ev = _receive_until(ws, EventType.PROVIDER_LIST_RESPONSE)[-1]
            openai = _provider_entry(list_ev.payload["providers"], "openai")
            assert openai["discovery_error"]["code"] == 403
            assert openai["discovery_error"]["message"] == "forbidden"


# ---------------------------------------------------------------------------
# RUX-B-07 — START_TASK pre-work off the receive loop
# ---------------------------------------------------------------------------


class TestStartTaskPreworkOffLoop:
    def test_instant_message_answered_before_slow_classification(self, ws_env):
        async def slow_classify(prompt, provider=None):
            await asyncio.sleep(6)
            return IntentDecision("chat", "slow verdict", "model")

        def fake_fast_chat(*args, **kwargs):
            async def gen():
                yield Event(
                    type=EventType.TASK_COMPLETED,
                    request_id=kwargs.get("request_id"),
                    payload={"task_id": "fake-task", "state": "succeeded",
                             "response": "chat answer",
                             "session_id": kwargs.get("session_id")})
            return gen()

        sid = session_store.create_session("Existing chat")
        stack = ExitStack()
        _mock_store(stack, ["google_gemini"])
        stack.enter_context(patch.object(
            ws_module, "classify_intent", AsyncMock(side_effect=slow_classify)))
        stack.enter_context(patch.object(
            ws_module, "_create_provider_instance", return_value=object()))
        stack.enter_context(patch.object(
            ws_module, "run_fast_chat", fake_fast_chat))
        with stack:
            ws = _connect_ws(stack)
            _send(ws, EventType.START_TASK,
                  {"prompt": "hello there", "session_id": sid})
            _send_set_default(ws)
            # The classification is still 6 s out. The instant
            # message must be answered NOW — under the old inline
            # pre-work nothing at all could arrive until the
            # classification (and the whole start) had finished.
            ev = _receive_with_timeout(ws, timeout=3.0)
            assert ev.type == EventType.MODEL_DEFAULT_CHANGED
            # And the task itself still completes, after its
            # classification lands.
            done = _receive_until(ws, EventType.TASK_COMPLETED)[-1]
            assert done.payload["response"] == "chat answer"


# ---------------------------------------------------------------------------
# RUX-B-08 — read-path lookups + exports off the loop
# ---------------------------------------------------------------------------


class TestReadPathOffLoop:
    def test_instant_message_answered_before_slow_hub_search(self, ws_env, monkeypatch):
        class SlowHub:
            async def search(self, source, query, family):
                await asyncio.sleep(2)
                return {"models": [{"id": "m1"}], "error": None}

        monkeypatch.setattr(engine_state, "model_hub", SlowHub())
        stack = ExitStack()
        _mock_store(stack, PROVIDERS)
        with stack:
            ws = _connect_ws(stack)
            _send(ws, EventType.MODEL_HUB_SEARCH_REQUEST,
                  {"source": "hf", "query": "whisper"})
            _send_set_default(ws)
            events = _collect_until(ws, [
                EventType.MODEL_DEFAULT_CHANGED,
                EventType.MODEL_HUB_SEARCH_RESPONSE])
            order = _types(events)
            assert order.index(EventType.MODEL_DEFAULT_CHANGED) < order.index(
                EventType.MODEL_HUB_SEARCH_RESPONSE)
            search_ev = next(
                ev for ev in events
                if ev.type == EventType.MODEL_HUB_SEARCH_RESPONSE)
            assert search_ev.payload == {"models": [{"id": "m1"}], "error": None}

    def test_instant_message_answered_before_slow_memory_export(self, ws_env):
        class SlowBackend:
            def export(self):
                time.sleep(2)
                return {"facts": [{"id": 1}], "episodes": []}

        stack = ExitStack()
        _mock_store(stack, PROVIDERS)
        stack.enter_context(patch.object(
            ws_module, "backend_or_none",
            lambda cfg, context=None: SlowBackend()))
        with stack:
            ws = _connect_ws(stack)
            _send(ws, EventType.MEMORY_EXPORT_REQUEST)
            _send_set_default(ws)
            events = _collect_until(ws, [
                EventType.MODEL_DEFAULT_CHANGED,
                EventType.MEMORY_EXPORT_RESPONSE])
            order = _types(events)
            assert order.index(EventType.MODEL_DEFAULT_CHANGED) < order.index(
                EventType.MEMORY_EXPORT_RESPONSE)
            export_ev = next(
                ev for ev in events
                if ev.type == EventType.MEMORY_EXPORT_RESPONSE)
            assert export_ev.payload == {
                "success": True,
                "export": {"facts": [{"id": 1}], "episodes": []},
            }


# ---------------------------------------------------------------------------
# RUX-B-09 — voice family off the loop, dictation chained in order
# ---------------------------------------------------------------------------


class _FakeVoicePipeline:
    def __init__(self):
        self.ice_listener = None
        self.stopped = False

    async def start(self):
        await asyncio.sleep(2)
        return {"sdp": "fake-config"}

    def set_rtc_ice_listener(self, listener):
        self.ice_listener = listener

    async def run(self):
        await asyncio.sleep(60)

    async def stop(self):
        self.stopped = True


class TestVoiceOffLoop:
    def test_instant_message_answered_before_slow_session_start(
            self, ws_env, monkeypatch):
        pipeline = _FakeVoicePipeline()
        sid = session_store.create_session("Voice chat")
        stack = ExitStack()
        _mock_store(stack, PROVIDERS)
        stack.enter_context(patch.object(
            ws_module, "_create_provider_instance", return_value=object()))
        stack.enter_context(patch(
            "aether_engine.voice.pipeline.create_voice_pipeline",
            return_value=(pipeline, None)))
        with stack:
            ws = _connect_ws(stack)
            _send(ws, EventType.VOICE_SESSION_START_REQUEST,
                  {"session_id": sid})
            _send_set_default(ws)
            events = _collect_until(ws, [
                EventType.MODEL_DEFAULT_CHANGED,
                EventType.VOICE_SESSION_START_RESPONSE])
            order = _types(events)
            assert order.index(EventType.MODEL_DEFAULT_CHANGED) < order.index(
                EventType.VOICE_SESSION_START_RESPONSE)
            start_ev = next(
                ev for ev in events
                if ev.type == EventType.VOICE_SESSION_START_RESPONSE)
            assert start_ev.payload == {
                "ok": True, "session_id": sid,
                "webrtc_config": {"sdp": "fake-config"}, "error": None,
            }
            # Clean shutdown of the live session.
            _send(ws, EventType.VOICE_SESSION_STOP_REQUEST,
                  {"session_id": sid})
            stop_ev = _receive_until(
                ws, EventType.VOICE_SESSION_STOP_RESPONSE)[-1]
            assert stop_ev.payload == {"ok": True}
            assert pipeline.stopped

    def test_instant_message_answered_before_slow_dictation_chunk(
            self, ws_env, monkeypatch):
        class SlowStt:
            def __init__(self):
                self.open_calls = 0

            def unavailable_reason(self):
                return None

            async def open(self):
                self.open_calls += 1

            async def close(self):
                pass

            async def transcribe(self, audio, *, sample_rate):
                await asyncio.sleep(2)
                return "hello"

        fake_stt = SlowStt()
        monkeypatch.setattr(ws_module, "create_stt_engine", lambda: fake_stt)
        stack = ExitStack()
        _mock_store(stack, PROVIDERS)
        with stack:
            ws = _connect_ws(stack)
            _send(ws, EventType.DICTATION_CHUNK_REQUEST, {
                "audio": base64.b64encode(b"a").decode("ascii"),
                "sample_rate": 16000, "is_final": False,
            })
            _send_set_default(ws)
            events = _collect_until(ws, [
                EventType.MODEL_DEFAULT_CHANGED,
                EventType.DICTATION_CHUNK_RESPONSE])
            order = _types(events)
            assert order.index(EventType.MODEL_DEFAULT_CHANGED) < order.index(
                EventType.DICTATION_CHUNK_RESPONSE)
            chunk_ev = next(
                ev for ev in events
                if ev.type == EventType.DICTATION_CHUNK_RESPONSE)
            assert chunk_ev.payload == {
                "ok": True, "text": "hello", "is_final": False, "error": None}

    def test_dictation_chunks_are_answered_in_arrival_order(
            self, ws_env, monkeypatch):
        class VariableStt:
            def unavailable_reason(self):
                return None

            async def open(self):
                pass

            async def close(self):
                pass

            async def transcribe(self, audio, *, sample_rate):
                if audio == b"first":
                    await asyncio.sleep(0.6)
                    return "first-text"
                return "second-text"

        monkeypatch.setattr(
            ws_module, "create_stt_engine", lambda: VariableStt())
        stack = ExitStack()
        _mock_store(stack, PROVIDERS)
        with stack:
            ws = _connect_ws(stack)
            for audio in (b"first", b"second"):
                _send(ws, EventType.DICTATION_CHUNK_REQUEST, {
                    "audio": base64.b64encode(audio).decode("ascii"),
                    "sample_rate": 16000, "is_final": False,
                })
            texts = []
            for _ in range(10):
                ev = Event.from_json(ws.receive_text())
                if ev.type == EventType.DICTATION_CHUNK_RESPONSE:
                    texts.append(ev.payload["text"])
                if len(texts) == 2:
                    break
            # The chain, not duration, decides the answer order.
            assert texts == ["first-text", "second-text"]


# ---------------------------------------------------------------------------
# RUX-B-10 — plugin lifecycle off the loop
# ---------------------------------------------------------------------------


class _StubInstaller:
    def __init__(self, delay=0.0, error=None):
        self.delay = delay
        self.error = error
        self.installed = None
        self.uninstalled = []

    def prepare_install(self, source):
        return {"source": source, "manifest": {"name": "plug"}}

    def install(self, data):
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        self.installed = data

    def uninstall(self, name):
        self.uninstalled.append(name)

    def get_all(self):
        return {}


class TestPluginLifecycleOffLoop:
    def _install_and_grant(self, ws, request_id):
        _send(ws, EventType.PLUGIN_INSTALL_REQUEST,
              {"source": "somewhere"}, request_id=request_id)
        approval = _receive_until(ws, EventType.PLUGIN_APPROVAL_REQUEST)[-1]
        assert approval.payload["plugin_name"] == "plug"
        _send(ws, EventType.PLUGIN_APPROVAL_GRANTED, request_id=request_id)

    def test_instant_message_answered_before_slow_install(
            self, ws_env, monkeypatch):
        stub = _StubInstaller(delay=2)
        monkeypatch.setattr(engine_state, "plugin_installer", stub)
        stack = ExitStack()
        _mock_store(stack, PROVIDERS)
        with stack:
            ws = _connect_ws(stack)
            self._install_and_grant(ws, "plug-slow")
            _send_set_default(ws)
            events = _collect_until(ws, [
                EventType.MODEL_DEFAULT_CHANGED,
                EventType.PLUGIN_INSTALL_RESPONSE])
            order = _types(events)
            assert order.index(EventType.MODEL_DEFAULT_CHANGED) < order.index(
                EventType.PLUGIN_INSTALL_RESPONSE)
            install_ev = next(
                ev for ev in events
                if ev.type == EventType.PLUGIN_INSTALL_RESPONSE)
            assert install_ev.payload == {"success": True}
            assert stub.installed is not None

    def test_install_failure_payload_is_unchanged(self, ws_env, monkeypatch):
        stub = _StubInstaller(error=RuntimeError("copy failed"))
        monkeypatch.setattr(engine_state, "plugin_installer", stub)
        stack = ExitStack()
        _mock_store(stack, PROVIDERS)
        with stack:
            ws = _connect_ws(stack)
            self._install_and_grant(ws, "plug-fail")
            install_ev = _receive_until(
                ws, EventType.PLUGIN_INSTALL_RESPONSE)[-1]
            assert install_ev.payload == {
                "success": False, "error": "copy failed"}

    def test_uninstall_success_payload_is_unchanged(self, ws_env, monkeypatch):
        stub = _StubInstaller()
        monkeypatch.setattr(engine_state, "plugin_installer", stub)
        stack = ExitStack()
        _mock_store(stack, PROVIDERS)
        with stack:
            ws = _connect_ws(stack)
            _send(ws, EventType.PLUGIN_UNINSTALL_REQUEST, {"name": "plug"})
            uninstall_ev = _receive_until(
                ws, EventType.PLUGIN_UNINSTALL_RESPONSE)[-1]
            assert uninstall_ev.payload == {"success": True, "name": "plug"}
            assert stub.uninstalled == ["plug"]
