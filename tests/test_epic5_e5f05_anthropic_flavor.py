"""E5-F05 — the saved custom Anthropic flavour must govern dispatch
and discovery.

ADR 0013 gives custom providers two integration flavours,
``openai_compatible`` and ``anthropic``, and PROVIDER_SAVE retains the
choice in ``custom_provider_types``. But nothing downstream consulted
it (frozen-tree anchors):

  * construction forwarded only name/model/base URL
    (``orchestration/core.py``), and ``LiteLLMProvider`` resolved its
    model with no flavour input;
  * ``resolve_litellm_model`` mapped EVERY ``custom_*`` provider to
    the OpenAI protocol (``providers/litellm_provider.py``), so a
    custom Anthropic endpoint was dispatched as
    ``openai/claude-sonnet-4-6``;
  * discovery routed every custom name through the OpenAI-compatible
    implementation (``providers/discovery.py``), sending an
    ``Authorization`` header instead of ``x-api-key`` and parsing the
    catalog through the OpenAI branch.

E5-F04 made ``custom_provider_types`` load-bearing for keylessness
(anthropic-flavour customs require a key); this slice makes the same
record govern the protocol: one helper,
``config.custom_provider_flavor`` — recorded ``anthropic`` is
authoritative, anything else (including no record, the legacy-save
case) is ``openai_compatible`` — consulted at construction, model
resolution and discovery.

Controls pin that openai_compatible customs and the built-in
providers behave byte-identically to before.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aether_engine.config import UserConfig, custom_provider_flavor
from aether_engine.orchestration.core import (
    _build_provider_list_async,
    _create_provider_instance,
)
from aether_engine.providers.discovery import fetch_available_models
from aether_engine.providers.litellm_provider import (
    LiteLLMProvider,
    resolve_litellm_model,
)
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.registry import ModelEntry, ModelRegistry
from aether_engine.secrets.dpapi import BaseProtector, SecretDecryptionError
from aether_engine.secrets.storage import SecretStore
from aether_engine.state import engine_state

ANTH = "custom_anthropic"
ANTH_MODEL = "claude-sonnet-4-6"
ANTH_URL = "https://proxy.example.com"
LOCAL = "custom_local"
LOCAL_MODEL = "llama3:8b"
LOCAL_URL = "http://127.0.0.1:8080/v1"


class DummyProtector(BaseProtector):
    """The test_secret_store.py seam: reversible, no OS keyring."""

    def protect(self, data: bytes) -> bytes:
        return b"ENC:" + data

    def unprotect(self, data: bytes) -> bytes:
        if not data.startswith(b"ENC:"):
            raise SecretDecryptionError("Invalid header")
        return data[4:]


@pytest.fixture
def store(tmp_path):
    return SecretStore(db_path=tmp_path / "secrets.db", protector=DummyProtector())


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    """This VM has malformed proxy env vars that break httpx client
    construction (house pattern from test_model_discovery.py)."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy",
                "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)


def _registry() -> ModelRegistry:
    return ModelRegistry([
        ModelEntry(provider="google_gemini", id="gemini-main", label="Gemini Main",
                   capabilities=["chat"], priority=1),
        ModelEntry(provider=ANTH, id=ANTH_MODEL, label="Claude Sonnet 4.6 (custom)",
                   capabilities=["chat"], priority=1),
        ModelEntry(provider=LOCAL, id=LOCAL_MODEL, label="Llama 3 8B (local)",
                   capabilities=["chat"], priority=1),
    ])


def _cfg(**overrides) -> UserConfig:
    base = {
        "default_provider": ANTH,
        "default_model": ANTH_MODEL,
        "custom_base_urls": {ANTH: ANTH_URL, LOCAL: LOCAL_URL},
        "provider_models": {ANTH: [ANTH_MODEL], LOCAL: [LOCAL_MODEL]},
        "custom_provider_types": {ANTH: "anthropic", LOCAL: "openai_compatible"},
    }
    base.update(overrides)
    return UserConfig(**base)


# ---------------------------------------------------------------------------
# The flavour lookup: one helper, stated default
# ---------------------------------------------------------------------------

class TestFlavorHelper:
    def test_recorded_anthropic_is_authoritative(self):
        assert custom_provider_flavor(ANTH, _cfg()) == "anthropic"

    def test_recorded_openai_compatible(self):
        assert custom_provider_flavor(LOCAL, _cfg()) == "openai_compatible"

    def test_no_record_defaults_to_openai_compatible(self):
        """Legacy saves predate custom_provider_types: no record must
        mean the historical default, not a guess at anthropic."""
        cfg = _cfg(custom_provider_types={})
        assert custom_provider_flavor(ANTH, cfg) == "openai_compatible"
        assert custom_provider_flavor("custom_openai", cfg) == "openai_compatible"

    def test_unknown_recorded_value_defaults_to_openai_compatible(self):
        cfg = _cfg(custom_provider_types={ANTH: "something_else"})
        assert custom_provider_flavor(ANTH, cfg) == "openai_compatible"


# ---------------------------------------------------------------------------
# Seam (a): model resolution
# ---------------------------------------------------------------------------

class TestResolution:
    def test_anthropic_flavor_custom_resolves_anthropic_form(self):
        assert resolve_litellm_model(
            ANTH_MODEL, ANTH, provider_flavor="anthropic",
        ) == f"anthropic/{ANTH_MODEL}"

    def test_openai_flavor_custom_resolves_openai_form(self):
        """Control: the openai_compatible path is byte-identical."""
        assert resolve_litellm_model(
            LOCAL_MODEL, LOCAL, provider_flavor="openai_compatible",
        ) == f"openai/{LOCAL_MODEL}"

    def test_no_flavor_keeps_legacy_custom_default(self):
        """Control: callers that pass no flavour (the pre-fix world)
        resolve customs exactly as before."""
        assert resolve_litellm_model(ANTH_MODEL, ANTH) == f"openai/{ANTH_MODEL}"

    def test_builtin_anthropic_unchanged(self):
        """Control: the built-in anthropic provider never needed the
        flavour and must not be affected by it."""
        assert resolve_litellm_model(ANTH_MODEL, "anthropic") == f"anthropic/{ANTH_MODEL}"
        assert resolve_litellm_model(
            ANTH_MODEL, "anthropic", provider_flavor="openai_compatible",
        ) == f"anthropic/{ANTH_MODEL}"

    def test_anthropic_flavor_keeps_existing_anthropic_prefix(self):
        assert resolve_litellm_model(
            f"anthropic/{ANTH_MODEL}", ANTH, provider_flavor="anthropic",
        ) == f"anthropic/{ANTH_MODEL}"


# ---------------------------------------------------------------------------
# Seam (b): construction through the real factory (the audit's repro:
# configuring custom_anthropic: anthropic and constructing
# claude-sonnet-4-6 yielded openai/claude-sonnet-4-6)
# ---------------------------------------------------------------------------

@pytest.fixture
def factory_env(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr(engine_state, "user_config", cfg)
    monkeypatch.setattr(engine_state, "model_registry", _registry())
    monkeypatch.setattr(engine_state, "health_manager", ProviderHealthManager())
    monkeypatch.setattr(engine_state, "configured_providers", [ANTH, LOCAL])
    return cfg


class TestFactoryFlavor:
    def test_factory_builds_anthropic_form_for_anthropic_custom(
            self, factory_env, store):
        store.save_provider(ANTH, "anthropic-custom-key")
        inst = _create_provider_instance(
            ANTH, ANTH_MODEL, store, user_config=factory_env)
        assert isinstance(inst, LiteLLMProvider)
        assert inst.litellm_model == f"anthropic/{ANTH_MODEL}"
        assert inst.provider_flavor == "anthropic"
        assert inst.base_url == ANTH_URL
        assert inst.api_key == "anthropic-custom-key"

    def test_factory_openai_custom_unchanged(self, factory_env, store):
        inst = _create_provider_instance(
            LOCAL, LOCAL_MODEL, store, user_config=factory_env)
        assert inst.litellm_model == f"openai/{LOCAL_MODEL}"
        assert inst.provider_flavor == "openai_compatible"

    def test_factory_legacy_custom_without_recorded_flavor(self, factory_env, store):
        """A custom saved before flavours were recorded dispatches as
        openai_compatible — the historical behaviour."""
        cfg = _cfg(custom_provider_types={LOCAL: "openai_compatible"})
        inst = _create_provider_instance(
            ANTH, ANTH_MODEL, store, user_config=cfg)
        # No key needed: with no recorded type the shape is the legacy
        # keyless custom (E5-F04); the protocol is openai either way.
        assert inst.litellm_model == f"openai/{ANTH_MODEL}"
        assert inst.provider_flavor == "openai_compatible"

    def test_cross_provider_fallback_to_anthropic_custom_resolves_anthropic(
            self, factory_env, store):
        """Fallback candidates are resolved at construction time in
        the factory; a candidate served by an anthropic-flavour
        custom must carry the anthropic form, not openai/."""
        store.save_provider("google_gemini", "gemini-key")
        store.save_provider(ANTH, "anthropic-custom-key")
        cfg = _cfg(
            default_provider="google_gemini", default_model="gemini-main",
            fallback_chain=[ANTH])
        inst = _create_provider_instance(
            "google_gemini", "gemini-main", store, user_config=cfg)
        fb = [fm for fm in inst.fallback_models if fm.provider_name == ANTH]
        assert len(fb) == 1
        assert fb[0].model == f"anthropic/{ANTH_MODEL}"
        assert fb[0].base_url == ANTH_URL

    def test_provider_instance_direct_construction_with_flavor(self):
        """The provider itself honours an explicit flavour (the
        factory's channel); without one it keeps the legacy form."""
        inst = LiteLLMProvider(
            api_key="k", model=ANTH_MODEL, provider_name=ANTH,
            base_url=ANTH_URL, provider_flavor="anthropic")
        assert inst.litellm_model == f"anthropic/{ANTH_MODEL}"
        legacy = LiteLLMProvider(
            api_key="k", model=ANTH_MODEL, provider_name=ANTH, base_url=ANTH_URL)
        assert legacy.litellm_model == f"openai/{ANTH_MODEL}"


# ---------------------------------------------------------------------------
# Seam (c): discovery — the audit's repro sent Authorization, not
# x-api-key, and returned the catalog through the OpenAI branch.
# ---------------------------------------------------------------------------

def _mock_get(payload, status_code=200):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = payload
    mock_resp.text = "mock body"
    return mock_resp


@pytest.mark.asyncio
async def test_discovery_anthropic_flavor_uses_anthropic_protocol():
    payload = {"data": [{"id": ANTH_MODEL}, {"id": "text-embedding-3"}]}
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_get(payload)
        result = await fetch_available_models(
            ANTH, "anthropic-custom-key", base_url=ANTH_URL,
            provider_flavor="anthropic")
    assert result.ok
    assert result.models == [ANTH_MODEL]
    assert {e["id"] for e in result.excluded} == {"text-embedding-3"}
    url = mock_get.call_args.args[0]
    headers = mock_get.call_args.kwargs["headers"]
    assert url == f"{ANTH_URL}/v1/models"
    assert headers.get("x-api-key") == "anthropic-custom-key"
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_discovery_openai_flavor_custom_unchanged():
    """Control: openai_compatible discovery is byte-identical —
    Bearer auth against {base}/models, OpenAI-shaped catalog."""
    payload = {"data": [{"id": LOCAL_MODEL}, {"id": "text-embedding-ada-002"}]}
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_get(payload)
        result = await fetch_available_models(
            LOCAL, "local-key", base_url=LOCAL_URL,
            provider_flavor="openai_compatible")
    assert result.ok
    assert result.models == [LOCAL_MODEL]
    assert {e["id"] for e in result.excluded} == {"text-embedding-ada-002"}
    url = mock_get.call_args.args[0]
    headers = mock_get.call_args.kwargs["headers"]
    assert url == f"{LOCAL_URL}/models"
    assert headers.get("Authorization") == "Bearer local-key"
    assert "x-api-key" not in headers


@pytest.mark.asyncio
async def test_discovery_builtin_anthropic_unchanged():
    """Control: the built-in anthropic provider's discovery already
    used the Anthropic protocol and must not move."""
    payload = {"data": [{"id": ANTH_MODEL}]}
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_get(payload)
        result = await fetch_available_models("anthropic", "builtin-key")
    assert result.ok
    assert result.models == [ANTH_MODEL]
    url = mock_get.call_args.args[0]
    headers = mock_get.call_args.kwargs["headers"]
    assert url == "https://api.anthropic.com/v1/models"
    assert headers.get("x-api-key") == "builtin-key"


@pytest.mark.asyncio
async def test_provider_list_discovery_honours_saved_flavor(monkeypatch, store):
    """End-to-end at the provider-list seam: with the flavour saved
    in config (exactly what PROVIDER_SAVE persists), the list
    builder's auto-discovery for the anthropic custom speaks the
    Anthropic protocol — pre-fix this sent Authorization."""
    cfg = _cfg(provider_models={LOCAL: [LOCAL_MODEL]})
    monkeypatch.setattr(engine_state, "user_config", cfg)
    monkeypatch.setattr(engine_state, "model_registry", _registry())
    monkeypatch.setattr(engine_state, "health_manager", ProviderHealthManager())
    store.save_provider(ANTH, "anthropic-custom-key")
    payload = {"data": [{"id": ANTH_MODEL}]}
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_get(payload)
        providers = await _build_provider_list_async(store)
    anth = [p for p in providers if p["name"] == ANTH]
    assert len(anth) == 1
    assert anth[0]["models"] == [ANTH_MODEL]
    assert anth[0]["provider_type"] == "anthropic"
    headers = mock_get.call_args.kwargs["headers"]
    assert headers.get("x-api-key") == "anthropic-custom-key"
    assert "Authorization" not in headers
