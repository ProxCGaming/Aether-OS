"""FF3-07: the provider list carries per-model picker metadata.

The model pickers (chat, Settings default, aux, per-agent) used to
receive bare model id strings, even though the engine already holds
capability tags + costs (the curated benchmark cache) and a
learned-unusable record. ``_provider_model_meta`` exposes exactly
that existing data on each provider entry as ``model_meta`` — and
nothing else: a model with no engine-side data is absent from the
map, so the UI renders its metadata as absent, never invented.
"""
from types import SimpleNamespace

import pytest

from aether_engine.orchestration.discovery import (
    _build_provider_list_async,
    _provider_model_meta,
)


def test_benchmark_cache_fields_are_exposed():
    meta = _provider_model_meta(["gpt-4o"])
    entry = meta["gpt-4o"]
    assert "vision" in entry["capabilities"]
    assert "tools" in entry["capabilities"]
    assert entry["cost_input_1m"] == 2.50
    assert entry["cost_output_1m"] == 10.00
    assert "unusable" not in entry


def test_unknown_model_is_absent_not_invented():
    assert _provider_model_meta(["no-such-model-zzz"]) == {}


def test_static_unusable_model_is_flagged():
    meta = _provider_model_meta(["gemini-2.5-flash"])
    assert meta["gemini-2.5-flash"]["unusable"] is True


def test_learned_unusable_model_is_flagged(monkeypatch):
    import aether_engine.langgraph.model_routing as routing

    monkeypatch.setattr(routing, "_learned_cache", {"dead-model-9"})
    meta = _provider_model_meta(["dead-model-9", "gpt-4o"])
    assert meta["dead-model-9"]["unusable"] is True
    assert "unusable" not in meta["gpt-4o"]


def test_learned_match_uses_the_engine_normalisation(monkeypatch):
    """The learned set stores normalised ids (last path segment,
    lowered); a provider-prefixed listing of the same model must
    flag identically — the same rule pin_targets_unusable_model
    applies to pins."""
    import aether_engine.langgraph.model_routing as routing

    monkeypatch.setattr(routing, "_learned_cache", {"dead-model-9"})
    meta = _provider_model_meta(["someprovider/Dead-Model-9"])
    assert meta["someprovider/Dead-Model-9"]["unusable"] is True


@pytest.mark.asyncio
async def test_provider_list_entries_carry_model_meta(monkeypatch):
    from aether_engine.state import engine_state

    monkeypatch.setitem(
        engine_state.user_config.provider_models,
        "openai",
        ["gpt-4o", "no-such-model-zzz"],
    )
    store = SimpleNamespace(list_providers=list)
    cards = await _build_provider_list_async(store, discover=False)
    openai = next(c for c in cards if c["name"] == "openai")
    assert openai["model_meta"]["gpt-4o"]["cost_input_1m"] == 2.50
    # The unknown model stays listed, with no metadata entry.
    assert "no-such-model-zzz" in openai["models"]
    assert "no-such-model-zzz" not in openai["model_meta"]
