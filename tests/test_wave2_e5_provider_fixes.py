"""Wave 2 / chunk E5 — provider, routing and cost fixes.

Pins the confirmed Pass A findings (Pass B: all CONFIRMED):

- E5-A: OpenAI discovery's prefix list dropped the gpt-5 family (and
  o4) — the provider's current flagship line could never appear.
- E5-E: the Gemini discovery request carried the API key in the URL
  query string; it must travel in the ``x-goog-api-key`` header.
- E5-K: OpenRouter discovery sliced the raw list to 50 *before*
  filtering, so chat models ranked past the first 50 raw entries
  were never discovered.
- E5-B: ``classify_provider_error`` ran raw substring checks for
  "429"/"500" *before* the auth check, so an auth failure mentioning
  a status number classified as retriable; digit runs inside longer
  numbers (request ids, token counts) also matched. Auth/typed
  classification now wins and status matches are digit-boundary
  aware; a typed ``ProviderError``'s ``retriable`` flag is honoured.
- E5-I: the health-derived fallback path took a provider's first
  registry model with no chat-capability check, disagreeing with
  the configured-chain path; both now pick the first chat-capable
  model.
- E5-C: ``record_provider_call`` re-loaded the whole config from
  disk on every recorded call just to read the daily budget. The
  budget figure is now cached against the config file's stamp and
  re-read only when the file changes (a save IS reflected).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aether_engine import config as config_module
from aether_engine.cost import ledger as ledger_module
from aether_engine.cost.ledger import UsageLedger
from aether_engine.event_bus import EventBus
from aether_engine.providers.base import AuthenticationError, ProviderError
from aether_engine.providers.discovery import (
    _discover_gemini_models,
    _discover_openai_models,
    _discover_openrouter_models,
)
from aether_engine.routing.fallback import (
    classify_provider_error,
    get_fallback_candidates,
)
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.registry import ModelEntry, ModelRegistry


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    """Same proxy-env guard as the other discovery test modules."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                "https_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)


def _mock_get(fake_response, status_code=200):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = fake_response
    mock_resp.text = "mock body"
    return mock_resp


# ---------------------------------------------------------------------------
# E5-A — OpenAI discovery keeps the gpt-5 family
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_openai_discovery_includes_gpt5_family():
    fake_response = {
        "data": [
            {"id": "gpt-5"},
            {"id": "gpt-5-mini"},
            {"id": "gpt-5-nano"},
            {"id": "o4-mini"},
            {"id": "gpt-4o"},
            {"id": "text-embedding-3-large"},
            {"id": "dall-e-3"},
            {"id": "gpt-5-audio-preview"},
        ]
    }
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_get(fake_response)
        models, excluded = await _discover_openai_models("fake-key")

    assert "gpt-5" in models
    assert "gpt-5-mini" in models
    assert "gpt-5-nano" in models
    assert "o4-mini" in models
    assert "gpt-4o" in models
    # Non-chat junk still excluded, even from a kept family.
    assert "text-embedding-3-large" not in models
    assert "dall-e-3" not in models
    assert "gpt-5-audio-preview" not in models
    excluded_ids = {e["id"] for e in excluded}
    assert "text-embedding-3-large" in excluded_ids
    assert "dall-e-3" in excluded_ids


# ---------------------------------------------------------------------------
# E5-E — Gemini key travels in the header, never in the URL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gemini_discovery_sends_key_as_header_not_url():
    api_key = "AIza-test-key-not-for-url"
    fake_response = {
        "models": [
            {
                "name": "models/gemini-3.5-flash",
                "supportedGenerationMethods": ["generateContent"],
            },
        ]
    }
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_get(fake_response)
        models, _ = await _discover_gemini_models(api_key)

    assert models == ["gemini-3.5-flash"]
    args, kwargs = mock_get.call_args
    url = args[0] if args else kwargs.get("url", "")
    assert api_key not in url
    assert "key=" not in url
    headers = kwargs.get("headers") or {}
    assert headers.get("x-goog-api-key") == api_key


# ---------------------------------------------------------------------------
# E5-K — OpenRouter: filter first, then take the top 50 chat models
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_openrouter_discovery_filters_before_slicing():
    raw = [{"id": f"vendor/image-gen-{i}"} for i in range(55)]
    raw += [
        {"id": "openai/gpt-4o"},
        {"id": "anthropic/claude-sonnet-4"},
        {"id": "google/gemini-2.5-pro"},
    ]
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_get({"data": raw})
        models, _excluded = await _discover_openrouter_models("fake-key")

    assert "openai/gpt-4o" in models
    assert "anthropic/claude-sonnet-4" in models
    assert "google/gemini-2.5-pro" in models
    assert all("image" not in m for m in models)


# ---------------------------------------------------------------------------
# E5-B — fallback error classification
# ---------------------------------------------------------------------------

def test_auth_error_mentioning_500_is_not_retriable():
    retriable, msg = classify_provider_error(
        AuthenticationError("Upstream 500 while checking key: invalid API key"))
    assert retriable is False
    assert "authentication" in msg.lower()


def test_auth_status_wins_over_server_status_digits():
    retriable, _ = classify_provider_error(
        Exception("401 Unauthorized: invalid API key (gateway ref 500)"))
    assert retriable is False


def test_digit_runs_inside_longer_numbers_do_not_match():
    # "14290" contains "429"; "5032" contains "503"; neither is a status.
    retriable, _ = classify_provider_error(
        Exception("job 14290 finished; 5032 items remaining"))
    assert retriable is False
    # A token count of 1500 is not an HTTP 500.
    retriable, _ = classify_provider_error(
        Exception("request used 1500 tokens exactly"))
    assert retriable is False


def test_bare_status_codes_still_classify():
    assert classify_provider_error(Exception("503 Service Unavailable"))[0] is True
    assert classify_provider_error(Exception("429 Too Many Requests"))[0] is True
    assert classify_provider_error(Exception("HTTP 500 Internal Server Error"))[0] is True
    assert classify_provider_error(Exception("403 Forbidden"))[0] is False


def test_typed_provider_error_flag_is_honoured():
    assert classify_provider_error(
        ProviderError("transient blip", retriable=True))[0] is True
    assert classify_provider_error(
        ProviderError("Bad request: internal ref 500 on file", retriable=False))[0] is False


# ---------------------------------------------------------------------------
# E5-I — health-derived fallback path applies the chat filter too
# ---------------------------------------------------------------------------

def test_health_fallback_skips_non_chat_first_model():
    registry = ModelRegistry([
        ModelEntry(provider="provx", id="provx-image-1", label="Image",
                   capabilities=["image"], priority=1),
        ModelEntry(provider="provx", id="provx-chat-1", label="Chat",
                   capabilities=["chat"], priority=2),
    ])
    candidates = get_fallback_candidates(
        failed_provider="other",
        configured_providers=["provx"],
        health_manager=ProviderHealthManager(),
        registry=registry,
    )
    assert [c.id for c in candidates] == ["provx-chat-1"]


# ---------------------------------------------------------------------------
# E5-C — daily-budget config read is cached; a save is reflected
# ---------------------------------------------------------------------------

@pytest.fixture
def budget_env(tmp_path, monkeypatch):
    """Tmp CONFIG_PATH + tmp ledger + counting load_config wrapper."""
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg_path)

    real_load = config_module.load_config
    reads = {"count": 0}

    def counting_load(*args, **kwargs):
        reads["count"] += 1
        return real_load(*args, **kwargs)

    monkeypatch.setattr(config_module, "load_config", counting_load)
    # Start every test with an empty budget cache (attribute exists
    # only once the E5-C cache lands; tolerate its absence pre-fix).
    monkeypatch.setattr(ledger_module, "_daily_budget_cache", None, raising=False)

    ledger = UsageLedger(tmp_path / "usage.db")
    monkeypatch.setattr(ledger_module, "GLOBAL_USAGE_LEDGER", ledger)

    bus = EventBus()
    queue: asyncio.Queue = asyncio.Queue()
    bus.subscribe("t_e5c", queue)
    monkeypatch.setattr(ledger_module, "event_bus", bus)

    return cfg_path, ledger, queue, reads


def _drain_budget_events(queue):
    events = []
    while not queue.empty():
        ev = queue.get_nowait()
        if ev.type.value == "DAILY_BUDGET_EXCEEDED":
            events.append(ev)
    return events


def test_record_provider_call_reads_config_once_and_reflects_save(budget_env):
    _cfg_path, ledger, queue, reads = budget_env
    from aether_engine.config import UserConfig, save_config

    save_config(UserConfig(daily_budget_usd=100.0))
    ledger.record_call(
        model_name="gemini-3.5-flash", provider_name="google_gemini",
        input_tokens=10, output_tokens=5, cost_usd=5.0,
        session_id="s_e5c", task_id="t_e5c")

    with ledger_module.usage_context(session_id="s_e5c", task_id="t_e5c"):
        row1 = ledger_module.record_provider_call(
            model_name="gemini-3.5-flash", provider_name="google_gemini",
            input_tokens=10, output_tokens=5)
        row2 = ledger_module.record_provider_call(
            model_name="gemini-3.5-flash", provider_name="google_gemini",
            input_tokens=10, output_tokens=5)
    assert row1 is not None and row2 is not None
    # Two recorded calls, at most one config read between them.
    assert reads["count"] == 1
    # $5.05-ish of spend against a $100 budget: no alert.
    assert _drain_budget_events(queue) == []

    # Lower the budget below today's spend and save: the next recorded
    # call must see the NEW budget (one more read) and alert.
    save_config(UserConfig(daily_budget_usd=1.0))
    with ledger_module.usage_context(session_id="s_e5c", task_id="t_e5c"):
        row3 = ledger_module.record_provider_call(
            model_name="gemini-3.5-flash", provider_name="google_gemini",
            input_tokens=10, output_tokens=5)
    assert row3 is not None
    assert reads["count"] == 2
    events = _drain_budget_events(queue)
    assert len(events) == 1
    assert events[0].payload["daily_budget_usd"] == pytest.approx(1.0)
