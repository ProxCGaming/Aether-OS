"""Epic 2, E2-10: provider-keyed web search seam.

ddgs stays the no-key default. Keyed providers go through an
injectable transport (no real network in tests); failures surface
honestly; fallback to ddgs happens only when configured; a missing
key is a clear error, never a silent swap.
"""

from __future__ import annotations

import pytest

from aether_engine.config import UserConfig
from aether_engine.tools import web_search
from aether_engine.tools.web_search import search_web_dispatch, set_search_key_loader


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    monkeypatch.setenv("BRAVE_API_KEY", "test-brave-key")
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-firecrawl-key")
    set_search_key_loader(None)
    yield
    set_search_key_loader(None)


def _transport(body):
    def call(method, url, headers, json_body):
        call.seen = (method, url, headers, json_body)
        return body
    return call


def test_exa_provider_via_transport():
    body = {"results": [{"title": "T", "url": "https://x.example", "text": "snippet text"}]}
    out = search_web_dispatch("query", provider="exa", transport=_transport(body))
    assert "Title: T" in out and "URL: https://x.example" in out and "snippet text" in out


def test_brave_and_tavily_and_firecrawl_shapes():
    brave = {"web": {"results": [{"title": "B", "url": "https://b.example", "description": "bd"}]}}
    assert "Title: B" in search_web_dispatch("q", provider="brave", transport=_transport(brave))
    tavily = {"results": [{"title": "Tv", "url": "https://t.example", "content": "tc"}]}
    assert "Title: Tv" in search_web_dispatch("q", provider="tavily", transport=_transport(tavily))
    fire = {"data": {"web": [{"title": "F", "url": "https://f.example", "description": "fd"}]}}
    assert "Title: F" in search_web_dispatch("q", provider="firecrawl", transport=_transport(fire))


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY")
    out = search_web_dispatch("q", provider="exa", transport=_transport({}))
    assert "no API key stored for 'exa'" in out


def test_provider_failure_is_honest_and_fallback_is_opt_in(monkeypatch):
    def boom(method, url, headers, json_body):
        raise RuntimeError("HTTP 503")

    out = search_web_dispatch("q", provider="tavily", transport=boom)
    assert out.startswith("Search failed (tavily)")

    monkeypatch.setattr(web_search, "_search_ddgs", lambda q, m: "DDGS RESULTS")
    out = search_web_dispatch("q", provider="tavily", transport=boom, fallback_to_ddgs=True)
    assert "fell back to ddgs" in out and "DDGS RESULTS" in out
    # Without the opt-in there is no fallback.
    out = search_web_dispatch("q", provider="tavily", transport=boom, fallback_to_ddgs=False)
    assert "DDGS RESULTS" not in out


def test_secret_store_loader_wins_over_env(monkeypatch):
    set_search_key_loader(lambda name: "store-key" if name == "exa" else "")
    transport = _transport({"results": []})
    search_web_dispatch("q", provider="exa", transport=transport)
    assert transport.seen[2]["x-api-key"] == "store-key"


def test_config_round_trip():
    cfg = UserConfig()
    cfg.search_provider = "tavily"
    cfg.search_fallback_to_ddgs = True
    restored = UserConfig.from_dict(cfg.to_dict())
    assert restored.search_provider == "tavily" and restored.search_fallback_to_ddgs is True
    assert UserConfig.from_dict({"search_provider": "bogus"}).search_provider == "ddgs"
