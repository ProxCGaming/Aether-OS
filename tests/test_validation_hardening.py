"""Test-button hardening: validation dedupe + time budget.

The bug (reported 31 Oct 2026, root-caused to the Oct 2026 hang):
one Test click ran discovery + up to 5 sequential pings inline in
the websocket message loop — up to ~60s during which the whole app
looked frozen, and a double-click queued a second full scan behind
the first. Fixes pinned here:

- ``validate_provider_key_shared`` runs ONE validation per
  provider+key; concurrent duplicates attach to it, and the cache
  entry is dropped on completion so a later Test re-validates.
- ``_validate_provider_key`` is hard-capped by ``budget_s`` and
  returns an honest timeout result instead of running on silently.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from aether_engine.orchestration import core


@pytest.fixture(autouse=True)
def _clean_inflight():
    core._inflight_validations.clear()
    yield
    core._inflight_validations.clear()


def test_duplicate_tests_share_one_validation(monkeypatch):
    calls = {"n": 0}

    async def fake_validate(provider_name, api_key, base_url=None, persist_models=True, budget_s=30.0):
        calls["n"] += 1
        await asyncio.sleep(0.15)
        return {"status": "connected", "message": "ok", "models": ["m1"]}

    monkeypatch.setattr(core, "_validate_provider_key", fake_validate)

    async def run():
        return await asyncio.gather(
            core.validate_provider_key_shared("google_gemini", "k1"),
            core.validate_provider_key_shared("google_gemini", "k1"),
            core.validate_provider_key_shared("google_gemini", "k1"),
        )

    results = asyncio.run(run())
    assert calls["n"] == 1  # three clicks, one scan
    assert all(r["status"] == "connected" for r in results)

    # After completion the entry is gone: a fresh Test re-validates.
    asyncio.run(core.validate_provider_key_shared("google_gemini", "k1"))
    assert calls["n"] == 2


def test_different_key_is_a_different_validation(monkeypatch):
    calls = {"n": 0}

    async def fake_validate(provider_name, api_key, base_url=None, persist_models=True, budget_s=30.0):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return {"status": "connected", "message": f"key {api_key}"}

    monkeypatch.setattr(core, "_validate_provider_key", fake_validate)

    async def run():
        return await asyncio.gather(
            core.validate_provider_key_shared("google_gemini", "k1"),
            core.validate_provider_key_shared("google_gemini", "k2"),
        )

    results = asyncio.run(run())
    assert calls["n"] == 2
    assert {r["message"] for r in results} == {"key k1", "key k2"}


def test_validation_budget_returns_timeout_result(monkeypatch):
    async def hanging_inner(provider_name, api_key, base_url=None, persist_models=True):
        await asyncio.sleep(60)
        return {"status": "connected"}  # pragma: no cover — never reached

    monkeypatch.setattr(core, "_validate_provider_key_inner", hanging_inner)

    start = time.monotonic()
    result = asyncio.run(core._validate_provider_key("google_gemini", "k1", budget_s=0.2))
    elapsed = time.monotonic() - start

    assert elapsed < 5  # honoured the budget, not the 60s hang
    assert result["status"] == "network_error"
    assert "timed out" in result["message"]
    assert result["models"] == [] and result["excluded_models"] == []
