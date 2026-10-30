"""Provider-keyed web search (Epic 2, E2-10).

``search_web`` dispatches through a provider seam: ``ddgs`` (no key,
the long-standing default) or a keyed provider — Exa, Brave, Tavily,
Firecrawl — selected by ``search_provider`` in user config. Keys come
from the engine's encrypted secret store under the provider's name
(the Providers settings already save arbitrary provider keys); the
websocket layer installs :func:`set_search_key_loader` at startup.
Without the loader, keys fall back to the conventional environment
variables (``EXA_API_KEY`` etc.).

Failure honesty: a keyed provider that errors returns its error as
the tool result; it falls back to ddgs ONLY when the user configured
``search_fallback_to_ddgs``. A missing key is a clear error naming
where to put the key — never a silent provider swap.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("aether_engine.tools.web_search")

PROVIDERS = ("ddgs", "exa", "brave", "tavily", "firecrawl")
_ENV_KEYS = {"exa": "EXA_API_KEY", "brave": "BRAVE_API_KEY", "tavily": "TAVILY_API_KEY", "firecrawl": "FIRECRAWL_API_KEY"}

_key_loader: Callable[[str], str] | None = None
_config_loader: Callable[[], tuple[str, bool]] | None = None


def set_search_key_loader(loader: Callable[[str], str] | None) -> None:
    """Install the secret-store-backed key loader (websocket startup)."""
    global _key_loader
    _key_loader = loader


def set_search_config_loader(loader: Callable[[], tuple[str, bool]] | None) -> None:
    """Install the live config loader: () -> (provider, fallback_to_ddgs)."""
    global _config_loader
    _config_loader = loader


def current_search_config() -> tuple[str, bool]:
    if _config_loader is not None:
        try:
            provider, fallback = _config_loader()
            return (provider if provider in PROVIDERS else "ddgs", bool(fallback))
        except Exception:  # a broken config hook degrades to the ddgs default
            logger.debug("Search config loader failed", exc_info=True)
    return ("ddgs", False)


def _api_key(provider: str) -> str:
    if _key_loader is not None:
        try:
            key = _key_loader(provider)
            if key:
                return key
        except Exception:  # a broken store must degrade to env/no-key, not crash search
            logger.debug("Search key loader failed for %s", provider, exc_info=True)
    return os.environ.get(_ENV_KEYS.get(provider, ""), "")


def _format_results(results: list[dict[str, Any]]) -> str:
    if not results:
        return "No results found."
    blocks = []
    for r in results:
        blocks.append(f"Title: {r.get('title', '')}\nURL: {r.get('url', '')}\nSnippet: {r.get('snippet', '')}\n")
    return "\n".join(blocks)


def _search_ddgs(query: str, max_results: int) -> str:
    from ddgs import DDGS

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
    except Exception as e:  # noqa: BLE001 — ddgs raises arbitrary network errors; surfaced as the result
        return f"Search failed: {e}"
    return _format_results([
        {"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body")} for r in results
    ])


def _search_keyed(provider: str, query: str, max_results: int, transport: Any = None) -> str:
    """One keyed provider call. ``transport`` is injectable for tests:
    a callable taking (method, url, headers, json) and returning the
    decoded JSON body, raising on HTTP errors."""
    key = _api_key(provider)
    if not key:
        return (
            f"Search failed: no API key stored for '{provider}'. "
            f"Add it in Settings → Providers (saved as '{provider}') or set {_ENV_KEYS[provider]}."
        )
    import httpx

    def _default_transport(method: str, url: str, headers: dict, json_body: dict | None) -> Any:
        with httpx.Client(timeout=20.0) as client:
            resp = client.request(method, url, headers=headers, json=json_body)
            resp.raise_for_status()
            return resp.json()

    call = transport or _default_transport
    try:
        if provider == "exa":
            body = call("POST", "https://api.exa.ai/search",
                        {"x-api-key": key, "Content-Type": "application/json"},
                        {"query": query, "numResults": max_results})
            results = [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("text", "")[:500]} for r in body.get("results", [])]
        elif provider == "brave":
            body = call("GET", f"https://api.search.brave.com/res/v1/web/search?q={query}&count={max_results}",
                        {"X-Subscription-Token": key, "Accept": "application/json"}, None)
            results = [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("description", "")}
                       for r in body.get("web", {}).get("results", [])]
        elif provider == "tavily":
            body = call("POST", "https://api.tavily.com/search",
                        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        {"query": query, "max_results": max_results})
            results = [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content", "")[:500]} for r in body.get("results", [])]
        elif provider == "firecrawl":
            body = call("POST", "https://api.firecrawl.dev/v2/search",
                        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        {"query": query, "limit": max_results})
            data = body.get("data", {})
            web = data.get("web", data) if isinstance(data, dict) else data
            results = [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("description", "")} for r in web]
        else:  # pragma: no cover - guarded by PROVIDERS at the seam
            return f"Search failed: unknown provider '{provider}'."
    except Exception as e:  # noqa: BLE001 — provider/transport errors surface honestly as the result
        return f"Search failed ({provider}): {e}"
    return _format_results(results)


def search_web_dispatch(
    query: str,
    max_results: int = 5,
    *,
    provider: str = "ddgs",
    fallback_to_ddgs: bool = False,
    transport: Any = None,
) -> str:
    """The search_web tool body, parameterised by config for testability."""
    provider = provider if provider in PROVIDERS else "ddgs"
    if provider == "ddgs":
        return _search_ddgs(query, max_results)
    result = _search_keyed(provider, query, max_results, transport=transport)
    if result.startswith("Search failed") and fallback_to_ddgs:
        return f"[{provider} search failed; fell back to ddgs as configured]\n" + _search_ddgs(query, max_results)
    return result
