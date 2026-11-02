"""Epic 5 minors batch — E5-F12, F14, F15, F16, F17, F18, F19.

Each item was re-verified against current code before fixing; the
tests below pin the promised behaviour at the real seam:

- F12: the task-retry TASK_CREATED restamp must carry the FALLBACK
  model's capabilities, not the failed primary's.
- F14: a targeted REFRESH_MODELS for a SAVED provider must apply and
  persist the fresh catalog (registry + config + disk), exactly like
  the full refresh; unsaved templates still persist nothing.
- F15: a provider that has never been probed reports "standby", not
  a minted "healthy"; reset returns to standby. Eligibility
  (is_available) is unchanged — standby providers stay routable.
- F16: a pull to a chosen destination actually places the model's
  manifest + blobs there (copied from the daemon store); when the
  model is not in the store the task fails honestly instead of
  recording a fictional path.
- F17: the Ollama pull client is constructed with a finite
  phase-bounded timeout, never timeout=None.
- F18: diagnostics verifies the Tavily key with a real probe —
  200 = verified, 401/403 = rejected, anything else = present but
  unverified. Presence alone never earns "verified".
- F19: auto title/summary resolution prefers an available tiny
  local model (local provider, chat + fast capabilities) over the
  task/run provider; explicit overrides still win.

No live network, no live Ollama: scripted fakes throughout (house
patterns from the E5-F07/F09 suites).
"""
import json
import urllib.error
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.testclient import TestClient

import aether_engine.config as config_module
import aether_engine.orchestration.core as core_module
import aether_engine.routing.fallback as fallback_module
from aether_common.contracts import Event, EventType, TaskState
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig, save_config
from aether_engine.langgraph.compaction import resolve_summariser_provider
from aether_engine.memory import session_store
from aether_engine.models import local_models
from aether_engine.models.local_models import OllamaManager
from aether_engine.providers.discovery import DiscoveryResult
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.health import ProviderHealthManager, ProviderHealthState
from aether_engine.routing.policy import RoutingPolicy
from aether_engine.routing.registry import ModelEntry, ModelRegistry
from aether_engine.session_titles import resolve_title_provider
from aether_engine.validation import diagnostics

PROVIDERS = ["google_gemini", "openai", "anthropic"]


# ---------------------------------------------------------------------------
# Shared WS fixture (E5-F07 ws_env pattern): audit log path set BEFORE
# initialize_auth(); global config via a tmp CONFIG_PATH file; fresh
# health manager + scripted registry.
# ---------------------------------------------------------------------------


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
    return {"tmp_path": tmp_path, "config": cfg, "config_path": config_path}


def _connect():
    return TestClient(app)


def _mock_store(stack, providers):
    mock_store = stack.enter_context(
        patch.object(ws_module, "SecretStore")).return_value
    mock_store.list_providers.return_value = providers
    mock_store.load_provider.return_value = "fake-key"
    return mock_store


# ---------------------------------------------------------------------------
# E5-F12 — retry restamp carries the fallback model's capabilities
# ---------------------------------------------------------------------------


def _two_attempt_graph():
    calls = {"n": 0}

    def factory(*args, **kwargs):
        calls["n"] += 1
        attempt = calls["n"]

        async def gen():
            task_id = kwargs.get("task_id", "task-f12")
            yield Event(
                type=EventType.TASK_CREATED,
                request_id=kwargs.get("request_id"),
                payload={"task_id": task_id,
                         "prompt": kwargs.get("prompt", ""),
                         "state": TaskState.RUNNING.value},
            )
            if attempt == 1:
                yield Event(
                    type=EventType.TASK_FAILED,
                    request_id=kwargs.get("request_id"),
                    payload={"task_id": task_id,
                             "error": "429 rate limited",
                             "state": TaskState.FAILED.value,
                             "retriable": True,
                             "latency_ms": 1.0},
                )
            else:
                yield Event(
                    type=EventType.TASK_COMPLETED,
                    request_id=kwargs.get("request_id"),
                    payload={"task_id": task_id,
                             "state": TaskState.SUCCEEDED.value,
                             "response": "done on fallback",
                             "latency_ms": 5.0},
                )

        return gen()

    return factory


class TestRetryRestampCapabilities:
    def test_retry_stamp_reflects_the_fallback_model(self, ws_env):
        fallback_entry = ModelEntry(
            provider="anthropic", id="claude-main", label="Claude Main",
            capabilities=["chat"], priority=1)
        stack = ExitStack()
        _mock_store(stack, PROVIDERS)
        stack.enter_context(patch.object(
            ws_module, "_create_provider_instance",
            return_value=SimpleNamespace()))
        stack.enter_context(patch.object(
            ws_module, "run_langgraph_task", _two_attempt_graph()))
        stack.enter_context(patch.object(
            fallback_module, "get_configured_fallback_candidates",
            return_value=[fallback_entry]))
        sid = session_store.create_session("E5-F12 restamp")
        with (
            stack,
            _connect() as client,
            client.websocket_connect(
                "/ws/tasks",
                headers={"Authorization": f"Bearer {engine_state.auth_token}"},
            ) as ws,
        ):
            ws.receive_text()  # HELLO
            ws.send_text(Event(
                type=EventType.START_TASK,
                payload={"prompt": "Fix the bug in the parser and run the tests",
                         "session_id": sid},
            ).to_json())
            created = []
            for _ in range(30):
                ev = Event.from_json(ws.receive_text())
                if ev.type == EventType.TASK_CREATED:
                    created.append(ev)
                if ev.type == EventType.TASK_COMPLETED:
                    break
            else:
                raise AssertionError("task never completed")

        assert len(created) == 2, "expected the initial stamp + retry restamp"
        first = created[0].payload["routing"]
        assert first["provider"] == "google_gemini"
        assert first["model"] == "gemini-main"
        assert first["capabilities"] == ["chat", "code", "tools"]

        second = created[1].payload["routing"]
        assert second["provider"] == "anthropic"
        assert second["model"] == "claude-main"
        assert second["reason"].startswith("Fallback:")
        # The defect: this restamp kept ["chat", "code", "tools"] —
        # the primary's capabilities — for a chat-only fallback model.
        assert second["capabilities"] == ["chat"]


# ---------------------------------------------------------------------------
# E5-F14 — targeted refresh applies + persists for saved providers
# ---------------------------------------------------------------------------


class TestTargetedRefreshPersists:
    def _drive_refresh(self, ws_env, target, payload_extra=None):
        stack = ExitStack()
        _mock_store(stack, PROVIDERS)
        stack.enter_context(patch.object(
            ws_module, "fetch_available_models",
            AsyncMock(return_value=DiscoveryResult(
                models=["new-model"], excluded=[], error=None))))
        with (
            stack,
            _connect() as client,
            client.websocket_connect(
                "/ws/tasks",
                headers={"Authorization": f"Bearer {engine_state.auth_token}"},
            ) as ws,
        ):
            ws.receive_text()  # HELLO
            payload = {"provider": target}
            payload.update(payload_extra or {})
            ws.send_text(Event(
                type=EventType.REFRESH_MODELS_REQUEST, payload=payload,
            ).to_json())
            for _ in range(30):
                ev = Event.from_json(ws.receive_text())
                if ev.type == EventType.REFRESH_MODELS_RESPONSE:
                    return ev
            raise AssertionError("no REFRESH_MODELS_RESPONSE received")

    def test_targeted_refresh_of_saved_provider_applies_and_persists(self, ws_env):
        cfg = ws_env["config"]
        cfg.provider_models.update({
            "google_gemini": ["gemini-main"],
            "openai": ["old-model"],
            "anthropic": ["claude-main"],
        })
        save_config(cfg, ws_env["config_path"])

        ev = self._drive_refresh(ws_env, "openai")
        shown = {p["name"]: p for p in ev.payload["providers"]}
        # The response showed the fresh catalog even before the fix.
        assert shown["openai"]["models"] == ["new-model"]

        # The defect: config, registry and disk all kept the old one.
        # (Asserted on the engine's LIVE config object — the app may
        # reload user_config from disk at connect, replacing the
        # fixture's object; engine_state.user_config is authoritative.)
        assert engine_state.user_config.provider_models["openai"] == ["new-model"]
        registry_ids = [
            m.id for m in engine_state.model_registry.get_models_for_provider("openai")]
        assert registry_ids == ["new-model"]
        on_disk = json.loads(Path(ws_env["config_path"]).read_text())
        assert on_disk["provider_models"]["openai"] == ["new-model"]

    def test_targeted_refresh_of_unsaved_template_persists_nothing(self, ws_env):
        """Control: the finding does not ask to persist unsaved
        templates — a targeted refresh naming a provider that was
        never saved must leave config untouched."""
        cfg = ws_env["config"]
        cfg.provider_models.update({
            "google_gemini": ["gemini-main"],
            "openai": ["gpt-main"],
            "anthropic": ["claude-main"],
        })
        save_config(cfg, ws_env["config_path"])

        self._drive_refresh(
            ws_env, "acme_template", {"api_key": "payload-key"})
        assert "acme_template" not in engine_state.user_config.provider_models
        on_disk = json.loads(Path(ws_env["config_path"]).read_text())
        assert "acme_template" not in on_disk.get("provider_models", {})


# ---------------------------------------------------------------------------
# E5-F15 — unprobed providers are Standby, not minted "healthy"
# ---------------------------------------------------------------------------


class TestStandbyHealthState:
    def test_fresh_state_is_standby_not_healthy(self):
        state = ProviderHealthState(provider="never-probed")
        assert state.status == "standby"
        manager = ProviderHealthManager()
        assert manager.get_state("never-probed").status == "standby"

    def test_standby_provider_stays_routable(self):
        """Eligibility is not display state (E5-F08 separation): an
        unprobed provider must remain available to routing."""
        manager = ProviderHealthManager()
        assert manager.is_provider_available("never-probed") is True

    def test_recorded_outcomes_still_drive_the_real_states(self):
        manager = ProviderHealthManager()
        manager.record_success("p", latency_ms=42.0)
        assert manager.get_state("p").status == "healthy"
        manager.record_failure("p", "boom", is_retriable=True)
        assert manager.get_state("p").status == "degraded"

    def test_reset_returns_to_standby(self):
        manager = ProviderHealthManager()
        manager.record_success("p", latency_ms=42.0)
        assert manager.get_state("p").status == "healthy"
        manager.reset_provider("p")
        assert manager.get_state("p").status == "standby"

    def test_provider_list_reports_standby_for_unconfigured_cards(
            self, ws_env, monkeypatch):
        from aether_engine.orchestration.core import _build_provider_list

        store = SimpleNamespace(list_providers=list)
        cards = _build_provider_list(store)
        assert cards, "expected provider cards"
        for card in cards:
            assert card["has_key"] is False
            assert card["health"] == "standby"


# ---------------------------------------------------------------------------
# E5-F16 / F17 — pull destination + pull timeout
# ---------------------------------------------------------------------------

MODEL = "synthetic-model:latest"
_LAYER_DIGEST = "sha256:" + "ab" * 32
_CONFIG_DIGEST = "sha256:" + "cd" * 32


class _FakeResponse:
    def __init__(self, status_code=200, lines=None, text=""):
        self.status_code = status_code
        self._lines = lines or []
        self.text = text

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return self.text.encode()


class _StreamContext:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    async def __aenter__(self):
        if self._exc is not None:
            raise self._exc
        return self._response

    async def __aexit__(self, *exc_info):
        return False


class RecordingClient:
    """httpx.AsyncClient stand-in capturing construction kwargs and
    the pull request body (F16/F17 seams)."""

    instances: ClassVar[list] = []
    pull_requests: ClassVar[list] = []
    stream_lines: ClassVar[list] = []
    stream_exc: ClassVar[Exception | None] = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        RecordingClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url):
        return _FakeResponse(status_code=200, text="Ollama is running")

    async def delete(self, url, json=None):
        return _FakeResponse(status_code=200)

    def stream(self, method, url, json=None):
        RecordingClient.pull_requests.append(json)
        if RecordingClient.stream_exc is not None:
            return _StreamContext(exc=RecordingClient.stream_exc)
        return _StreamContext(response=_FakeResponse(
            status_code=200, lines=list(RecordingClient.stream_lines)))


@pytest.fixture
def recording_httpx(monkeypatch):
    RecordingClient.instances = []
    RecordingClient.pull_requests = []
    RecordingClient.stream_lines = [
        json.dumps({"status": "pulling manifest"}),
        json.dumps({"status": "downloading", "total": 100, "completed": 100}),
        json.dumps({"status": "success"}),
    ]
    RecordingClient.stream_exc = None
    monkeypatch.setattr(local_models.httpx, "AsyncClient", RecordingClient)
    return RecordingClient


def _seed_daemon_store(store: Path) -> dict:
    """A minimal Ollama store holding MODEL: one manifest + the two
    blobs it references (config + one layer)."""
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.ollama.image.config",
                   "digest": _CONFIG_DIGEST, "size": 3},
        "layers": [{"mediaType": "application/vnd.ollama.image.model",
                    "digest": _LAYER_DIGEST, "size": 5}],
    }
    manifest_rel = Path("manifests/registry.ollama.ai/library/synthetic-model/latest")
    manifest_path = store / manifest_rel
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest))
    blobs = store / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    (blobs / _CONFIG_DIGEST.replace(":", "-")).write_bytes(b"cfg")
    (blobs / _LAYER_DIGEST.replace(":", "-")).write_bytes(b"layer")
    return {"manifest_rel": manifest_rel, "manifest": manifest}


async def _collect_download(mgr, dest):
    events = []
    async for ev in mgr.download_model_task(
        model_name=MODEL, destination_dir=dest,
        task_id="dl-1", request_id="req-1",
    ):
        events.append(ev)
    return events


class TestPullDestination:
    @pytest.mark.asyncio
    async def test_pull_places_model_files_in_chosen_destination(
            self, recording_httpx, tmp_path):
        store = tmp_path / "daemon-store"
        seeded = _seed_daemon_store(store)
        mgr = OllamaManager()
        mgr.models_dir = store
        dest = tmp_path / "chosen-models"

        events = await _collect_download(mgr, str(dest))
        types = [e.type for e in events]
        assert EventType.TASK_COMPLETED in types
        assert EventType.TASK_FAILED not in types

        # The model actually arrived: manifest + both blobs, store
        # layout mirrored under the chosen directory.
        placed_manifest = dest / seeded["manifest_rel"]
        assert placed_manifest.exists()
        assert json.loads(placed_manifest.read_text()) == seeded["manifest"]
        assert (dest / "blobs" / _CONFIG_DIGEST.replace(":", "-")).read_bytes() == b"cfg"
        assert (dest / "blobs" / _LAYER_DIGEST.replace(":", "-")).read_bytes() == b"layer"
        # The recorded path keeps its established shape — now truthful.
        assert mgr._downloaded_paths[MODEL] == str(dest / MODEL)

    @pytest.mark.asyncio
    async def test_pull_to_custom_destination_fails_honestly_when_store_lacks_model(
            self, recording_httpx, tmp_path):
        store = tmp_path / "empty-store"
        store.mkdir()
        mgr = OllamaManager()
        mgr.models_dir = store
        dest = tmp_path / "chosen-models"

        events = await _collect_download(mgr, str(dest))
        types = [e.type for e in events]
        # The daemon claimed success but holds nothing to place:
        # no fictional completion, no recorded path.
        assert EventType.TASK_COMPLETED not in types
        failed = [e for e in events if e.type == EventType.TASK_FAILED]
        assert len(failed) == 1
        assert MODEL not in mgr._downloaded_paths

    @pytest.mark.asyncio
    async def test_pull_into_daemon_store_itself_needs_no_relocation(
            self, recording_httpx, tmp_path):
        """Control: the default destination IS the daemon store —
        the pull already lands there."""
        store = tmp_path / "daemon-store"
        _seed_daemon_store(store)
        mgr = OllamaManager()
        mgr.models_dir = store

        events = await _collect_download(mgr, str(store))
        types = [e.type for e in events]
        assert EventType.TASK_COMPLETED in types
        assert mgr._downloaded_paths[MODEL] == str(store / MODEL)


class TestPullTimeout:
    @pytest.mark.asyncio
    async def test_pull_client_timeout_is_finite_and_phase_bounded(
            self, recording_httpx, tmp_path):
        store = tmp_path / "daemon-store"
        _seed_daemon_store(store)
        mgr = OllamaManager()
        mgr.models_dir = store
        events = await _collect_download(mgr, str(store))
        assert EventType.TASK_COMPLETED in [e.type for e in events]

        timeouts = [inst.kwargs.get("timeout") for inst in RecordingClient.instances]
        pull_timeouts = [t for t in timeouts if isinstance(t, httpx.Timeout)]
        assert pull_timeouts, f"no phase-bounded timeout used: {timeouts!r}"
        for t in pull_timeouts:
            assert t.connect is not None and t.connect < float("inf")
            assert t.read is not None and t.read < float("inf")
            assert t.write is not None and t.write < float("inf")
            assert t.pool is not None and t.pool < float("inf")
        # And no construction in the flow may pass timeout=None.
        assert all(t is not None for t in timeouts), (
            f"a client was constructed with timeout=None: {timeouts!r}")

    @pytest.mark.asyncio
    async def test_silent_daemon_surfaces_as_failed_pull_not_hang(
            self, recording_httpx, tmp_path):
        """Control for the failure shape: a read timeout mid-stream
        is converted to TASK_FAILED by the existing failure path."""
        RecordingClient.stream_exc = httpx.ReadTimeout(
            "read timed out", request=httpx.Request("POST", "http://x"))
        mgr = OllamaManager()
        mgr.models_dir = tmp_path / "store"
        events = await _collect_download(mgr, str(tmp_path / "dest"))
        types = [e.type for e in events]
        assert EventType.TASK_COMPLETED not in types
        assert EventType.TASK_FAILED in types
        assert MODEL not in mgr._downloaded_paths


# ---------------------------------------------------------------------------
# E5-F18 — diagnostics must verify the Tavily key, not its presence
# ---------------------------------------------------------------------------


def _urlopen_router(tavily_behaviour):
    def fake_urlopen(req, timeout=None):
        url = getattr(req, "full_url", str(req))
        if "tavily" in url:
            if isinstance(tavily_behaviour, Exception):
                raise tavily_behaviour
            response = MagicMock()
            response.status = tavily_behaviour
            response.__enter__.return_value = response
            return response
        raise urllib.error.URLError("refused")
    return fake_urlopen


def _web_search_entry(monkeypatch, tavily_behaviour):
    monkeypatch.setenv("TAVILY_API_KEY", "not-a-real-key")
    with patch.object(diagnostics.urllib.request, "urlopen",
                      _urlopen_router(tavily_behaviour)):
        logs = diagnostics.run_environment_diagnostics()
    return {e["target_name"]: e for e in logs}["Web Search API"]


class TestTavilyKeyDiagnostics:
    def test_verified_only_when_the_probe_answers_200(self, monkeypatch):
        entry = _web_search_entry(monkeypatch, 200)
        assert entry["status"] == "OK"
        assert entry["details"] == "Key verified"

    def test_rejected_key_is_failed_not_verified(self, monkeypatch):
        entry = _web_search_entry(monkeypatch, urllib.error.HTTPError(
            "https://api.tavily.com/usage", 401, "Unauthorized", {}, None))
        assert entry["status"] == "FAILED"
        assert entry["details"] != "Key verified"
        assert "rejected" in entry["details"].lower()

    def test_unreachable_probe_is_present_but_unverified(self, monkeypatch):
        entry = _web_search_entry(
            monkeypatch, urllib.error.URLError("network unreachable"))
        assert entry["status"] == "WARN"
        assert entry["details"] != "Key verified"
        assert "could not be verified" in entry["details"].lower()


# ---------------------------------------------------------------------------
# E5-F19 — auto aux resolution prefers an available tiny local model
# ---------------------------------------------------------------------------


def _aux_registry(with_local=True, local_caps=("chat", "fast")) -> ModelRegistry:
    models = [
        ModelEntry(provider="openai", id="gpt-main", label="GPT Main",
                   capabilities=["chat"], priority=1),
    ]
    if with_local:
        models.append(ModelEntry(
            provider="ollama", id="phi3:mini", label="Phi3 Mini",
            capabilities=list(local_caps), priority=1))
    return ModelRegistry(models)


@pytest.fixture
def aux_env(monkeypatch):
    calls = []

    def fake_factory(provider_name, model, secret_store, **kwargs):
        calls.append({"provider": provider_name, "model": model, **kwargs})
        return SimpleNamespace(provider=provider_name, model=model)

    monkeypatch.setattr(core_module, "_create_provider_instance", fake_factory)
    return calls


class TestTinyLocalPreference:
    def test_title_auto_prefers_tiny_local(self, aux_env, monkeypatch):
        monkeypatch.setattr(engine_state, "model_registry", _aux_registry())
        fallback = SimpleNamespace(provider="openai", model="gpt-main")
        result = resolve_title_provider(UserConfig(), object(), fallback)
        assert result is not fallback
        assert (result.provider, result.model) == ("ollama", "phi3:mini")

    def test_summariser_auto_prefers_tiny_local(self, aux_env, monkeypatch):
        monkeypatch.setattr(engine_state, "model_registry", _aux_registry())
        fallback = SimpleNamespace(provider="openai", model="gpt-main")
        result = resolve_summariser_provider(UserConfig(), object(), fallback)
        assert result is not fallback
        assert (result.provider, result.model) == ("ollama", "phi3:mini")

    def test_explicit_override_still_wins_over_local(self, aux_env, monkeypatch):
        monkeypatch.setattr(engine_state, "model_registry", _aux_registry())
        cfg = UserConfig(auxiliary_models={"title": "openai:::gpt-main"})
        fallback = SimpleNamespace(provider="google_gemini", model="gemini-main")
        result = resolve_title_provider(cfg, object(), fallback)
        assert (result.provider, result.model) == ("openai", "gpt-main")

    def test_no_local_model_falls_back(self, aux_env, monkeypatch):
        monkeypatch.setattr(
            engine_state, "model_registry", _aux_registry(with_local=False))
        fallback = SimpleNamespace(provider="openai", model="gpt-main")
        assert resolve_title_provider(UserConfig(), object(), fallback) is fallback
        assert resolve_summariser_provider(
            UserConfig(), object(), fallback) is fallback

    def test_large_local_model_is_not_tiny(self, aux_env, monkeypatch):
        """A local model without the registry's fast/tiny class does
        not qualify — housekeeping stays on the task provider."""
        monkeypatch.setattr(engine_state, "model_registry",
                            _aux_registry(local_caps=("chat",)))
        fallback = SimpleNamespace(provider="openai", model="gpt-main")
        assert resolve_title_provider(UserConfig(), object(), fallback) is fallback

    def test_no_secret_store_falls_back(self, aux_env, monkeypatch):
        monkeypatch.setattr(engine_state, "model_registry", _aux_registry())
        fallback = SimpleNamespace(provider="openai", model="gpt-main")
        assert resolve_title_provider(UserConfig(), None, fallback) is fallback
        assert resolve_summariser_provider(
            UserConfig(), None, fallback) is fallback

    def test_unbuildable_local_model_falls_back(self, aux_env, monkeypatch):
        monkeypatch.setattr(engine_state, "model_registry", _aux_registry())

        def broken_factory(*args, **kwargs):
            raise RuntimeError("ollama down")

        monkeypatch.setattr(
            core_module, "_create_provider_instance", broken_factory)
        fallback = SimpleNamespace(provider="openai", model="gpt-main")
        assert resolve_title_provider(UserConfig(), object(), fallback) is fallback
        assert resolve_summariser_provider(
            UserConfig(), object(), fallback) is fallback
