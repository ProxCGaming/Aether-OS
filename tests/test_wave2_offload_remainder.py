"""Wave 2 remainder — event-loop offload: N-11, N-13.

- N-11: per-call latency recording (LiteLLMProvider success path) and
  the reachability checker's cache read / result write are
  synchronous SQLite on the event loop; both must move off it
  (``asyncio.to_thread``, the E2-01 pattern) without changing what
  gets recorded. (The provider's GeneratorExit firing points cannot
  await by language rule and keep the inline recorder — the success
  path measured here is the every-call path the finding names.)
- N-13: the hub inventory's ``downloaded()`` runs a recursive HF
  cache walk synchronously inside an async method.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import aether_engine.providers.litellm_provider as litellm_provider_module
import aether_engine.routing.latency as latency_module
from aether_engine.models.hub_downloads import ModelHubDownloads
from aether_engine.models.reachability import ModelReachabilityChecker
from aether_engine.providers.litellm_provider import LiteLLMProvider
from aether_engine.routing.latency import LatencyStore


async def _probe_delay(coro, probe_s=0.05):
    task = asyncio.create_task(coro)
    start = time.monotonic()
    await asyncio.sleep(probe_s)
    delay = time.monotonic() - start
    result = await task
    return delay, result


# ---------------------------------------------------------------------------
# N-11 — reachability cache access off the loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reachability_cache_read_off_loop(tmp_path):
    checker = ModelReachabilityChecker(db_path=tmp_path / "reach.db")

    def slow_cached(provider, model_id):
        time.sleep(0.4)
        return (True, time.time())

    checker.get_cached_reachability = slow_cached
    delay, reachable = await _probe_delay(
        checker.check_single_model("prov", "model-x", "key"))
    assert reachable is True
    assert delay < 0.25, f"event loop stalled {delay:.2f}s by cache read"


@pytest.mark.asyncio
async def test_reachability_record_off_loop(tmp_path, monkeypatch):
    checker = ModelReachabilityChecker(db_path=tmp_path / "reach.db")
    original_record = checker.record_reachability

    def slow_record(*args, **kwargs):
        time.sleep(0.4)
        return original_record(*args, **kwargs)

    checker.record_reachability = slow_record

    class _FakeProvider:
        def __init__(self, **kwargs):
            pass

        async def call_stream(self, messages, **kwargs):
            yield SimpleNamespace(text="hi")

    monkeypatch.setattr(litellm_provider_module, "LiteLLMProvider", _FakeProvider)
    delay, reachable = await _probe_delay(
        checker.check_single_model("prov", "model-x", "key"))
    assert reachable is True
    assert delay < 0.25, f"event loop stalled {delay:.2f}s by reachability write"
    assert checker.get_cached_reachability("prov", "model-x") is not None


# ---------------------------------------------------------------------------
# N-11 — provider latency recording off the loop (success path)
# ---------------------------------------------------------------------------

def _chunk(text=None, finish=None, usage=None):
    delta = SimpleNamespace(
        content=text, reasoning_content=None, thinking=None, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish)],
        usage=usage)


class _Stream:
    def __init__(self, chunks, delay=0.01):
        self._chunks = chunks
        self._delay = delay

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c
            await asyncio.sleep(self._delay)


@pytest.mark.asyncio
async def test_latency_recording_off_loop_on_success_path(
        tmp_path, monkeypatch):
    import litellm

    store = LatencyStore(tmp_path / "latency.db")
    monkeypatch.setattr(latency_module, "GLOBAL_LATENCY_STORE", store)
    real_record = latency_module.record_latency_sample

    def slow_record(**kwargs):
        time.sleep(0.4)
        return real_record(**kwargs)

    monkeypatch.setattr(latency_module, "record_latency_sample", slow_record)

    usage = SimpleNamespace(prompt_tokens=5, completion_tokens=10)

    async def fake_acompletion(**kwargs):
        return _Stream([
            _chunk(text="hello"),
            _chunk(text=" more"),
            _chunk(finish="stop", usage=usage),
        ])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    provider = LiteLLMProvider(
        api_key="sk-test", model="gpt-main", provider_name="openai")

    async def _drain():
        return [c async for c in provider.call_stream(
            [{"role": "user", "content": "hello"}],
            tools=None, node_name="test")]

    delay, chunks = await _probe_delay(_drain())
    assert "".join(c.text or "" for c in chunks) == "hello more"
    assert delay < 0.25, f"event loop stalled {delay:.2f}s by latency recording"
    # The sample still landed, keyed to the serving model.
    summary = store.summary("openai", "gpt-main")
    assert summary is not None and summary.samples == 1


# ---------------------------------------------------------------------------
# N-13 — hub inventory HF scan off the loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hub_downloaded_scan_off_loop():
    class _OllamaStub:
        async def list_downloaded_models(self):
            return []

    manager = ModelHubDownloads(
        hub=SimpleNamespace(), ollama=_OllamaStub())

    def slow_scan():
        time.sleep(0.4)
        return []

    manager._scan_hf_speech_cache = slow_scan
    delay, result = await _probe_delay(manager.downloaded())
    assert result == {"models": [], "error": None}
    assert delay < 0.25, f"event loop stalled {delay:.2f}s by HF cache scan"
