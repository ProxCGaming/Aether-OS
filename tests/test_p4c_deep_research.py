"""P4c — GPT Researcher backend: the ``deep_research`` tool.

The plan's test shape (step 3): recorded-fixture tests with a stubbed
retriever + LLM, no live API calls anywhere in this file.

Layers covered:
  * the fallback retriever over Aether's web tools (parse, fetch,
    domain filter, failure honesty);
  * settings resolution (pin vs default, Tavily key selection,
    embedding mapping) and the library config the wrapper builds;
  * the run wrapper against a fake researcher (success, budget stop,
    unavailable library, delegation-log entry shape);
  * registration: the tool is in the registry as READ_ONLY and the
    researcher's P2c ceiling admits it;
  * execute_tool_node recording a successful run's sources in the
    delegation log;
  * one REAL GPT Researcher run, end to end, with the LLM stubbed at
    the library's provider funnel, the web stubbed at Aether's tool
    bodies, and constant embeddings — the spike's proof that the
    wrapper's config + retriever swap drive the actual library.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import ClassVar

import pytest

from aether_engine.config import UserConfig
from aether_engine.langgraph.backends import researcher_gpt
from aether_engine.langgraph.backends.researcher_gpt import (
    AetherWebRetriever,
    DeepResearchRuntime,
    build_library_config,
    create_deep_research_tool,
    delegation_log_entry,
    run_deep_research,
)

SEARCH_TEXT = (
    "Title: Alpha Page\nURL: https://example.com/alpha\nSnippet: alpha snippet\n"
    "\n"
    "Title: Beta Page\nURL: https://other.org/beta\nSnippet: beta snippet\n"
)


def _fetch_ok(url: str) -> str:
    return (
        f"[fetch_url: {url} | title: Fetched {url}]\n"
        f"Full text read from {url}. Fixture body."
    )


class _FakeSecretStore:
    """load_provider over a dict; a missing name raises, like the
    real store's ProviderNotFoundError path."""

    def __init__(self, secrets: dict | None = None):
        self._secrets = dict(secrets or {})

    def load_provider(self, name: str) -> str:
        if name not in self._secrets:
            raise KeyError(name)
        return self._secrets[name]


def _fake_provider(provider_name="google_gemini", model="gemini-2.5-flash"):
    return SimpleNamespace(
        provider_name=provider_name,
        model=model,
        litellm_model=f"gemini/{model}",
        api_key="test-provider-key",
        base_url=None,
    )


def _runtime(user_config=None, secrets=None) -> DeepResearchRuntime:
    return DeepResearchRuntime(
        user_config=user_config or UserConfig(
            default_provider="google_gemini", default_model="gemini-2.5-flash"),
        secret_store=_FakeSecretStore(secrets),
    )


# ── Fallback retriever ──────────────────────────────────────────────


class TestAetherWebRetriever:
    def test_search_fetches_full_text_and_cites_final_url(self):
        retriever = AetherWebRetriever(
            "fixture query",
            search_fn=lambda q, n: SEARCH_TEXT,
            fetch_fn=_fetch_ok,
        )
        results = retriever.search(max_results=5)
        assert [r["url"] for r in results] == [
            "https://example.com/alpha", "https://other.org/beta"]
        assert all("Full text read from" in r["raw_content"] for r in results)
        assert results[0]["title"] == "Fetched https://example.com/alpha"
        assert AetherWebRetriever.requires_scraping is False

    def test_failed_fetch_skips_the_page_never_fakes_content(self):
        def fetch(url: str) -> str:
            if "alpha" in url:
                return "Error: fetching 'https://example.com/alpha' returned HTTP 500."
            return _fetch_ok(url)

        retriever = AetherWebRetriever(
            "q", search_fn=lambda q, n: SEARCH_TEXT, fetch_fn=fetch)
        results = retriever.search()
        assert [r["url"] for r in results] == ["https://other.org/beta"]

    def test_query_domains_filter(self):
        retriever = AetherWebRetriever(
            "q", query_domains=["example.com"],
            search_fn=lambda q, n: SEARCH_TEXT, fetch_fn=_fetch_ok)
        assert [r["url"] for r in retriever.search()] == [
            "https://example.com/alpha"]

    def test_failed_or_empty_search_returns_nothing(self):
        for raw in ("Search failed: boom", "No results found.", ""):
            retriever = AetherWebRetriever(
                "q", search_fn=lambda q, n, raw=raw: raw, fetch_fn=_fetch_ok)
            assert retriever.search() == []


# ── Settings + library config ───────────────────────────────────────


class TestSettingsResolution:
    def test_default_provider_resolution(self):
        settings = researcher_gpt._resolve_settings(
            _runtime(), provider_factory=lambda *a, **k: _fake_provider())
        assert settings.provider_name == "google_gemini"
        assert settings.litellm_model == "gemini/gemini-2.5-flash"
        assert settings.retriever_name == "aether_web"  # no Tavily key stored

    def test_researcher_pin_wins_over_default(self):
        config = UserConfig(
            default_provider="google_gemini", default_model="gemini-2.5-flash",
            agent_models={"researcher": "openai:gpt-5"})
        seen = {}

        def factory(p_name, m_name, store, **kwargs):
            seen["call"] = (p_name, m_name)
            return _fake_provider(p_name, m_name)

        settings = researcher_gpt._resolve_settings(
            _runtime(config), provider_factory=factory)
        assert seen["call"] == ("openai", "gpt-5")
        assert settings.litellm_model == "gemini/gpt-5"  # the fake's shape

    def test_tavily_key_selects_tavily(self):
        settings = researcher_gpt._resolve_settings(
            _runtime(secrets={"tavily": "tvly-test-key"}),
            provider_factory=lambda *a, **k: _fake_provider())
        assert settings.retriever_name == "tavily"
        assert build_library_config(settings)["RETRIEVER"] == "tavily"

    def test_library_config_routes_all_stages_through_litellm(self):
        settings = researcher_gpt._resolve_settings(
            _runtime(), provider_factory=lambda *a, **k: _fake_provider())
        config = build_library_config(settings)
        for stage in ("FAST_LLM", "SMART_LLM", "STRATEGIC_LLM"):
            assert config[stage] == "litellm:gemini/gemini-2.5-flash"
        assert config["LLM_KWARGS"]["api_key"] == "test-provider-key"
        assert config["RETRIEVER"] == "custom"  # placeholder; swapped at run time
        assert config["EMBEDDING"].startswith("ollama:")

    def test_budget_field_round_trips(self):
        config = UserConfig(deep_research_cost_budget_usd=2.5)
        restored = UserConfig.from_dict(config.to_dict())
        assert restored.deep_research_cost_budget_usd == 2.5
        assert UserConfig.from_dict({}).deep_research_cost_budget_usd == 0.0


# ── The run wrapper, fake researcher ────────────────────────────────


class _FakeResearcher:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.retrievers = ["placeholder"]
        self.conducted = False
        self.report_written = False
        self.cost = 0.25

    async def conduct_research(self):
        self.conducted = True
        return "context"

    async def write_report(self):
        self.report_written = True
        return "# Fixture Report\n\nFindings."

    def get_costs(self):
        return self.cost

    def get_research_sources(self):
        return [
            {"url": "https://example.com/alpha", "title": "Alpha Page"},
            {"url": "https://example.com/alpha", "title": "Alpha Page"},
            {"url": "https://other.org/beta", "title": "Beta Page"},
        ]

    def get_source_urls(self):
        return ["https://example.com/alpha", "https://other.org/beta"]


def _holder_factory(holder: list, cls=_FakeResearcher):
    def factory(**kwargs):
        instance = cls(**kwargs)
        holder.append(instance)
        return instance

    return factory


@pytest.mark.asyncio
async def test_run_success_payload_and_artifact():
    from aether_engine.cost.ledger import usage_context

    holder: list = []
    saved: list = []
    with usage_context(task_id="task-42"):
        result = await run_deep_research(
            "fixture question",
            runtime=_runtime(),
            researcher_factory=_holder_factory(holder),
            provider_factory=lambda *a, **k: _fake_provider(),
            save_artifact_fn=lambda tid, kind, name, content: saved.append(
                (tid, kind, name, content)) or "/artifacts/deep.md",
        )
    assert result.ok
    assert result.report.startswith("# Fixture Report")
    assert result.retriever == "aether_web"
    # The fallback retriever replaced the library's resolved list.
    assert holder[0].retrievers == [AetherWebRetriever]
    # Sources de-duplicated by URL.
    assert [s["url"] for s in result.sources] == [
        "https://example.com/alpha", "https://other.org/beta"]
    assert result.cost_usd == 0.25
    assert result.artifact_path == "/artifacts/deep.md"
    assert saved and saved[0][0] == "task-42" and saved[0][1] == "research"
    payload = result.to_tool_payload()
    assert payload["status"] == "ok"
    assert payload["sources"] == result.sources
    assert "stage caps" in payload["budget_note"]  # no budget configured: says so


@pytest.mark.asyncio
async def test_run_budget_stop_skips_the_report_stage():
    holder: list = []

    def factory(**kwargs):
        instance = _FakeResearcher(**kwargs)
        instance.cost = 5.0
        holder.append(instance)
        return instance

    config = UserConfig(
        default_provider="google_gemini", default_model="gemini-2.5-flash",
        deep_research_cost_budget_usd=1.0)
    result = await run_deep_research(
        "expensive question",
        runtime=_runtime(config),
        researcher_factory=factory,
        provider_factory=lambda *a, **k: _fake_provider(),
        save_artifact_fn=lambda *a: "/unused",
    )
    assert result.ok
    assert holder[0].conducted and not holder[0].report_written
    assert "stopped before the report stage" in result.report
    assert result.cost_usd == 5.0
    assert len(result.sources) == 2  # the gathered sources still recorded


@pytest.mark.asyncio
async def test_run_empty_query_and_broken_factory_are_honest_errors():
    result = await run_deep_research(
        "  ", runtime=_runtime(),
        provider_factory=lambda *a, **k: _fake_provider())
    assert not result.ok and "non-empty" in result.error

    def broken_factory(**kwargs):
        raise RuntimeError("library exploded")

    result = await run_deep_research(
        "fixture question",
        runtime=_runtime(),
        researcher_factory=broken_factory,
        provider_factory=lambda *a, **k: _fake_provider(),
    )
    assert not result.ok and "library exploded" in result.error
    assert result.to_tool_payload()["status"] == "error"


def test_delegation_log_entry_shape():
    payload = {
        "status": "ok", "query": "q", "retriever": "aether_web",
        "sources": [{"url": "https://example.com/alpha", "title": "Alpha"}],
        "cost_usd": 0.25, "artifact_path": "/artifacts/deep.md",
    }
    entry = delegation_log_entry(payload, "researcher")
    assert entry is not None
    assert "decision" not in entry  # existing log readers key on it and skip
    assert entry["deep_research"]["sources"] == payload["sources"]
    assert entry["deep_research"]["retriever"] == "aether_web"
    assert delegation_log_entry({"status": "error"}, "researcher") is None


# ── Registration + ceiling ──────────────────────────────────────────


def test_tool_registered_read_only_and_admitted_by_researcher_ceiling():
    from aether_engine.tools.ceilings import apply_tool_ceiling
    from aether_engine.tools.registry import ToolRisk, build_tool_registry

    registry = build_tool_registry()
    tool = registry.get_tool("deep_research")
    assert tool.risk == ToolRisk.READ_ONLY
    assert "deep_research" in registry.read_only_names()

    definitions = registry.get_definitions()
    risk_by_name = {t.name: t.risk for t in registry.get_tools()}
    offered = apply_tool_ceiling("researcher", definitions, risk_by_name)
    offered_names = [d["function"]["name"] for d in offered]
    assert "deep_research" in offered_names
    # The ceiling's headline guarantee still holds around the new tool.
    assert "write_file" not in offered_names
    assert "delete_file" not in offered_names


def test_tool_definition_shape():
    tool = create_deep_research_tool()
    definition = tool.to_definition()
    assert definition["function"]["name"] == "deep_research"
    assert definition["function"]["parameters"]["required"] == ["query"]


# ── execute_tool_node delegation-log integration ────────────────────


@pytest.mark.asyncio
async def test_execute_tool_node_records_deep_research_sources():
    from aether_engine.langgraph.nodes.execute_tool import execute_tool_node

    payload = {
        "status": "ok", "query": "fixture question", "retriever": "aether_web",
        "report": "# Fixture Report",
        "sources": [{"url": "https://example.com/alpha", "title": "Alpha Page"}],
        "cost_usd": 0.25, "budget_note": "note", "artifact_path": "/a/deep.md",
    }

    class _Registry:
        async def execute(self, name, args, **kwargs):
            return json.dumps(payload)

    state = {
        "messages": [],
        "pending_tool_call": {
            "name": "deep_research", "args": {"query": "fixture question"},
            "call_id": "c1",
        },
        "active_specialist": "researcher",
        "tool_call_signature": None,
        "tool_call_repeats": 0,
    }
    config = {"configurable": {
        "tool_registry": _Registry(), "agent_tools": {}, "user_config": None}}
    update = await execute_tool_node(state, config)
    entries = update.get("delegation_log") or []
    assert len(entries) == 1
    assert entries[0]["deep_research"]["sources"] == payload["sources"]
    assert entries[0]["agent"] == "researcher"


# ── Real library, stubbed LLM + web (the spike's proof) ─────────────


class _ConstantEmbeddings:
    """LangChain Embeddings double: identical vectors, so the
    library's similarity filter keeps every fetched document."""

    def embed_documents(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0, 0.0, 0.0]


class _StubLLMProvider:
    """Prompt-aware stand-in at the library's provider funnel."""

    last_usage_metadata = None
    last_response_metadata: ClassVar[dict] = {}

    async def get_chat_response(self, messages, stream=False, websocket=None, **kwargs):
        text = "\n".join(
            str(m.get("content", "")) for m in messages).lower()
        if "search queries" in text or "sub-queries" in text or "sub queries" in text:
            return '["stub sub-question one", "stub sub-question two"]'
        if "agent" in text and "role" in text:
            return '{"agent": "Stub Agent", "role": "stub researcher"}'
        return (
            "Stub synthesis: the fixture topic is documented by the "
            "stub sources, which agree on the fixture facts."
        )


@pytest.mark.asyncio
async def test_real_library_run_with_stubbed_llm_and_web(monkeypatch):
    from gpt_researcher import GPTResearcher
    from gpt_researcher.llm_provider import GenericLLMProvider

    # Environment hygiene for this VM only: the ollama package (a
    # library transitive dep, imported when the Memory constructs
    # the configured Ollama embeddings) builds an httpx client at
    # import time, and httpx rejects this VM's bracketed-IPv6
    # no_proxy entries ("Invalid port: ':1]'"). Sanitise no_proxy —
    # the proxy vars themselves stay, because the library's cost
    # accounting downloads its tiktoken encoding on first use.
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1")

    monkeypatch.setattr(
        GenericLLMProvider, "from_provider",
        classmethod(lambda cls, provider, **kw: _StubLLMProvider()))
    monkeypatch.setattr(researcher_gpt, "_default_search",
                        lambda q, n: SEARCH_TEXT)
    monkeypatch.setattr(researcher_gpt, "_default_fetch", _fetch_ok)

    def factory(**kwargs):
        instance = GPTResearcher(**kwargs)
        # The configured embedder is the owner's local Ollama, which
        # this test must not call; swap in the constant double on the
        # constructed researcher (the wrapper's factory seam exists
        # for exactly this).
        instance.memory._embeddings = _ConstantEmbeddings()
        return instance

    result = await run_deep_research(
        "the fixture topic",
        runtime=_runtime(),
        researcher_factory=factory,
        provider_factory=lambda *a, **k: _fake_provider(),
        save_artifact_fn=lambda *a: "/artifacts/fixture.md",
    )
    assert result.ok, result.error
    assert result.retriever == "aether_web"
    assert result.report  # the library wrote a report from stub text
    source_urls = [s["url"] for s in result.sources]
    assert "https://example.com/alpha" in source_urls
    assert "https://other.org/beta" in source_urls
    assert isinstance(result.cost_usd, float)
