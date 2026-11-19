"""GPT Researcher backend for the researcher specialist (P4c).

Wraps **GPT Researcher** (Apache-2.0, pinned ``gpt-researcher==0.17.0``)
as a single registry tool — ``deep_research`` — so the native
researcher decides when a question warrants the heavy engine: depth
is a delegation decision, visible in the delegation log, rather than
every research task paying deep-research cost.

**Spike findings (plan step 0, verified against the installed
0.17.0 — the plan's "v3.8.0" is not a version the PyPI package has
ever carried; 0.17.0 is the current release and is what the pin
names):**

* Per-stage model config is FULLY supported: the library config keys
  ``FAST_LLM`` / ``SMART_LLM`` / ``STRATEGIC_LLM`` each take a
  ``"provider:model"`` string and are parsed independently. All
  three stages are pointed at the delegation's resolved model —
  there is no stage the wrapper cannot route.
* The LLM provider ``"litellm"`` is supported (via langchain's
  ChatLiteLLM), and the config's ``LLM_KWARGS`` are merged into the
  chat-model constructor kwargs — so the resolved provider's API
  key and custom base URL ride the config, with NO environment
  mutation for the LLM leg.
* Retrievers resolve by NAME at construction (built-ins or
  entry-point plugins); there is no class-injection parameter. The
  retriever contract is ``Cls(query, query_domains=None)`` with a
  sync ``search(max_results)`` returning ``list[dict]``; a retriever
  declaring ``requires_scraping = False`` hands back already-fetched
  content as ``{url, raw_content}`` items. The fallback retriever
  below implements exactly that contract over Aether's own
  ``search_web`` / ``fetch_url`` tool bodies, and is installed by
  replacing the constructed researcher's ``retrievers`` list — the
  instance attribute every search path in the library reads.
* Costs ARE exposed (``get_costs()`` / ``get_step_costs()``), so the
  budget is judged between the library's two macro-stages
  (``conduct_research`` → ``write_report``): a run over budget after
  the research stage never starts the report stage. The library
  exposes no mid-stage usage hook, so within a stage the bound is
  the stage cap (``MAX_ITERATIONS``) — the tool result's budget note
  says which bound applied, never silently.
* Embeddings have no litellm provider in the library; ``"ollama"``
  is supported, which is exactly how Aether serves its configured
  embedder (``memory/episodic.py``: bare Ollama model name), so the
  mapping is direct. One library bug the wrapper must absorb: the
  ollama embeddings branch reads ``os.environ["OLLAMA_BASE_URL"]``
  unconditionally — a hard KeyError when unset — so the wrapper
  scopes that variable for the run (Aether's own endpoint,
  ``http://127.0.0.1:11434``, or the configured custom base URL)
  whenever the embedder is Ollama and the variable is unset.

**Model resolution.** "The model the delegation resolved" is, at
tool-execution time, the researcher's ``agent_models`` pin when one
is set, else the configured default provider/model — the same
resolution the specialist runner applies before S14. S14's
per-delegation auto-choice is made inside the graph run and is not
observable from a tool; when it re-routes a delegation, this backend
still follows the pin-or-default resolution. The provider is
constructed through the same factory the runner uses
(``orchestration.core._create_provider_instance``), so fallback
policy, custom base URLs and key handling are the engine's own.

**Retriever choice is never silent.** With a Tavily key stored
(provider name ``"tavily"`` in the secret store, or
``TAVILY_API_KEY`` in the environment) the library's Tavily
retriever runs; without one, the fallback retriever over Aether's
web tools runs at lower depth. Either way the tool result names the
retriever that ran, and the delegation-log entry records it.

**Output.** The tool result is a JSON object: the report, the
source list (``[{url, title}]`` — the citation shape every consumer
of this backend shares), which retriever ran, the library-recorded
cost, the budget note, and the artifact path. The report is also
saved as a task artifact through ``artifacts.manager.save_artifact``
(the specialists' existing artifact path); it is not pasted into
chat state wholesale. Sources are recorded in the delegation log by
``execute_tool_node`` via :func:`delegation_log_entry`.

Runs are serialised on a module lock: the Tavily leg scopes the key
through the process environment (the only channel the library's
Tavily retriever reads), and two interleaved runs must not see each
other's key or config. Deep research is heavyweight; serial is the
honest shape for v1.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from aether_engine.tools.registry import Tool, ToolRisk

logger = logging.getLogger("aether_engine.langgraph.backends.researcher_gpt")

DEEP_RESEARCH_TOOL_NAME = "deep_research"

#: Stage cap handed to the library config: research iterations per
#: run. The library's own default is 3; named here because, with no
#: cost budget configured, this cap is the run's only bound and the
#: tool result says so.
MAX_RESEARCH_ITERATIONS = 3

#: Per-page character budget for the fallback retriever's fetches,
#: and the cap on each page's raw_content handed to the library —
#: deep-research context is assembled from many pages, and one huge
#: page must not crowd out the rest.
FETCH_CHARS_PER_PAGE = 12000
RAW_CONTENT_CHARS = 8000

#: GPT Researcher 0.17.0's supported embedding providers
#: (memory/embeddings.py's match arms, read at spike time). Aether's
#: embedder resolves to "ollama" in practice; the set exists so a
#: future prefixed config name (e.g. "openai/text-embedding-3") maps
#: across only when the library can actually serve it.
_LIBRARY_EMBEDDING_PROVIDERS = frozenset({
    "aimlapi", "azure_openai", "bedrock", "cohere", "custom",
    "dashscope", "fireworks", "gigachat", "google_genai",
    "google_vertexai", "huggingface", "minimax", "mistralai",
    "nebius", "netmind", "nomic", "ollama", "openai", "openrouter",
    "together", "voyageai",
})


# ── Runtime seam (installed by the websocket layer, N-43 pattern) ──


@dataclass
class DeepResearchRuntime:
    """What a deep-research run needs from the live engine: the
    current user config (read fresh per run) and the connection's
    secret store (``load_provider(name) -> str``)."""

    user_config: Any
    secret_store: Any = None


_runtime_loader: Callable[[], DeepResearchRuntime | None] | None = None


def set_deep_research_runtime_loader(
    loader: Callable[[], DeepResearchRuntime | None] | None,
) -> Callable[[], DeepResearchRuntime | None] | None:
    """Install the runtime loader (websocket startup).

    Returns the previously installed loader so the caller that
    replaced it can restore it when it is done — the loaders are
    module-global seams, not per-connection state (N-43).
    """
    global _runtime_loader
    previous = _runtime_loader
    _runtime_loader = loader
    return previous


def _runtime() -> DeepResearchRuntime:
    """The live runtime, or a persisted-config fallback.

    With no loader installed (tests, one-shot calls) the user config
    is the persisted one and there is no secret store: provider keys
    then resolve through LiteLLM's own environment conventions and
    the Tavily leg falls back to ``TAVILY_API_KEY`` — degraded, and
    every consumer below treats a missing key as "not configured",
    never as an empty-string key.
    """
    if _runtime_loader is not None:
        try:
            runtime = _runtime_loader()
        except Exception:  # a broken seam degrades to the fallback, not a crash
            logger.debug("Deep research runtime loader failed", exc_info=True)
            runtime = None
        if runtime is not None:
            return runtime
    from aether_engine.config import load_config

    try:
        user_config = load_config()
    except Exception:  # noqa: BLE001 — a broken persisted config must not crash the tool; settings resolution reports the failure
        user_config = None
    return DeepResearchRuntime(user_config=user_config, secret_store=None)


def _load_secret(runtime: DeepResearchRuntime, name: str) -> str:
    """A stored provider key, or "" when there is none.

    The secret store raises for a missing provider; that is a normal
    "not configured" answer here, not an error.
    """
    store = runtime.secret_store
    if store is not None:
        try:
            key = store.load_provider(name)
            if key:
                return str(key)
        except Exception:
            logger.debug("No stored secret for %s", name, exc_info=True)
    return ""


# ── Settings resolution ─────────────────────────────────────────────


@dataclass
class DeepResearchSettings:
    """Everything one run needs, resolved from config + secrets."""

    provider_name: str
    model_name: str
    litellm_model: str
    api_key: str = ""
    api_base: str | None = None
    embedding_config: str = "ollama:qwen3-embedding:0.6b"
    #: Base URL for the embedding endpoint when the embedder needs
    #: one scoped into the environment (the Ollama case — see the
    #: module docstring's spike notes). None = nothing to scope.
    embedding_base_url: str | None = None
    tavily_api_key: str = ""
    budget_usd: float = 0.0
    max_iterations: int = MAX_RESEARCH_ITERATIONS

    @property
    def retriever_name(self) -> str:
        return "tavily" if self.tavily_api_key else "aether_web"


def _embedding_config(user_config: Any) -> str:
    """Aether's embedder as a GPT Researcher ``provider:model`` string.

    Aether resolves its embedder to a LiteLLM form (``ollama/<name>``
    for the local default — see memory/episodic.py). The library's
    form is the same pair joined by a colon, when the provider is
    one the library supports; anything else degrades to the local
    Ollama form of the bare model name, which is what Aether itself
    serves.
    """
    from aether_engine.memory.episodic import litellm_embedding_model

    litellm_form = litellm_embedding_model(user_config)
    provider, _, model = litellm_form.partition("/")
    if provider in _LIBRARY_EMBEDDING_PROVIDERS and model:
        return f"{provider}:{model}"
    return f"ollama:{model or litellm_form}"


def _resolve_settings(
    runtime: DeepResearchRuntime,
    *,
    provider_factory: Callable[..., Any] | None = None,
) -> DeepResearchSettings:
    """Resolve the run's model, keys, embedder and budget.

    Model: the researcher's ``agent_models`` pin when set (the
    runner's own pre-S14 resolution), else the configured default
    provider/model. The provider is constructed through the runner's
    factory so its ``litellm_model`` / ``api_key`` / ``base_url``
    are the engine's own values; a pin whose construction fails
    falls back to the default provider, exactly as the runner falls
    back to the graph provider. With no secret store (no loader
    installed) construction is impossible, so the LiteLLM model
    string is resolved directly and keys are left to LiteLLM's
    environment conventions.
    """
    user_config = runtime.user_config
    if user_config is None:
        raise ValueError("no user config available for deep research")

    pin = None
    agent_models = getattr(user_config, "agent_models", None) or {}
    raw_pin = agent_models.get("researcher")
    if raw_pin and raw_pin != "inherit" and ":" in str(raw_pin):
        pin = str(raw_pin)

    default_provider = str(getattr(user_config, "default_provider", "") or "")
    default_model = str(getattr(user_config, "default_model", "") or "")

    factory = provider_factory
    if factory is None:
        from aether_engine.orchestration.core import _create_provider_instance

        factory = _create_provider_instance

    provider_name = ""
    model_name = ""
    litellm_model = ""
    api_key = ""
    api_base: str | None = None

    attempts: list[tuple[str, str]] = []
    if pin is not None:
        p_name, m_name = pin.split(":", 1)
        attempts.append((p_name, m_name))
    attempts.append((default_provider, default_model))

    last_error: Exception | None = None
    for p_name, m_name in attempts:
        if not p_name or not m_name:
            continue
        if runtime.secret_store is None:
            from aether_engine.providers.litellm_provider import (
                resolve_litellm_model,
            )

            provider_name, model_name = p_name, m_name
            litellm_model = resolve_litellm_model(m_name, p_name)
            custom_urls = getattr(user_config, "custom_base_urls", None) or {}
            api_base = custom_urls.get(p_name)
            last_error = None
            break
        try:
            provider = factory(
                p_name, m_name, runtime.secret_store, user_config=user_config)
        except Exception as exc:  # noqa: BLE001 — an unusable pin falls back to the default provider (the runner's behaviour); a failed default surfaces below
            last_error = exc
            logger.warning(
                "Deep research provider %s:%s unusable (%s); trying the next resolution",
                p_name, m_name, exc)
            continue
        provider_name = str(getattr(provider, "provider_name", p_name))
        model_name = str(getattr(provider, "model", m_name))
        litellm_model = str(getattr(provider, "litellm_model", "") or "")
        api_key = str(getattr(provider, "api_key", "") or "")
        base_url = getattr(provider, "base_url", None)
        api_base = str(base_url) if base_url else None
        last_error = None
        break

    if not litellm_model:
        detail = f": {last_error}" if last_error is not None else ""
        raise ValueError(
            "could not resolve a model for deep research "
            f"(pin={pin!r}, default={default_provider}:{default_model}){detail}")

    tavily_key = _load_secret(runtime, "tavily") or os.environ.get("TAVILY_API_KEY", "")
    embedding_config = _embedding_config(user_config)
    embedding_base_url: str | None = None
    if embedding_config.startswith("ollama:"):
        # The library's ollama embeddings branch hard-requires
        # OLLAMA_BASE_URL (module docstring); scope Aether's own
        # endpoint — or the owner's configured base URL for the
        # ollama provider — when the environment doesn't set one.
        custom_urls = getattr(user_config, "custom_base_urls", None) or {}
        embedding_base_url = (
            custom_urls.get("ollama") or "http://127.0.0.1:11434")
    budget = getattr(user_config, "deep_research_cost_budget_usd", 0.0)
    try:
        budget_usd = max(0.0, float(budget or 0.0))
    except (TypeError, ValueError):
        budget_usd = 0.0

    return DeepResearchSettings(
        provider_name=provider_name,
        model_name=model_name,
        litellm_model=litellm_model,
        api_key=api_key,
        api_base=api_base,
        embedding_config=embedding_config,
        embedding_base_url=embedding_base_url,
        tavily_api_key=tavily_key,
        budget_usd=budget_usd,
    )


def build_library_config(settings: DeepResearchSettings) -> dict[str, Any]:
    """The GPT Researcher config dict for one run (written to a
    per-run JSON file — the library's config channel — so no
    environment state carries between runs).

    All three stage models are the delegation's resolved model via
    the ``litellm`` provider (spike: per-stage routing is fully
    supported, and one delegation has one resolved model). The
    resolved provider's key/base ride ``LLM_KWARGS`` into the chat
    model constructor. ``RETRIEVER`` names the Tavily retriever when
    a key exists; otherwise it names the library's ``custom``
    retriever as a valid placeholder — the constructed researcher's
    retriever list is replaced with :class:`AetherWebRetriever`
    before any search runs, and the result reports the fallback as
    the retriever that ran.
    """
    llm_kwargs: dict[str, Any] = {}
    if settings.api_key:
        llm_kwargs["api_key"] = settings.api_key
    if settings.api_base:
        llm_kwargs["api_base"] = settings.api_base
    model = f"litellm:{settings.litellm_model}"
    return {
        "RETRIEVER": "tavily" if settings.tavily_api_key else "custom",
        "EMBEDDING": settings.embedding_config,
        "FAST_LLM": model,
        "SMART_LLM": model,
        "STRATEGIC_LLM": model,
        "LLM_KWARGS": llm_kwargs,
        "MAX_ITERATIONS": settings.max_iterations,
        "REPORT_SOURCE": "web",
    }


# ── Fallback retriever over Aether's web tools (plan step 2) ───────


def _default_search(query: str, max_results: int) -> str:
    """The search_web tool body, with the live configured provider."""
    from aether_engine.tools.web_search import (
        current_search_config,
        search_web_dispatch,
    )

    provider, fallback = current_search_config()
    return search_web_dispatch(
        query, max_results=max_results, provider=provider,
        fallback_to_ddgs=fallback)


def _default_fetch(url: str) -> str:
    """The fetch_url tool body (guards, pinning, extraction included)."""
    from aether_engine.tools.web_fetch import _fetch_url_impl

    return _fetch_url_impl(url, max_chars=FETCH_CHARS_PER_PAGE)


def _parse_search_results(raw: str) -> list[dict[str, str]]:
    """Parse search_web's text blocks into {title, url, snippet}.

    The format is the tool's own (``Title:`` / ``URL:`` /
    ``Snippet:`` lines per block). A failed or empty search yields
    no hits — the retriever then honestly returns nothing rather
    than parsing an error string as a result.
    """
    if not raw or raw.startswith("Search failed") or raw.strip() == "No results found.":
        return []
    hits: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in raw.splitlines():
        if line.startswith("Title:"):
            if current.get("url"):
                hits.append(current)
            current = {"title": line[len("Title:"):].strip(), "url": "", "snippet": ""}
        elif line.startswith("URL:"):
            current["url"] = line[len("URL:"):].strip()
        elif line.startswith("Snippet:"):
            current["snippet"] = line[len("Snippet:"):].strip()
    if current.get("url"):
        hits.append(current)
    return [h for h in hits if h["url"].startswith(("http://", "https://"))]


_FETCH_HEADER_RE = re.compile(
    r"^\[fetch_url: (?P<url>\S+) \| title: (?P<title>.*)\]\n?")


def _split_fetch_output(text: str) -> tuple[str, str, str]:
    """(final_url, title, body) from a fetch_url result string.

    The citation header carries the FINAL url (after redirects) and
    the page title — the source list must cite where the content
    actually came from, not the pre-redirect link search returned.
    """
    match = _FETCH_HEADER_RE.match(text or "")
    if not match:
        return "", "", text or ""
    return match.group("url"), match.group("title"), text[match.end():]


def _host_matches(url: str, domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(
        host == d.lower().removeprefix("*.") or host.endswith("." + d.lower().removeprefix("*."))
        for d in domains
    )


class AetherWebRetriever:
    """GPT Researcher retriever backed by Aether's web tools.

    Implements the library's retriever contract (see the module
    docstring): constructed per search with the query, ``search()``
    synchronous (the library calls it in a worker thread). Each hit
    is fetched in full through ``fetch_url`` — its SSRF guard,
    redirect handling and extraction included — and returned as
    already-fetched content (``requires_scraping = False``), so the
    library never re-scrapes a page Aether already read. A page
    whose fetch fails is skipped, never replaced with its snippet:
    the report's citations must be pages the run actually read.
    """

    requires_scraping = False

    def __init__(
        self,
        query: str,
        query_domains: list[str] | None = None,
        *,
        search_fn: Callable[[str, int], str] | None = None,
        fetch_fn: Callable[[str], str] | None = None,
    ):
        self.query = query
        self.query_domains = list(query_domains or [])
        self._search_fn = search_fn
        self._fetch_fn = fetch_fn

    def search(self, max_results: int = 5) -> list[dict[str, Any]]:
        search_fn = self._search_fn or _default_search
        fetch_fn = self._fetch_fn or _default_fetch
        try:
            raw = search_fn(self.query, max_results)
        except Exception:
            logger.warning("AetherWebRetriever search failed", exc_info=True)
            return []
        results: list[dict[str, Any]] = []
        for hit in _parse_search_results(raw):
            if self.query_domains and not _host_matches(hit["url"], self.query_domains):
                continue
            try:
                fetched = fetch_fn(hit["url"])
            except Exception:
                logger.debug("AetherWebRetriever fetch failed for %s", hit["url"], exc_info=True)
                continue
            if not fetched or fetched.startswith("Error:"):
                continue
            final_url, title, body = _split_fetch_output(fetched)
            body = body.strip()
            if not body:
                continue
            results.append({
                "url": final_url or hit["url"],
                "title": title or hit["title"],
                "raw_content": body[:RAW_CONTENT_CHARS],
            })
        return results


# ── The run ─────────────────────────────────────────────────────────


@dataclass
class DeepResearchResult:
    """One run's outcome — the tool payload's source object."""

    query: str
    ok: bool
    report: str = ""
    sources: list[dict[str, str]] = field(default_factory=list)
    retriever: str = ""
    retriever_note: str = ""
    cost_usd: float | None = None
    budget_note: str = ""
    artifact_path: str | None = None
    error: str = ""

    def to_tool_payload(self) -> dict[str, Any]:
        if not self.ok:
            return {"status": "error", "query": self.query, "error": self.error}
        return {
            "status": "ok",
            "query": self.query,
            "retriever": self.retriever,
            "retriever_note": self.retriever_note,
            "report": self.report,
            "sources": self.sources,
            "cost_usd": self.cost_usd,
            "budget_note": self.budget_note,
            "artifact_path": self.artifact_path,
        }


def _safe_costs(researcher: Any) -> float | None:
    """The library's recorded run cost, or None when it can't say."""
    try:
        return float(researcher.get_costs())
    except Exception:
        logger.debug("get_costs() failed", exc_info=True)
        return None


def _normalise_sources(researcher: Any) -> list[dict[str, str]]:
    """The run's sources as [{url, title}], de-duplicated by URL.

    Reads ``get_research_sources()`` (the library's per-source
    records); falls back to ``get_source_urls()`` when the records
    carry nothing usable.
    """
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        records = researcher.get_research_sources() or []
    except Exception:  # noqa: BLE001 — source reporting must never fail a completed run
        records = []
    for record in records:
        if not isinstance(record, dict):
            continue
        url = str(record.get("url") or record.get("href") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        sources.append({"url": url, "title": str(record.get("title") or "").strip()})
    if not sources:
        try:
            urls = researcher.get_source_urls() or []
        except Exception:  # noqa: BLE001 — as above
            urls = []
        for url in urls:
            url = str(url).strip()
            if url and url not in seen:
                seen.add(url)
                sources.append({"url": url, "title": ""})
    return sources


def _artifact_filename(query: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:48] or "query"
    digest = hashlib.sha1(query.encode("utf-8")).hexdigest()[:8]
    return f"deep_research_{slug}_{digest}.md"


#: Serialises runs (see the module docstring: the Tavily key and
#: the Ollama base URL are scoped through the process environment —
#: the only channels the library reads for them).
_run_lock = asyncio.Lock()


async def run_deep_research(
    query: str,
    *,
    runtime: DeepResearchRuntime | None = None,
    researcher_factory: Callable[..., Any] | None = None,
    provider_factory: Callable[..., Any] | None = None,
    save_artifact_fn: Callable[..., str] | None = None,
) -> DeepResearchResult:
    """Run one deep-research delegation and return its outcome.

    Never raises: every failure is an ``ok=False`` result whose
    ``error`` names what failed — the tool-surface doctrine the web
    tools follow. ``researcher_factory`` / ``provider_factory`` /
    ``save_artifact_fn`` are the test seams; production passes none.
    """
    async with _run_lock:
        return await _run_deep_research_locked(
            query,
            runtime=runtime,
            researcher_factory=researcher_factory,
            provider_factory=provider_factory,
            save_artifact_fn=save_artifact_fn,
        )


async def _run_deep_research_locked(
    query: str,
    *,
    runtime: DeepResearchRuntime | None,
    researcher_factory: Callable[..., Any] | None,
    provider_factory: Callable[..., Any] | None,
    save_artifact_fn: Callable[..., str] | None,
) -> DeepResearchResult:
    query = (query or "").strip()
    if not query:
        return DeepResearchResult(
            query=query, ok=False, error="deep_research needs a non-empty query.")

    runtime = runtime if runtime is not None else _runtime()
    try:
        settings = _resolve_settings(runtime, provider_factory=provider_factory)
    except Exception as exc:  # noqa: BLE001 — resolution failure is the run's honest error result
        return DeepResearchResult(
            query=query, ok=False, error=f"deep_research is not configured to run: {exc}")

    if researcher_factory is None:
        try:
            from gpt_researcher import GPTResearcher
        except ImportError as exc:
            return DeepResearchResult(
                query=query, ok=False,
                error=f"deep_research is unavailable: the gpt-researcher package is not installed ({exc}).")
        researcher_factory = GPTResearcher

    retriever_note = (
        "Tavily retriever (key configured)"
        if settings.tavily_api_key
        else "fallback retriever over Aether's search_web/fetch_url tools "
             "(no Tavily key configured — lower depth)"
    )
    if settings.budget_usd > 0:
        budget_note = (
            f"Budget ${settings.budget_usd:.2f} per run, judged from the "
            "library's recorded costs between its research and report stages.")
    else:
        budget_note = (
            "No cost budget configured; the run was bounded by stage caps "
            f"(max {settings.max_iterations} research iterations) only.")

    config_path: str | None = None
    old_tavily_key = os.environ.get("TAVILY_API_KEY")
    # Scoped before construction: the library reads OLLAMA_BASE_URL
    # when its Memory is first built, which can happen inside the
    # factory call itself. Only set when unset — an explicitly
    # configured environment value is the owner's and wins.
    scoped_ollama_base = (
        settings.embedding_base_url
        if settings.embedding_base_url and "OLLAMA_BASE_URL" not in os.environ
        else None)
    if scoped_ollama_base is not None:
        os.environ["OLLAMA_BASE_URL"] = scoped_ollama_base
    try:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", prefix="aether_gptr_config_",
                delete=False, encoding="utf-8") as handle:
            json.dump(build_library_config(settings), handle)
            config_path = handle.name
        researcher = researcher_factory(
            query=query, report_type="research_report",
            config_path=config_path, verbose=False)
        # The Config has been read by now; the per-run file's job is done.
        os.unlink(config_path)
        config_path = None

        if not settings.tavily_api_key:
            # Fallback path: replace the resolved retriever classes
            # with Aether's own (the instance attribute every search
            # path in the library reads — see the module docstring).
            researcher.retrievers = [AetherWebRetriever]
        elif settings.tavily_api_key:
            os.environ["TAVILY_API_KEY"] = settings.tavily_api_key

        await researcher.conduct_research()
        cost = _safe_costs(researcher)
        sources = _normalise_sources(researcher)

        if (settings.budget_usd > 0 and cost is not None
                and cost > settings.budget_usd):
            return DeepResearchResult(
                query=query, ok=True,
                report=(
                    "Deep research stopped before the report stage: the "
                    f"research stage already cost ${cost:.4f}, over the "
                    f"${settings.budget_usd:.2f} per-run budget. "
                    f"{len(sources)} source(s) were gathered (listed in "
                    "the sources); narrow the question or raise "
                    "deep_research_cost_budget_usd to get a full report."),
                sources=sources,
                retriever=settings.retriever_name,
                retriever_note=retriever_note,
                cost_usd=cost,
                budget_note=budget_note + " The budget stopped this run.",
            )

        report = await researcher.write_report()
        cost = _safe_costs(researcher)
        if not sources:
            sources = _normalise_sources(researcher)
    except Exception as exc:
        logger.warning("Deep research run failed for %r", query, exc_info=True)
        return DeepResearchResult(
            query=query, ok=False,
            error=f"deep_research run failed: {exc}")
    finally:
        if config_path is not None:
            try:
                os.unlink(config_path)
            except OSError:
                pass
        if settings.tavily_api_key:
            if old_tavily_key is None:
                os.environ.pop("TAVILY_API_KEY", None)
            else:
                os.environ["TAVILY_API_KEY"] = old_tavily_key
        if scoped_ollama_base is not None:
            os.environ.pop("OLLAMA_BASE_URL", None)

    artifact_path: str | None = None
    task_id = ""
    try:
        from aether_engine.cost.ledger import get_usage_context

        task_id = str(get_usage_context().get("task_id") or "")
    except Exception:  # noqa: BLE001 — attribution is best-effort; the report still returns
        task_id = ""
    if task_id and report:
        saver = save_artifact_fn
        if saver is None:
            from aether_engine.artifacts.manager import save_artifact

            saver = save_artifact
        try:
            artifact_path = saver(
                task_id, "research", _artifact_filename(query), report)
        except Exception:
            logger.warning("Deep research artifact save failed", exc_info=True)

    return DeepResearchResult(
        query=query, ok=True,
        report=report,
        sources=sources,
        retriever=settings.retriever_name,
        retriever_note=retriever_note,
        cost_usd=cost,
        budget_note=budget_note,
        artifact_path=artifact_path,
    )


# ── Delegation-log integration ──────────────────────────────────────


def delegation_log_entry(
    payload: dict[str, Any], agent: str | None,
) -> dict[str, Any] | None:
    """The delegation-log entry for one deep_research tool result.

    Called by ``execute_tool_node`` with the tool's parsed JSON
    payload. The entry carries no ``decision`` key, so every existing
    reader of the log (which keys on ``decision.next``) skips it —
    the same shape as the P2c offered-vs-called entries. Returns
    None for anything that is not a successful run: a failed call
    leaves no sources to record.
    """
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return None
    return {
        "agent": agent or "researcher",
        "timestamp": time.time(),
        "deep_research": {
            "query": payload.get("query", ""),
            "retriever": payload.get("retriever", ""),
            "sources": payload.get("sources", []),
            "cost_usd": payload.get("cost_usd"),
            "artifact_path": payload.get("artifact_path"),
        },
    }


# ── The registry tool ───────────────────────────────────────────────


def create_deep_research_tool() -> Tool:
    """The ``deep_research`` tool (P4c).

    READ_ONLY: the backend reads the web and writes only the task's
    own artifact record, so the researcher specialist's P2c ceiling
    (read-only) admits it — that admission is the slice's contract:
    depth becomes a delegation decision the researcher makes, with
    the heavy engine behind the same trust pipeline as every other
    tool.
    """

    async def _deep_research(query: str) -> str:
        result = await run_deep_research(query)
        return json.dumps(result.to_tool_payload(), ensure_ascii=False)

    return Tool(
        name=DEEP_RESEARCH_TOOL_NAME,
        description=(
            "Run deep, multi-pass web research on one question with the "
            "GPT Researcher engine: it plans sub-questions, searches and "
            "reads multiple sources, and writes a cited report. Use it "
            "for questions that need synthesis across many sources — it "
            "takes minutes and costs real model spend, so use search_web "
            "and fetch_url for quick lookups instead. Returns a JSON "
            "object with the report, the source list, which retriever "
            "ran, and the run's recorded cost."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The research question, stated in full — the engine plans its own sub-questions from it.",
                },
            },
            "required": ["query"],
        },
        execute_fn=_deep_research,
        risk=ToolRisk.READ_ONLY,
        timeout_s=900,
        max_output_chars=262144,
    )
