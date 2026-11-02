"""E5-F07 — outer task health ignores authentication failures and
credits a failed primary for per-call fallback success.

Two mechanisms, one attribution defect in the websocket pump
(``execute_task_generator``):

1. Only a RETRIABLE ``TASK_FAILED`` reached the pump's failure
   recorder. A nonretriable failure (401 invalid key,
   ``retriable=False``) was forwarded to the client with NO health
   update, so a provider with a dead key kept a pristine
   "healthy / zero failures" slate and kept being routed to.

2. On ``TASK_COMPLETED`` the pump recorded success against the
   OUTER provider variable (the configured primary — it changes
   only on a task-level retry). When the per-call fallback inside
   ``LiteLLMProvider.call_stream`` served the response (primary
   rate-limited → fallback answered), the failed primary collected
   the health credit and the provider that did the work got none.

Fix shape pinned here:
  * every TASK_FAILED updates health for the attempt's provider
    with the payload's retriability (a 401 counts, permanently);
  * the provider layer notes which provider actually served each
    call into a task-scoped channel (``providers/serving.py``); the
    pump credits completion to the LAST noted serving provider —
    the one that served the completing call — and falls back to the
    outer provider when no call noted one;
  * cancellation records nothing, exactly as before.

All providers are scripted stubs / fakes (house pattern): no network.
"""
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import litellm
import pytest
from starlette.testclient import TestClient

import aether_engine.config as config_module
from aether_common.contracts import Event, EventType, TaskState
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig, save_config
from aether_engine.memory import session_store
from aether_engine.providers.litellm_provider import FallbackModel, LiteLLMProvider
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.health import ProviderHealthManager
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


def _cfg(**overrides) -> UserConfig:
    base = {"default_provider": "google_gemini", "default_model": "gemini-main"}
    base.update(overrides)
    return UserConfig(**base)


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    """Engine state + stores isolated to tmp (E5-F02 ws_env pattern):
    audit log path set BEFORE initialize_auth(); global config via a
    tmp CONFIG_PATH file; a FRESH health manager so the assertions
    below read only what the pump recorded."""
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    cfg = _cfg(workspace_roots=[str(tmp_path)])
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

    def set_global(**fields):
        for key, value in fields.items():
            setattr(cfg, key, value)
        save_config(cfg, config_path)

    return {"tmp_path": tmp_path, "config": cfg, "set_global": set_global}


def _health():
    return engine_state.health_manager.get_all_statuses()


def _receive_until_terminal(ws, max_events=30):
    events = []
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        events.append(ev)
        if ev.type in (EventType.TASK_COMPLETED, EventType.TASK_FAILED,
                       EventType.TASK_CANCELLED):
            return events
    raise AssertionError(
        f"no terminal event received; got {[ev.type for ev in events]}")


def _drive_graph(ws_env, gen_factory, *, prompt="Fix the bug in the parser and run the tests"):
    """Drive one START_TASK through the real pump with a scripted
    graph generator (F-09/E5-F02 house pattern)."""
    stack = ExitStack()
    mock_store = stack.enter_context(
        patch.object(ws_module, "SecretStore")).return_value
    mock_store.list_providers.return_value = PROVIDERS
    mock_store.load_provider.return_value = "fake-key"
    stack.enter_context(patch.object(
        ws_module, "_create_provider_instance", return_value=SimpleNamespace()))
    stack.enter_context(patch.object(
        ws_module, "run_langgraph_task", gen_factory))
    sid = session_store.create_session("E5-F07 attribution")
    with (
        stack,
        TestClient(app) as client,
        client.websocket_connect(
            "/ws/tasks",
            headers={"Authorization": f"Bearer {engine_state.auth_token}"},
        ) as ws,
    ):
        ws.receive_text()  # HELLO
        ws.send_text(Event(
            type=EventType.START_TASK,
            payload={"prompt": prompt, "session_id": sid},
        ).to_json())
        return _receive_until_terminal(ws)


def _scripted_graph(events_spec):
    def fake_run_langgraph_task(*args, **kwargs):
        async def gen():
            task_id = kwargs.get("task_id", "task-e5f07")
            yield Event(
                type=EventType.TASK_CREATED,
                request_id=kwargs.get("request_id"),
                payload={"task_id": task_id,
                         "prompt": kwargs.get("prompt", ""),
                         "state": TaskState.RUNNING.value},
            )
            for ev_type, payload in events_spec:
                yield Event(
                    type=ev_type,
                    request_id=kwargs.get("request_id"),
                    payload={"task_id": task_id, **payload},
                )

        return gen()

    return fake_run_langgraph_task


# ---------------------------------------------------------------------------
# Mechanism 1 — a nonretriable failure (401) must update health.
# ---------------------------------------------------------------------------


class TestNonretriableFailureAttribution:
    def test_auth_failure_records_permanent_health_failure(self, ws_env):
        events = _drive_graph(ws_env, _scripted_graph([
            (EventType.TASK_FAILED, {
                "error": "401 invalid key: incorrect API key provided",
                "state": TaskState.FAILED.value,
                "retriable": False,
                "latency_ms": 1.0,
            }),
        ]))
        # The client still receives the failure, unaltered.
        assert events[-1].type == EventType.TASK_FAILED
        assert "401" in events[-1].payload["error"]

        state = _health().get("google_gemini")
        assert state is not None, "the pump recorded no health at all"
        assert state["consecutive_failures"] == 1
        assert state["status"] == "offline"  # nonretriable -> permanent
        assert "401" in (state["last_error"] or "")

    def test_retriable_failure_still_records_and_is_delivered(self, ws_env):
        """Control: the pre-existing retriable path is unchanged —
        failure recorded (degraded on first failure), no candidates
        means no retry and the failure reaches the client."""
        import aether_engine.routing.fallback as fallback_module

        with patch.object(
            fallback_module, "get_configured_fallback_candidates",
            return_value=[],
        ):
            events = _drive_graph(ws_env, _scripted_graph([
                (EventType.TASK_FAILED, {
                    "error": "429 rate limited",
                    "state": TaskState.FAILED.value,
                    "retriable": True,
                    "latency_ms": 1.0,
                }),
            ]))
        assert events[-1].type == EventType.TASK_FAILED
        state = _health().get("google_gemini")
        assert state is not None
        assert state["consecutive_failures"] == 1
        assert state["status"] == "degraded"

    def test_cancellation_records_no_health(self, ws_env):
        """Control: a cancelled task is not a provider outcome —
        no failure, no success, as before."""
        events = _drive_graph(ws_env, _scripted_graph([
            (EventType.TASK_CANCELLED, {"state": TaskState.CANCELLED.value}),
        ]))
        assert events[-1].type == EventType.TASK_CANCELLED
        for state in _health().values():
            assert state["consecutive_failures"] == 0
            assert state["last_successful_request"] is None

    def test_completion_without_provider_calls_credits_outer_provider(self, ws_env):
        """Control: when no provider call noted a serving provider,
        completion credits the outer provider exactly as before."""
        events = _drive_graph(ws_env, _scripted_graph([
            (EventType.TASK_COMPLETED, {
                "state": TaskState.SUCCEEDED.value,
                "response": "done",
                "latency_ms": 5.0,
            }),
        ]))
        assert events[-1].type == EventType.TASK_COMPLETED
        state = _health().get("google_gemini")
        assert state is not None
        assert state["last_successful_request"] is not None
        assert state["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# The serving-provider channel (provider layer).
# ---------------------------------------------------------------------------


def _chunk(text=None, finish=None):
    delta = SimpleNamespace(
        content=text, reasoning_content=None, thinking=None, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish)],
        usage=None,
    )


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c


def _text_stream(text):
    return _Stream([_chunk(text=text), _chunk(finish="stop")])


def _openai_provider_with_anthropic_fallback():
    return LiteLLMProvider(
        api_key="sk-openai",
        model="gpt-main",
        provider_name="openai",
        fallback_models=[FallbackModel(
            model="anthropic/claude-main", api_key="sk-anthropic",
            provider_name="anthropic")],
    )


def _fallback_acompletion(calls):
    async def fake_acompletion(**kwargs):
        calls.append(kwargs["model"])
        if "claude" in kwargs["model"]:
            return _text_stream("fallback answer")
        raise litellm.RateLimitError("rate limited", "openai", kwargs["model"])

    return fake_acompletion


async def _drain(provider):
    return [c async for c in provider.call_stream(
        [{"role": "user", "content": "hello"}], tools=None, node_name="test")]


class TestServingChannel:
    def test_fallback_success_notes_the_fallback_provider(self, monkeypatch):
        import asyncio

        from aether_engine.providers import serving

        calls = []
        monkeypatch.setattr(litellm, "acompletion", _fallback_acompletion(calls))
        token = serving.begin_serving_tracking()
        try:
            chunks = asyncio.run(_drain(_openai_provider_with_anthropic_fallback()))
            assert "".join(c.text or "" for c in chunks) == "fallback answer"
            assert serving.serving_provider() == "anthropic"
        finally:
            serving.end_serving_tracking(token)

    def test_last_served_call_wins(self, monkeypatch):
        """The partial-fallback rule: the completion credit follows
        the provider that served the LAST (completing) call — earlier
        calls served by another provider do not claim it."""
        import asyncio

        from aether_engine.providers import serving

        calls = []
        monkeypatch.setattr(litellm, "acompletion", _fallback_acompletion(calls))
        primary_only = LiteLLMProvider(
            api_key="sk-openai", model="gpt-main", provider_name="openai")

        async def primary_ok(**kwargs):
            return _text_stream("primary answer")

        token = serving.begin_serving_tracking()
        try:
            # Call 1 served by the fallback (primary rate-limited)...
            asyncio.run(_drain(_openai_provider_with_anthropic_fallback()))
            assert serving.serving_provider() == "anthropic"
            # ...call 2 served by the primary itself: the note moves.
            monkeypatch.setattr(litellm, "acompletion", primary_ok)
            asyncio.run(_drain(primary_only))
            assert serving.serving_provider() == "openai"
            # Clearing (the pump does this per attempt) empties it.
            serving.clear_serving_provider()
            assert serving.serving_provider() is None
        finally:
            serving.end_serving_tracking(token)

    def test_no_scope_is_a_noop(self, monkeypatch):
        import asyncio

        from aether_engine.providers import serving

        calls = []
        monkeypatch.setattr(litellm, "acompletion", _fallback_acompletion(calls))
        # No begin_serving_tracking(): noting must silently do
        # nothing (provider calls happen outside tasks too — probes,
        # classification, titles).
        chunks = asyncio.run(_drain(_openai_provider_with_anthropic_fallback()))
        assert "".join(c.text or "" for c in chunks) == "fallback answer"
        assert serving.serving_provider() is None


# ---------------------------------------------------------------------------
# Mechanism 2 — through the real pump: a real fast-chat stream whose
# primary rate-limits and whose per-call fallback answers must credit
# the FALLBACK provider, not the failed primary.
# ---------------------------------------------------------------------------


def _drive_fast_chat(ws_env, provider, monkeypatch):
    """START_TASK routed to fast chat (classifier patched to chat),
    with the REAL run_fast_chat and a REAL LiteLLMProvider whose
    litellm transport is faked."""
    stack = ExitStack()
    mock_store = stack.enter_context(
        patch.object(ws_module, "SecretStore")).return_value
    mock_store.list_providers.return_value = PROVIDERS
    mock_store.load_provider.return_value = "fake-key"
    stack.enter_context(patch.object(
        ws_module, "_create_provider_instance", return_value=provider))
    stack.enter_context(patch.object(
        ws_module, "classify_intent",
        AsyncMock(return_value=SimpleNamespace(is_chat=True))))
    sid = session_store.create_session("E5-F07 fast chat")
    with (
        stack,
        TestClient(app) as client,
        client.websocket_connect(
            "/ws/tasks",
            headers={"Authorization": f"Bearer {engine_state.auth_token}"},
        ) as ws,
    ):
        ws.receive_text()  # HELLO
        ws.send_text(Event(
            type=EventType.START_TASK,
            payload={"prompt": "hello, how are you?", "session_id": sid},
        ).to_json())
        return _receive_until_terminal(ws)


class TestPumpFallbackAttribution:
    def test_fallback_success_credits_serving_provider(self, ws_env, monkeypatch):
        ws_env["set_global"](default_provider="openai", default_model="gpt-main")
        calls = []
        monkeypatch.setattr(litellm, "acompletion", _fallback_acompletion(calls))

        audit_events = []

        class SpyLogger:
            def log_event(self, name, payload):
                audit_events.append((name, payload))

        monkeypatch.setattr("aether_engine.audit.AuditLogger", SpyLogger)

        events = _drive_fast_chat(
            ws_env, _openai_provider_with_anthropic_fallback(), monkeypatch)
        assert events[-1].type == EventType.TASK_COMPLETED
        assert events[-1].payload["response"] == "fallback answer"
        # The per-call fallback really happened (primary tried first).
        assert calls and "claude" in calls[-1]
        # NODE_FALLBACK_TRIGGERED audit emission preserved (E5-F01).
        assert any(name == "NODE_FALLBACK_TRIGGERED" for name, _ in audit_events)

        statuses = _health()
        anthropic = statuses.get("anthropic")
        assert anthropic is not None, "the serving provider got no health entry"
        assert anthropic["last_successful_request"] is not None
        assert anthropic["consecutive_failures"] == 0
        openai = statuses.get("openai")
        assert openai is None or openai["last_successful_request"] is None, (
            "the failed primary collected the completion credit")

    def test_primary_success_credits_primary(self, ws_env, monkeypatch):
        """Control: no fallback — the primary serves and is credited,
        exactly as before."""
        ws_env["set_global"](default_provider="openai", default_model="gpt-main")

        async def primary_ok(**kwargs):
            return _text_stream("primary answer")

        monkeypatch.setattr(litellm, "acompletion", primary_ok)
        provider = LiteLLMProvider(
            api_key="sk-openai", model="gpt-main", provider_name="openai")
        events = _drive_fast_chat(ws_env, provider, monkeypatch)
        assert events[-1].type == EventType.TASK_COMPLETED
        statuses = _health()
        openai = statuses.get("openai")
        assert openai is not None
        assert openai["last_successful_request"] is not None
        anthropic = statuses.get("anthropic")
        assert anthropic is None or anthropic["last_successful_request"] is None
