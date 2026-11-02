"""R2-NEW-1 — reachability probes must not pollute the usage ledger.

The M19 fix (``8bd1c26``) deleted the ledger's context-less drop and
added GeneratorExit early-close recording in ``LiteLLMProvider`` —
which made model REACHABILITY PROBES start writing estimated
``system`` rows: ``check_single_model`` runs a real ``call_stream``
probe per model and returns after the first chunk, abandoning the
generator, whose close path then records the probe as if it were
agent work. Probes run on every forced discovery refresh and in the
weekly capability jobs, so the cost totals M19/M20 exist to keep
honest silently filled with non-agent traffic. (The capability
self-test suite is the same class of traffic and was likewise
dropped pre-M19 by design.)

Fix: an explicit recording opt-out on the provider —
``LiteLLMProvider(record_usage=False)`` — used by the probe
constructions. Real task usage recording is unchanged, including
the early-close path for genuine calls (pinned by the control test
below and by tests/test_m19_cost_coverage.py).
"""

from __future__ import annotations

from types import SimpleNamespace

import litellm
import pytest

from aether_engine.cost import ledger as ledger_module
from aether_engine.cost.ledger import UsageLedger
from aether_engine.models.reachability import ModelReachabilityChecker
from aether_engine.providers.litellm_provider import LiteLLMProvider


@pytest.fixture
def global_ledger(tmp_path, monkeypatch):
    """Point the process-wide ledger (the one the recording hook
    uses) at a throwaway database."""
    led = UsageLedger(tmp_path / "usage.db")
    monkeypatch.setattr(ledger_module, "GLOBAL_USAGE_LEDGER", led)
    return led


def _chunk(text=None, finish=None, usage=None):
    delta = SimpleNamespace(
        content=text, reasoning_content=None, thinking=None, tool_calls=None)
    chunk = SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])
    if usage is not None:
        chunk.usage = usage
    return chunk


def _usage(prompt_tokens, completion_tokens):
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c


def _fake_acompletion(text="Hello world"):
    async def fake_acompletion(**kwargs):
        return _Stream([
            _chunk(text, finish="stop", usage=_usage(1000, 500)),
        ])

    return fake_acompletion


def _provider(**overrides):
    kwargs = {
        "api_key": "test-key",
        "model": "gemini-3.5-flash",
        "provider_name": "google_gemini",
    }
    kwargs.update(overrides)
    return LiteLLMProvider(**kwargs)


async def _take_first_chunk_then_abandon(provider):
    """The reachability pattern: read one chunk, abandon the stream."""
    stream = provider.call_stream([{"role": "user", "content": "hi"}])
    first = await stream.__anext__()
    await stream.aclose()
    return first


class TestProbeRecordingOptOut:
    async def test_probe_provider_early_close_writes_no_row(
            self, global_ledger, monkeypatch):
        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion())
        provider = _provider(record_usage=False)
        first = await _take_first_chunk_then_abandon(provider)
        assert first.text == "Hello world"
        assert global_ledger.query()["entries"] == []

    async def test_probe_provider_full_consumption_writes_no_row(
            self, global_ledger, monkeypatch):
        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion())
        provider = _provider(record_usage=False)
        chunks = [c async for c in provider.call_stream(
            [{"role": "user", "content": "hi"}])]
        assert [c.text for c in chunks if c.text] == ["Hello world"]
        assert global_ledger.query()["entries"] == []

    async def test_default_provider_early_close_still_records(
            self, global_ledger, monkeypatch):
        """Control: M19's early-close recording for REAL calls is
        unchanged — only the explicit opt-out silences recording."""
        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion())
        provider = _provider()
        first = await _take_first_chunk_then_abandon(provider)
        assert first.text == "Hello world"
        entries = global_ledger.query()["entries"]
        assert len(entries) == 1
        assert entries[0]["task_class"] == "system"


class TestReachabilityProbeEndToEnd:
    async def test_check_single_model_writes_no_ledger_row(
            self, global_ledger, monkeypatch, tmp_path):
        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion())
        checker = ModelReachabilityChecker(db_path=tmp_path / "reach.db")
        reachable = await checker.check_single_model(
            "google_gemini", "gemini-3.5-flash", "test-key", force=True)
        assert reachable is True
        # The probe abandons its stream after the first chunk; the
        # abandoned generator is closed by the event loop's asyncgen
        # finaliser, which is the moment the pre-fix code recorded
        # the probe's row. Flush finalisers before asserting.
        import asyncio
        import gc

        gc.collect()
        for _ in range(5):
            await asyncio.sleep(0)
            gc.collect()
        # The probe really ran (its own reachability cache recorded
        # the result) — but the usage ledger stays clean.
        assert checker.get_cached_reachability(
            "google_gemini", "gemini-3.5-flash") is not None
        assert global_ledger.query()["entries"] == []
