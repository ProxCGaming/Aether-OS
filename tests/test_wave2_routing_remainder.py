"""Wave 2 (Builder I) — routing/orchestration remainder.

- E5-H [S4] Routing tie-break does one sync DB query per candidate
  per selection — ``routing/policy.py:156`` calls
  ``LatencyStore.ttft_p50_ms`` per candidate, each a fresh connect.
- E5-L [S4] Startup validation can certify an offline provider as
  "consistent" — ``routing/startup.py`` health branch falls through
  when no healthy alternative exists.
- N-14 [S2] One unkeyed fallback candidate truncates the whole
  cross-provider chain — ``orchestration/core.py:469-511``.
- N-23 [S3] Shared provider validation is unshielded: one waiter's
  cancellation kills it for all — ``orchestration/core.py:298-319``.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from aether_engine.app import engine_state
from aether_engine.config import UserConfig
from aether_engine.orchestration import core
from aether_engine.orchestration.core import _create_provider_instance
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.latency import LatencyStore
from aether_engine.routing.policy import RoutingPolicy
from aether_engine.routing.registry import ModelEntry, ModelRegistry
from aether_engine.routing.startup import validate_startup_configuration
from aether_engine.secrets.storage import ProviderNotFoundError, SecretStore

# ---------------------------------------------------------------------------
# E5-H — one connection per selection, not one per candidate
# ---------------------------------------------------------------------------


def _seed(store, provider, model, ttfts):
    for i, ttft in enumerate(ttfts):
        store.record_sample(
            provider_name=provider, model_name=model,
            ttft_ms=float(ttft), total_ms=float(ttft) + 10.0,
            timestamp=1_000.0 + i)


def test_e5h_pick_reads_all_candidates_over_one_connection(
        tmp_path, monkeypatch):
    store = LatencyStore(tmp_path / "latency.db")
    _seed(store, "openai", "slow-model", [500, 520, 480])
    _seed(store, "openai", "fast-model", [50, 60, 40])
    _seed(store, "openai", "mid-model", [200, 210, 190])
    candidates = [
        ModelEntry(provider="openai", id="slow-model", label="Slow",
                   capabilities=["chat"], priority=1),
        ModelEntry(provider="openai", id="fast-model", label="Fast",
                   capabilities=["chat"], priority=1),
        ModelEntry(provider="openai", id="mid-model", label="Mid",
                   capabilities=["chat"], priority=1),
    ]
    policy = RoutingPolicy(registry=ModelRegistry(candidates),
                           latency_store=store)

    connections = 0
    original = LatencyStore._get_conn

    def counting_conn(self):
        nonlocal connections
        connections += 1
        return original(self)

    monkeypatch.setattr(LatencyStore, "_get_conn", counting_conn)
    chosen, latency_decided = policy._pick(candidates)

    assert connections == 1
    # Behaviour guard: the fastest measured model still wins its tier.
    assert chosen.id == "fast-model"
    assert latency_decided is True


# ---------------------------------------------------------------------------
# E5-L — an offline default with no healthy alternative is not
# "everything is consistent"
# ---------------------------------------------------------------------------


@pytest.fixture
def secret_store(tmp_path):
    return SecretStore(db_path=tmp_path / "secrets.db")


@pytest.fixture
def fake_keyring():
    from unittest.mock import patch

    storage = {}

    def set_password(service, username, password):
        storage[(service, username)] = password

    def get_password(service, username):
        return storage.get((service, username))

    def delete_password(service, username):
        storage.pop((service, username), None)

    with (
        patch("keyring.set_password", side_effect=set_password),
        patch("keyring.get_password", side_effect=get_password),
        patch("keyring.delete_password", side_effect=delete_password),
        patch("sys.platform", "linux"),
    ):
        yield storage


def _startup_registry():
    return ModelRegistry([
        ModelEntry(provider="google_gemini", id="gemini-2.5-flash",
                   label="Gemini Flash", priority=1),
        ModelEntry(provider="openai", id="gpt-4o-mini",
                   label="GPT-4o Mini", priority=1),
    ])


def test_e5l_offline_default_without_alternative_is_not_consistent(
        fake_keyring, secret_store):
    secret_store.save_provider("google_gemini", "gemini-key")
    secret_store.save_provider("openai", "openai-key")
    health_mgr = ProviderHealthManager()
    health_mgr.record_failure("google_gemini", "Endpoint down",
                              is_retriable=False)
    health_mgr.record_failure("openai", "Endpoint down",
                              is_retriable=False)

    cfg = UserConfig(default_provider="google_gemini",
                     default_model="gemini-2.5-flash")
    result = validate_startup_configuration(
        user_config=cfg,
        secret_store=secret_store,
        health_manager=health_mgr,
        registry=_startup_registry(),
    )

    # The desired provider stays active (there is nothing healthier
    # to select) but the result must say it is offline — the
    # was_fallback=False / reason=None "consistent" shape is a lie.
    assert result.active_provider == "google_gemini"
    assert result.was_fallback is True
    assert result.fallback_reason is not None
    assert "offline" in result.fallback_reason


# ---------------------------------------------------------------------------
# N-14 — an unkeyed chain entry is skipped, not chain-fatal
# ---------------------------------------------------------------------------


def _factory_registry():
    return ModelRegistry([
        ModelEntry(provider="google_gemini", id="gemini-main",
                   label="Gemini Main", capabilities=["chat"], priority=1),
        ModelEntry(provider="google_gemini", id="gemini-alt",
                   label="Gemini Alt", capabilities=["chat"], priority=2),
        ModelEntry(provider="openai", id="gpt-main", label="GPT Main",
                   capabilities=["chat"], priority=1),
        ModelEntry(provider="anthropic", id="claude-main",
                   label="Claude Main", capabilities=["chat"], priority=1),
    ])


@pytest.fixture
def factory_env(monkeypatch):
    cfg = UserConfig(default_provider="google_gemini",
                     default_model="gemini-main")
    monkeypatch.setattr(engine_state, "user_config", cfg)
    monkeypatch.setattr(engine_state, "model_registry", _factory_registry())
    monkeypatch.setattr(engine_state, "health_manager",
                        ProviderHealthManager())
    monkeypatch.setattr(
        engine_state, "configured_providers",
        ["google_gemini", "openai", "anthropic"])
    return cfg


def test_n14_unkeyed_chain_entry_does_not_truncate_the_chain(factory_env):
    cfg = factory_env
    cfg.fallback_chain = ["anthropic", "openai"]
    store = MagicMock()

    def load_provider(name):
        if name == "anthropic":
            raise ProviderNotFoundError("anthropic")
        return "fake-key"

    store.load_provider.side_effect = load_provider
    inst = _create_provider_instance("google_gemini", "gemini-main", store)

    cross = [fm.provider_name for fm in inst.fallback_models
             if fm.provider_name != "google_gemini"]
    # anthropic (unkeyed) is skipped; openai — later in the chain —
    # is still computed, and the same-provider fallback survives.
    assert cross == ["openai"]
    assert any(fm.provider_name == "google_gemini"
               for fm in inst.fallback_models)


# ---------------------------------------------------------------------------
# N-23 — shared validation survives one waiter's cancellation
# ---------------------------------------------------------------------------


async def test_n23_cancelling_one_waiter_leaves_validation_running(
        monkeypatch):
    calls = 0
    gate = asyncio.Event()

    async def fake_validate(provider_name, api_key, base_url=None,
                            persist_models=False):
        nonlocal calls
        calls += 1
        await gate.wait()
        return {"status": "ok", "message": "", "models": [],
                "excluded_models": [], "latency_ms": 1.0}

    monkeypatch.setattr(core, "_validate_provider_key", fake_validate)
    core._inflight_validations.clear()

    waiter1 = asyncio.create_task(
        core.validate_provider_key_shared("prov-n23", "key-n23"))
    waiter2 = asyncio.create_task(
        core.validate_provider_key_shared("prov-n23", "key-n23"))
    await asyncio.sleep(0.05)
    assert calls == 1  # deduped onto ONE shared validation

    waiter1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter1

    gate.set()
    result = await waiter2
    assert result["status"] == "ok"
    assert calls == 1  # the shared run was never cancelled or re-run
