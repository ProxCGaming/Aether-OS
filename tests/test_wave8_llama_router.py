"""llama.cpp router backend: config, validation, manager parsing,
tier policy, supervisor control and provider-list integration.

Everything here runs without a real llama.cpp: the router is an
httpx MockTransport script, the supervisor's spawn seam is faked,
and the absent-router cases point at a port nothing listens on.
No test spawns a real process, and no real API key appears
anywhere — the key-plumbing test uses an obvious sentinel and
asserts it reaches only the Authorization header.
"""
from __future__ import annotations

import time

import httpx
import pytest

from aether_engine.config import DEFAULT_LLAMACPP_ROUTER_URL, UserConfig
from aether_engine.config_updates import (
    HANDED_OFF_FIELDS,
    validate_handed_off_updates,
)
from aether_engine.models import llama_router
from aether_engine.models.llama_presets import (
    DAILY_MODEL_IDLE_TTL_S,
    plan_tier_actions,
    preset_rows,
)
from aether_engine.models.llama_router import (
    LLAMACPP_NOT_INSTALLED_ERROR,
    LlamaRouterManager,
)

GIB = 1024 ** 3


# ------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------


class FakeSecretStore:
    """The SecretStore surface the manager + discovery read."""

    def __init__(self, keys: dict | None = None):
        self._keys = dict(keys or {})

    def load_provider(self, provider: str) -> str:
        if provider not in self._keys:
            from aether_engine.secrets.storage import ProviderNotFoundError

            raise ProviderNotFoundError(provider)
        return self._keys[provider]

    def list_providers(self) -> list[str]:
        return sorted(self._keys)


class FakeRouter:
    """A scripted llama.cpp router behind httpx.MockTransport.

    ``up`` flips /health + /models between answering and 503s the
    way a loading router does — transport failures (router simply
    absent) are exercised separately against a dead port.
    """

    def __init__(self, entries: list | None = None):
        self.up = True
        self.entries = entries if entries is not None else []
        self.models_status = 200
        self.load_status = 200
        self.load_body: dict = {"success": True}
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
            "authorization": request.headers.get("authorization"),
        })
        path = request.url.path
        if path == "/health":
            return httpx.Response(200 if self.up else 503,
                                  json={"status": "ok"})
        if path == "/props":
            return httpx.Response(200, json={"version": "test-build-1"})
        if path == "/models":
            if not self.up:
                return httpx.Response(503, json={})
            if self.models_status != 200:
                return httpx.Response(self.models_status, json={})
            return httpx.Response(200, json={"data": self.entries})
        if path == "/models/load":
            return httpx.Response(self.load_status, json=self.load_body)
        if path == "/models/unload":
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
        lambda: {"machine": {"ram": {"total_bytes": 16 * GIB}, "gpu": {}}})
    return router


def _manager(**kwargs) -> LlamaRouterManager:
    kwargs.setdefault("router_url", "http://127.0.0.1:9931")
    return LlamaRouterManager(**kwargs)


# ------------------------------------------------------------------
# Config + strict validation
# ------------------------------------------------------------------


def test_config_defaults():
    cfg = UserConfig()
    assert cfg.llamacpp_enabled is False
    assert cfg.llamacpp_router_url == DEFAULT_LLAMACPP_ROUTER_URL
    assert cfg.llamacpp_router_url == "http://127.0.0.1:9931"
    assert cfg.llamacpp_binary_path == ""
    assert cfg.llamacpp_models_dir == ""


def test_config_round_trip():
    cfg = UserConfig(
        llamacpp_enabled=True,
        llamacpp_router_url="http://127.0.0.1:8080",
        llamacpp_binary_path="/opt/llama/llama-server",
        llamacpp_models_dir="/models",
    )
    back = UserConfig.from_dict(cfg.to_dict())
    assert back.llamacpp_enabled is True
    assert back.llamacpp_router_url == "http://127.0.0.1:8080"
    assert back.llamacpp_binary_path == "/opt/llama/llama-server"
    assert back.llamacpp_models_dir == "/models"


def test_config_load_cleaning():
    cfg = UserConfig.from_dict({
        "llamacpp_router_url": 42,
        "llamacpp_binary_path": None,
        "llamacpp_models_dir": ["/x"],
    })
    assert cfg.llamacpp_router_url == DEFAULT_LLAMACPP_ROUTER_URL
    assert cfg.llamacpp_binary_path == ""
    assert cfg.llamacpp_models_dir == ""
    cfg = UserConfig.from_dict(
        {"llamacpp_router_url": "  http://127.0.0.1:9931/  "})
    assert cfg.llamacpp_router_url == "http://127.0.0.1:9931"


def test_llamacpp_fields_are_handed_off():
    for field in ("llamacpp_enabled", "llamacpp_router_url",
                  "llamacpp_binary_path", "llamacpp_models_dir"):
        assert field in HANDED_OFF_FIELDS


def test_validation_accepts_and_cleans():
    cleaned = validate_handed_off_updates({
        "llamacpp_enabled": True,
        "llamacpp_router_url": "http://127.0.0.1:9931/",
        "llamacpp_binary_path": "  /opt/llama/llama-server ",
        "llamacpp_models_dir": "",
    })
    assert cleaned == {
        "llamacpp_enabled": True,
        "llamacpp_router_url": "http://127.0.0.1:9931",
        "llamacpp_binary_path": "/opt/llama/llama-server",
        "llamacpp_models_dir": "",
    }


@pytest.mark.parametrize("field,value", [
    ("llamacpp_enabled", "yes"),
    ("llamacpp_enabled", 1),
    ("llamacpp_enabled", None),
    ("llamacpp_router_url", 9931),
    ("llamacpp_router_url", ""),
    ("llamacpp_router_url", "127.0.0.1:9931"),
    ("llamacpp_router_url", "ftp://example.com/llama"),
    ("llamacpp_binary_path", 42),
    ("llamacpp_binary_path", None),
    ("llamacpp_models_dir", ["/models"]),
])
def test_validation_refuses_wrong_types_naming_the_field(field, value):
    with pytest.raises(ValueError) as excinfo:
        validate_handed_off_updates({field: value})
    assert field in str(excinfo.value)


# ------------------------------------------------------------------
# Absent router: the default on every machine tonight
# ------------------------------------------------------------------


async def test_absent_router_status_is_prompt_and_honest():
    # Port 9 (discard) on loopback: nothing listens, refused fast.
    manager = _manager(router_url="http://127.0.0.1:9")
    started = time.monotonic()
    status = await manager.router_status()
    elapsed = time.monotonic() - started
    assert status == {"running": False, "version": None,
                      "models_loaded": None, "error": None}
    assert elapsed < 5.0

    listing = await manager.list_models()
    assert listing == {"models": [], "error": None}

    payload = await manager.status_payload(enabled=False)
    assert payload["running"] is False
    assert "error" not in payload
    assert payload["supervised"] is False


async def test_absent_router_control():
    manager = _manager(router_url="http://127.0.0.1:9")
    start = await manager.router_control("start")
    assert start == {"success": False, "running": False,
                     "error": LLAMACPP_NOT_INSTALLED_ERROR}
    stop = await manager.router_control("stop")
    assert stop == {"success": True, "running": False, "error": None}
    bogus = await manager.router_control("restart")
    assert bogus["success"] is False
    assert bogus["running"] is False
    assert "unknown router action" in bogus["error"]


async def test_load_unload_absent_router():
    manager = _manager(router_url="http://127.0.0.1:9")
    load = await manager.load_model("some-model")
    assert load["success"] is False
    assert "not reachable" in load["error"]
    empty = await manager.load_model("")
    assert empty == {"success": False, "error": "no model was specified"}


# ------------------------------------------------------------------
# Router parsing (scripted transport)
# ------------------------------------------------------------------


async def test_status_parse(fake_router):
    fake_router.entries = [
        {"id": "a", "status": {"value": "loaded"}},
        {"id": "b", "status": {"value": "loaded"}},
        {"id": "c", "status": {"value": "unloaded"}},
    ]
    status = await _manager().router_status()
    assert status["running"] is True
    assert status["version"] == "test-build-1"
    assert status["models_loaded"] == 2
    assert status["error"] is None


async def test_status_up_but_models_unreadable(fake_router):
    fake_router.models_status = 500
    status = await _manager().router_status()
    assert status["running"] is True
    assert status["models_loaded"] is None
    assert status["error"] is not None
    listing = await _manager().list_models()
    assert listing["models"] == []
    assert listing["error"] is not None


async def test_models_parse_states_sizes_presets_fit(fake_router, tmp_path):
    model_file = tmp_path / "big.gguf"
    model_file.write_bytes(b"\0" * (20 * GIB // 1024))  # stat fallback below
    fake_router.entries = [
        {"id": "ggml-org/Qwen2.5-7B-Instruct-GGUF:Q4_K_M",
         "status": {"value": "loaded"}, "size": 4 * GIB,
         "some_future_field": {"nested": True}},
        {"id": "local/llama-3.1-8b-q4.gguf",
         "status": {"value": "unloaded"}, "size_bytes": 14 * GIB},
        {"id": "local/nomic-embed-text.gguf",
         "status": {"value": "sleeping"}},
        {"id": "local/mystery.gguf", "status": {"value": "loading"}},
        {"id": "local/bare-string.gguf", "status": "loaded",
         "path": str(model_file)},
        {"id": "local/no-status.gguf"},
        {"status": {"value": "loaded"}},  # no id: skipped
        "not-a-dict",
    ]
    listing = await _manager().list_models()
    assert listing["error"] is None
    rows = {r["id"]: r for r in listing["models"]}
    assert len(rows) == 6

    qwen = rows["ggml-org/Qwen2.5-7B-Instruct-GGUF:Q4_K_M"]
    assert qwen["loaded"] is True
    assert qwen["size_bytes"] == 4 * GIB
    assert qwen["fit"] == "comfortable"
    assert qwen["preset"] == "qwen-2-5-7b"
    assert qwen["tier"] == "daily"
    assert qwen["name"] == "Qwen2.5-7B-Instruct-GGUF:Q4_K_M"

    llama = rows["local/llama-3.1-8b-q4.gguf"]
    assert llama["loaded"] is False
    assert llama["fit"] == "tight"
    assert llama["preset"] == "llama-3-1-8b"

    embed = rows["local/nomic-embed-text.gguf"]
    assert embed["loaded"] is False  # sleeping = evicted, not resident
    assert embed["size_bytes"] is None
    assert embed["fit"] is None
    assert embed["tier"] == "resident"

    assert rows["local/mystery.gguf"]["loaded"] is None  # loading
    bare = rows["local/bare-string.gguf"]
    assert bare["loaded"] is True  # bare-string status form
    assert bare["size_bytes"] == 20 * GIB // 1024  # from the file stat
    assert rows["local/no-status.gguf"]["loaded"] is None


async def test_load_unload_calls(fake_router):
    manager = _manager()
    assert await manager.load_model("m1") == {"success": True, "error": None}
    assert await manager.unload_model("m1") == {"success": True, "error": None}
    assert fake_router.posts("/models/load")[0]["json"] == {"model": "m1"}
    assert fake_router.posts("/models/unload")[0]["json"] == {"model": "m1"}


async def test_load_refusal_carries_router_message(fake_router):
    fake_router.load_status = 404
    fake_router.load_body = {"error": {"message": "model not found",
                                       "type": "invalid_request_error"}}
    res = await _manager().load_model("nope")
    assert res == {"success": False, "error": "model not found"}


async def test_api_key_header_and_secrecy(fake_router):
    sentinel = "unit-test-sentinel"
    store = FakeSecretStore({"llamacpp": sentinel})
    manager = _manager(secret_store=store)
    status = await manager.router_status()
    assert fake_router.requests[0]["authorization"] == f"Bearer {sentinel}"
    # The key value appears in no returned structure.
    assert sentinel not in repr(status)
    assert sentinel not in repr(await manager.list_models())
    # No stored key -> no header, still works.
    fake_router.requests.clear()
    await _manager(secret_store=FakeSecretStore()).router_status()
    assert fake_router.requests[0]["authorization"] is None


async def test_models_payload_shape(fake_router):
    fake_router.entries = [
        {"id": "local/nomic-embed-text.gguf", "status": {"value": "loaded"}},
    ]
    payload = await _manager().models_payload()
    assert payload["models"][0]["id"] == "local/nomic-embed-text.gguf"
    presets = {p["id"]: p for p in payload["presets"]}
    assert presets["nomic-embed"]["available"] is True
    assert presets["qwen-2-5-7b"]["available"] is False
    assert "error" not in payload


# ------------------------------------------------------------------
# Tier policy (pure)
# ------------------------------------------------------------------


def _row(model_id, loaded=True, last_used=None, job_state=None):
    return {"id": model_id, "loaded": loaded,
            "last_used": last_used, "job_state": job_state}


def test_policy_resident_loads_only_on_start_and_never_unloads():
    rows = [_row("local/llama-3.2-3b-q4.gguf", loaded=False)]
    assert plan_tier_actions(rows, now=1000.0) == {"load": [], "unload": []}
    plan = plan_tier_actions(rows, now=1000.0, router_just_started=True)
    assert plan == {"load": ["local/llama-3.2-3b-q4.gguf"], "unload": []}
    loaded = [_row("local/llama-3.2-3b-q4.gguf", loaded=True,
                   last_used=0.0, job_state="done")]
    assert plan_tier_actions(loaded, now=10 ** 9) == {"load": [], "unload": []}


def test_policy_daily_ttl():
    model = "local/qwen2.5-7b-q4.gguf"
    now = 100_000.0
    stale = [_row(model, last_used=now - DAILY_MODEL_IDLE_TTL_S - 1)]
    assert plan_tier_actions(stale, now=now)["unload"] == [model]
    fresh = [_row(model, last_used=now - 10)]
    assert plan_tier_actions(fresh, now=now)["unload"] == []
    unknown = [_row(model, last_used=None)]
    assert plan_tier_actions(unknown, now=now)["unload"] == []
    busy = [_row(model, last_used=now - 99_999, job_state="active")]
    assert plan_tier_actions(busy, now=now)["unload"] == []


def test_policy_heavy_unloads_when_job_done():
    model = "local/qwen2.5-32b-q4.gguf"
    assert plan_tier_actions(
        [_row(model, job_state="done")], now=1.0)["unload"] == [model]
    assert plan_tier_actions(
        [_row(model, job_state="active")], now=1.0)["unload"] == []
    assert plan_tier_actions(
        [_row(model, job_state=None)], now=1.0)["unload"] == []


def test_policy_never_touches_unknown_or_unowned():
    rows = [
        _row("local/qwen2.5-7b-q4.gguf", loaded=None, job_state="done"),
        _row("someone-elses-model", loaded=True, job_state="done",
             last_used=0.0),
    ]
    assert plan_tier_actions(rows, now=10 ** 9) == {"load": [], "unload": []}


def test_preset_rows_availability():
    rows = {r["id"]: r for r in preset_rows(["x/nomic-embed-text.gguf"])}
    assert rows["nomic-embed"]["available"] is True
    assert rows["nomic-embed"]["tier"] == "resident"
    assert rows["llama-3-3-70b"]["available"] is False
    assert rows["llama-3-3-70b"]["tier"] == "heavy"


# ------------------------------------------------------------------
# Tier policy application (manager)
# ------------------------------------------------------------------


async def test_apply_tier_policy_unloads_stale_daily(fake_router):
    model = "local/qwen2.5-7b-q4.gguf"
    fake_router.entries = [{"id": model, "status": {"value": "loaded"}}]
    manager = _manager()
    manager._last_used[model] = time.monotonic() - DAILY_MODEL_IDLE_TTL_S - 60
    result = await manager.apply_tier_policy()
    assert result["unload"] == [model]
    assert result["errors"] == {}
    assert fake_router.posts("/models/unload")[0]["json"] == {"model": model}


async def test_note_job_completed_unloads_heavy_only(fake_router):
    heavy = "local/qwen2.5-32b-q4.gguf"
    daily = "local/qwen2.5-7b-q4.gguf"
    fake_router.entries = [
        {"id": heavy, "status": {"value": "loaded"}},
        {"id": daily, "status": {"value": "loaded"}},
    ]
    manager = _manager()
    assert (await manager.note_job_completed(daily))["success"] is True
    assert fake_router.posts("/models/unload") == []
    assert (await manager.note_job_completed(heavy))["success"] is True
    assert fake_router.posts("/models/unload")[0]["json"] == {"model": heavy}


# ------------------------------------------------------------------
# Supervisor (spawn seam faked — no real process)
# ------------------------------------------------------------------


class FakeProc:
    def __init__(self):
        self.alive = True
        self.returncode = None
        self.terminated = False

    def poll(self):
        return None if self.alive else self.returncode

    def terminate(self):
        self.terminated = True
        self.alive = False
        self.returncode = 0

    def kill(self):
        self.alive = False
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


async def test_start_refuses_without_existing_binary(tmp_path):
    manager = _manager(binary_path=str(tmp_path / "no-such-binary"))
    res = await manager.start_router()
    assert res == {"success": False, "running": False,
                   "error": LLAMACPP_NOT_INSTALLED_ERROR}


async def test_supervised_start_and_stop(fake_router, monkeypatch, tmp_path):
    url = "http://127.0.0.1:19931"
    binary = tmp_path / "llama-server"
    binary.write_bytes(b"fake")
    fake_router.up = False
    spawned = {}

    def fake_spawn(argv, env):
        proc = FakeProc()
        spawned["argv"] = argv
        spawned["env"] = env
        spawned["proc"] = proc
        fake_router.up = True  # the child brings the router up
        return proc

    monkeypatch.setattr(llama_router, "_spawn_router_process", fake_spawn)
    fake_router.entries = [
        {"id": "local/nomic-embed-text.gguf",
         "status": {"value": "unloaded"}},
    ]
    store = FakeSecretStore({"llamacpp": "unit-test-sentinel"})
    manager = LlamaRouterManager(
        router_url=url, binary_path=str(binary),
        models_dir="/models", secret_store=store)
    try:
        res = await manager.start_router()
        assert res == {"success": True, "running": True, "error": None}
        argv = spawned["argv"]
        assert argv[0] == str(binary)
        assert argv[argv.index("--port") + 1] == "19931"
        assert argv[argv.index("--models-dir") + 1] == "/models"
        # The API key rides the environment, never the command line.
        assert spawned["env"]["LLAMA_ARG_API_KEY"] == "unit-test-sentinel"
        assert "unit-test-sentinel" not in " ".join(argv)
        assert manager.supervised_running() is True
        # Resident tier was loaded on start.
        loads = fake_router.posts("/models/load")
        assert loads and loads[0]["json"] == {
            "model": "local/nomic-embed-text.gguf"}

        stop = await manager.stop_router()
        assert stop == {"success": True, "running": False, "error": None}
        assert spawned["proc"].terminated is True
        assert manager.supervised_running() is False
    finally:
        llama_router._SUPERVISED_ROUTERS.pop(url, None)


async def test_stop_refuses_external_router(fake_router):
    # Router answering (fake transport) but no supervised child.
    manager = _manager(router_url="http://127.0.0.1:19932")
    res = await manager.stop_router()
    assert res["success"] is False
    assert res["running"] is True
    assert "not started by Aether" in res["error"]


# ------------------------------------------------------------------
# Provider-list integration (discovery)
# ------------------------------------------------------------------


@pytest.fixture
def discovery_state(monkeypatch):
    from aether_engine.orchestration import discovery
    from aether_engine.state import engine_state

    saved_config = engine_state.user_config
    saved_snapshot = discovery._llamacpp_snapshot
    yield discovery, engine_state
    engine_state.user_config = saved_config
    discovery._llamacpp_snapshot = saved_snapshot


async def test_provider_absent_when_disabled_or_down(
        fake_router, discovery_state):
    discovery, engine_state = discovery_state
    store = FakeSecretStore()
    cfg = UserConfig(llamacpp_enabled=False)
    engine_state.user_config = cfg
    providers = await discovery._build_provider_list_async(store)
    assert all(p["name"] != "llamacpp" for p in providers)

    # Enabled but the router is not answering (the scripted
    # router is down on every endpoint): still absent.
    cfg.llamacpp_enabled = True
    fake_router.up = False
    providers = await discovery._build_provider_list_async(store)
    assert all(p["name"] != "llamacpp" for p in providers)

    # Enabled and answering, but with no models: absent too —
    # an empty provider entry would be a husk in the picker.
    fake_router.up = True
    fake_router.entries = []
    providers = await discovery._build_provider_list_async(store)
    assert all(p["name"] != "llamacpp" for p in providers)


async def test_provider_present_with_loaded_meta(
        fake_router, discovery_state):
    discovery, engine_state = discovery_state
    fake_router.entries = [
        {"id": "local/qwen2.5-7b-q4.gguf", "status": {"value": "loaded"}},
        {"id": "local/llama-3.1-8b-q4.gguf",
         "status": {"value": "unloaded"}},
    ]
    engine_state.user_config = UserConfig(llamacpp_enabled=True)
    store = FakeSecretStore()
    providers = await discovery._build_provider_list_async(store)
    entries = [p for p in providers if p["name"] == "llamacpp"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["models"] == [
        "local/llama-3.1-8b-q4.gguf", "local/qwen2.5-7b-q4.gguf"]
    assert entry["model_meta"]["local/qwen2.5-7b-q4.gguf"]["loaded"] is True
    assert entry["model_meta"]["local/llama-3.1-8b-q4.gguf"]["loaded"] is False
    assert entry["base_url"] == "http://127.0.0.1:9931"
    assert entry["health"] == "healthy"

    # Router goes away: the background probe clears the snapshot
    # and reports the change; the next list omits the provider.
    fake_router.entries = []
    assert await discovery.discover_pending_provider_models(store) is True
    providers = await discovery._build_provider_list_async(
        store, discover=False)
    assert all(p["name"] != "llamacpp" for p in providers)


async def test_stored_router_key_mints_no_phantom_custom_provider(
        fake_router, discovery_state):
    discovery, engine_state = discovery_state
    engine_state.user_config = UserConfig(llamacpp_enabled=False)
    store = FakeSecretStore({"llamacpp": "unit-test-sentinel"})
    providers = await discovery._build_provider_list_async(store)
    assert all(p["name"] != "llamacpp" for p in providers)


# ------------------------------------------------------------------
# WS senders (fake connection)
# ------------------------------------------------------------------


class FakeConn:
    """The WsConnection surface the senders use, with the real
    reply discipline (builder -> payload, raise -> error payload).
    """

    def __init__(self, secret_store=None):
        self.secret_store = secret_store or FakeSecretStore()
        self.sent: list[tuple] = []
        self.spawned: list = []

    async def reply(self, req_id, event_type, payload_builder, *,
                    error_payload, send_failure_message, warn=None):
        try:
            payload = await payload_builder()
        except Exception as e:  # noqa: BLE001 — mirrors WsConnection.reply
            payload = error_payload(e)
        await self.send_event(req_id, event_type, payload,
                              send_failure_message)

    async def send_event(self, req_id, event_type, payload,
                         send_failure_message):
        self.sent.append((req_id, event_type, payload))

    def spawn_background(self, coro):
        self.spawned.append(coro)


async def test_ws_status_sender_absent_router(discovery_state):
    from aether_common.contracts import EventType
    from aether_engine.routes import ws_llamacpp

    _, engine_state = discovery_state
    engine_state.user_config = UserConfig(
        llamacpp_router_url="http://127.0.0.1:9")
    conn = FakeConn()
    await ws_llamacpp.send_llamacpp_status_result(conn, "r1")
    req_id, event_type, payload = conn.sent[0]
    assert req_id == "r1"
    assert event_type is EventType.LLAMACPP_STATUS_RESPONSE
    assert payload == {
        "enabled": False, "running": False,
        "url": "http://127.0.0.1:9", "version": None,
        "models_loaded": None, "supervised": False,
    }


async def test_ws_control_sender_refuses_without_binary(discovery_state):
    from aether_common.contracts import EventType
    from aether_engine.routes import ws_llamacpp

    _, engine_state = discovery_state
    engine_state.user_config = UserConfig(
        llamacpp_router_url="http://127.0.0.1:9")
    conn = FakeConn()
    await ws_llamacpp.send_llamacpp_router_control_result(conn, "r2", "start")
    _, event_type, payload = conn.sent[0]
    assert event_type is EventType.LLAMACPP_ROUTER_CONTROL_RESPONSE
    assert payload == {"success": False, "running": False,
                       "error": LLAMACPP_NOT_INSTALLED_ERROR}


async def test_ws_handlers_spawn_background_senders():
    from aether_engine.routes import ws_llamacpp

    class Msg:
        def __init__(self):
            self.request_id = "r3"
            self.payload = {"model_id": "m", "action": "stop"}

    conn = FakeConn()
    await ws_llamacpp.handle_llamacpp_status_request(conn, Msg())
    await ws_llamacpp.handle_llamacpp_models_request(conn, Msg())
    await ws_llamacpp.handle_llamacpp_model_load_request(conn, Msg())
    await ws_llamacpp.handle_llamacpp_model_unload_request(conn, Msg())
    await ws_llamacpp.handle_llamacpp_router_control_request(conn, Msg())
    assert len(conn.spawned) == 5
    for coro in conn.spawned:
        coro.close()
