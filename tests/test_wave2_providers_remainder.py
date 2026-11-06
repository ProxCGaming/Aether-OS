"""Wave 2 (Builder I) — providers/supervisor/local-models remainder.

- E5-O [S4] Retriable-except tuple degrades to catch-all if a LiteLLM
  name is absent — ``providers/litellm_provider.py:905``:
  ``getattr(litellm, "MidStreamFallbackError", Exception)`` puts
  ``Exception`` itself in the tuple on a LiteLLM without that name.
- N-24 [S3] Generic-exception 429 path retains the exact last-model
  bug fixed for typed errors — ``providers/litellm_provider.py:938-951``.
- N-22 [S3] Supervisor adapter swallows the first typed provider
  failure and retries unconditionally —
  ``langgraph/supervisor_model.py:319-352``.
- N-10 [S2] Synchronous multi-GB file copy inside async Ollama
  download — ``models/local_models.py:149,332-333``.
- E5-R [S4] Dead variable + glob built from unvalidated model name in
  manifest lookup — ``models/local_models.py`` ``_find_manifest``.
"""

import json
import threading
from types import SimpleNamespace

import litellm
import pytest
from langchain_core.messages import HumanMessage
from langgraph.config import var_child_runnable_config

from aether_engine.cost import ledger as ledger_module
from aether_engine.cost.ledger import UsageLedger
from aether_engine.langgraph.supervisor_model import AetherSupervisorModel
from aether_engine.models import local_models
from aether_engine.models.local_models import OllamaManager
from aether_engine.providers.base import (
    AuthenticationError,
    ProviderError,
    RateLimitError,
)
from aether_engine.providers.litellm_provider import (
    FallbackModel,
    LiteLLMProvider,
)


@pytest.fixture
def global_ledger(tmp_path, monkeypatch):
    led = UsageLedger(tmp_path / "usage.db")
    monkeypatch.setattr(ledger_module, "GLOBAL_USAGE_LEDGER", led)
    return led


def _chunk(text):
    delta = SimpleNamespace(
        content=text, reasoning_content=None, thinking=None,
        tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason="stop")])


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c


async def _drain(provider):
    return [c async for c in provider.call_stream(
        [{"role": "user", "content": "hi"}])]


# ---------------------------------------------------------------------------
# E5-O — the retriable tuple must never contain Exception itself
# ---------------------------------------------------------------------------


def test_e5o_retriable_tuple_without_midstream_name_is_exact():
    from aether_engine.providers.litellm_provider import (
        _retriable_litellm_errors,
    )
    stub = SimpleNamespace(
        RateLimitError=type("RLE", (Exception,), {}),
        Timeout=type("TO", (Exception,), {}),
        APIConnectionError=type("ACE", (Exception,), {}),
        ServiceUnavailableError=type("SUE", (Exception,), {}),
        # no MidStreamFallbackError attribute at all
    )
    errors = _retriable_litellm_errors(stub)
    assert Exception not in errors
    assert errors == (
        stub.RateLimitError, stub.Timeout,
        stub.APIConnectionError, stub.ServiceUnavailableError)


def test_e5o_retriable_tuple_includes_midstream_when_present():
    from aether_engine.providers.litellm_provider import (
        _retriable_litellm_errors,
    )
    mid = type("MSFE", (Exception,), {})
    stub = SimpleNamespace(
        RateLimitError=type("RLE", (Exception,), {}),
        Timeout=type("TO", (Exception,), {}),
        APIConnectionError=type("ACE", (Exception,), {}),
        ServiceUnavailableError=type("SUE", (Exception,), {}),
        MidStreamFallbackError=mid,
    )
    assert mid in _retriable_litellm_errors(stub)


async def test_e5o_missing_midstream_name_keeps_generic_classification(
        monkeypatch, global_ledger):
    """Behavioural: on a LiteLLM without MidStreamFallbackError, an
    unexpected non-LiteLLM error must reach the generic classification
    (ProviderError, retriable=False) — pre-fix the tuple's Exception
    member swallowed it as "retriable" and the call ended in the
    generic NetworkError instead."""
    monkeypatch.delattr(litellm, "MidStreamFallbackError", raising=False)

    async def fake_acompletion(**kwargs):
        raise ValueError("totally unexpected boom")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    provider = LiteLLMProvider(
        api_key="test-key", model="gemini-3.5-flash",
        provider_name="google_gemini")
    with pytest.raises(ProviderError) as excinfo:
        await _drain(provider)
    assert type(excinfo.value) is ProviderError
    assert excinfo.value.retriable is False
    assert "Unexpected LiteLLM error" in str(excinfo.value)


# ---------------------------------------------------------------------------
# N-24 — a generic 429 on the LAST fallback model keeps its
# RateLimitError classification (the S1-N5 rule, applied to the
# generic path too)
# ---------------------------------------------------------------------------


async def test_n24_generic_429_on_last_model_raises_rate_limit(
        monkeypatch, global_ledger):
    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            raise litellm.RateLimitError(
                "rate limited", "google_gemini", "gemini-3.5-flash")
        raise Exception("HTTP 429 Too Many Requests")  # noqa: TRY002 — the defect under test is precisely an untyped generic error

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    provider = LiteLLMProvider(
        api_key="test-key", model="gemini-3.5-flash",
        provider_name="google_gemini",
        fallback_models=[
            FallbackModel(model="gpt-4o-mini", api_key="test-key",
                          provider_name="openai"),
        ])
    with pytest.raises(RateLimitError):
        await _drain(provider)
    assert len(calls) == 2


async def test_n24_generic_error_on_last_model_stays_provider_error(
        monkeypatch, global_ledger):
    """Guard: a non-429 generic failure on the last model is still
    the honest non-retriable ProviderError, not a rate limit."""
    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            raise litellm.RateLimitError(
                "rate limited", "google_gemini", "gemini-3.5-flash")
        raise Exception("some opaque failure")  # noqa: TRY002 — the defect under test is precisely an untyped generic error

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    provider = LiteLLMProvider(
        api_key="test-key", model="gemini-3.5-flash",
        provider_name="google_gemini",
        fallback_models=[
            FallbackModel(model="gpt-4o-mini", api_key="test-key",
                          provider_name="openai"),
        ])
    with pytest.raises(ProviderError) as excinfo:
        await _drain(provider)
    assert type(excinfo.value) is ProviderError
    assert excinfo.value.retriable is False


# ---------------------------------------------------------------------------
# N-22 — a typed provider failure is not retried as a text call
# ---------------------------------------------------------------------------


class _AuthFailingProvider:
    def __init__(self):
        self.calls = 0

    async def call_stream(self, messages, tools=None, node_name=None,
                          **kwargs):
        self.calls += 1
        raise AuthenticationError("invalid api key")
        yield  # pragma: no cover — makes this an async generator


async def test_n22_typed_failure_is_not_retried():
    """A typed provider failure (auth) on the with-tools call is the
    graph/pump's signal: it propagates after ONE call. Pre-fix the
    adapter's blanket ``except Exception`` swallowed it and made a
    second, billed call with tools=None before propagating."""
    provider = _AuthFailingProvider()
    token = var_child_runnable_config.set(
        {"configurable": {"session_id": "test-session",
                          "provider": provider}})
    try:
        with pytest.raises(AuthenticationError):
            await AetherSupervisorModel()._agenerate(
                [HumanMessage(content="write a hello world script")])
    finally:
        var_child_runnable_config.reset(token)
    assert provider.calls == 1


# ---------------------------------------------------------------------------
# N-10 — the destination copy runs off the event loop
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, lines=None):
        self.status_code = status_code
        self._lines = lines or []
        self.text = ""

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class _StreamContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc_info):
        return False


_PULL_LINES = [
    json.dumps({"status": "pulling manifest"}),
    json.dumps({"status": "success"}),
]


class _HealthyClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url):
        return _FakeResponse(status_code=200)

    def stream(self, method, url, json=None):
        return _StreamContext(
            _FakeResponse(status_code=200, lines=_PULL_LINES))


async def test_n10_destination_copy_runs_off_the_event_loop(
        tmp_path, monkeypatch):
    monkeypatch.setattr(local_models.httpx, "AsyncClient", _HealthyClient)
    store = tmp_path / "daemon-store"
    manifest = {
        "config": {"digest": "sha256:" + "a" * 64},
        "layers": [{"digest": "sha256:" + "b" * 64}],
    }
    manifest_path = (
        store / "manifests/registry.ollama.ai/library/test-model/latest")
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest))
    blobs = store / "blobs"
    blobs.mkdir(parents=True)
    (blobs / ("sha256-" + "a" * 64)).write_bytes(b"cfg")
    (blobs / ("sha256-" + "b" * 64)).write_bytes(b"layer")

    mgr = OllamaManager()
    mgr.models_dir = store

    placement_threads = []
    original = OllamaManager._place_model_in_destination

    def spy(self, model_name, dest):
        placement_threads.append(threading.get_ident())
        return original(self, model_name, dest)

    monkeypatch.setattr(
        OllamaManager, "_place_model_in_destination", spy)

    loop_thread = threading.get_ident()
    events = [ev async for ev in mgr.download_model_task(
        model_name="test-model:latest",
        destination_dir=str(tmp_path / "models"),
        task_id="dl-n10",
    )]

    from aether_common.contracts import EventType
    assert events[-1].type == EventType.TASK_COMPLETED
    assert placement_threads, "the placement copy never ran"
    assert placement_threads[0] != loop_thread


# ---------------------------------------------------------------------------
# E5-R — manifest lookup: no glob metacharacter injection
# ---------------------------------------------------------------------------


def _manifest_store(tmp_path):
    store = tmp_path / "daemon-store"
    manifest_path = (
        store / "manifests/registry.ollama.ai/library/foo/latest")
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("{}")
    return store, manifest_path


def test_e5r_glob_metacharacters_in_model_name_do_not_match(tmp_path):
    """``f[o]o`` is a glob character class: pre-fix it matched the
    ``foo`` manifest, so a model that was never pulled answered as
    present in the store."""
    store, _ = _manifest_store(tmp_path)
    mgr = OllamaManager()
    mgr.models_dir = store
    assert mgr._find_manifest("f[o]o:latest") is None
    assert mgr._find_manifest("f*o:latest") is None


def test_e5r_plain_lookup_still_finds_the_manifest(tmp_path):
    """Guard: ordinary names (with and without an explicit tag) keep
    resolving exactly as before."""
    store, manifest_path = _manifest_store(tmp_path)
    mgr = OllamaManager()
    mgr.models_dir = store
    assert mgr._find_manifest("foo:latest") == manifest_path
    assert mgr._find_manifest("foo") == manifest_path
