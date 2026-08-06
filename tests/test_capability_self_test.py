import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from aether_engine.capability.benchmark_cache import BENCHMARK_CACHE, get_benchmark_for_model
from aether_engine.capability.classifier import classify_model_capabilities
from aether_engine.capability.self_test import run_model_self_test

def test_benchmark_cache_lookup():
    assert "gemini-1.5-pro" in BENCHMARK_CACHE
    bench = BENCHMARK_CACHE["gemini-1.5-pro"]
    assert bench["mmlu"] > 80.0
    assert bench["humaneval"] > 80.0
    assert "code" in bench["capabilities"]
    assert "reasoning" in bench["capabilities"]

def test_capability_synthesis_option_a_only():
    profile = classify_model_capabilities("gemini-1.5-pro")
    assert profile["tier"] == "premium"
    assert "code" in profile["capabilities"]
    assert "reasoning" in profile["capabilities"]
    assert profile["has_benchmark"] is True
    assert profile["has_selftest"] is False

def test_capability_synthesis_option_c_only():
    mock_st = {
        "pass_rate": 1.0,
        "avg_latency_ms": 250.0,
        "category_scores": {"code": 1.0, "reasoning": 1.0, "chat": 1.0},
    }
    profile = classify_model_capabilities("unknown-custom-model", selftest_data=mock_st)
    assert profile["tier"] == "premium"
    assert "code" in profile["capabilities"]
    assert "reasoning" in profile["capabilities"]
    assert profile["has_benchmark"] is False
    assert profile["has_selftest"] is True

def test_capability_synthesis_both():
    mock_st = {
        "pass_rate": 0.95,
        "avg_latency_ms": 300.0,
        "category_scores": {"code": 1.0, "reasoning": 1.0},
    }
    profile = classify_model_capabilities("gemini-1.5-pro", selftest_data=mock_st)
    assert profile["tier"] == "premium"
    assert "reasoning" in profile["capabilities"]
    assert "code" in profile["capabilities"]
    assert profile["confidence"] >= 0.9
    assert profile["has_benchmark"] is True
    assert profile["has_selftest"] is True

@pytest.mark.asyncio
async def test_run_model_self_test():
    with patch("aether_engine.capability.self_test.LiteLLMProvider") as mock_prov_cls:
        mock_prov = MagicMock()
        async def _stream(*args, **kwargs):
            mock_chunk = MagicMock()
            mock_chunk.text = "120"
            yield mock_chunk
        mock_prov.call_stream = _stream
        mock_prov_cls.return_value = mock_prov

        report = await run_model_self_test("google_gemini", "gemini-1.5-pro", "fake-key")
        assert report["total_tests"] == 4
        assert report["model_id"] == "gemini-1.5-pro"
