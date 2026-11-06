"""Wave 5, Annex C row 2 remainder — capability_jobs honesty.

- E6-NEW-15: the dead duplicate ``_SELF_TEST_PROMPTS`` is deleted;
  the one live suite is ``capability.test_suite.SELF_TEST_SUITE``,
  executed by ``capability.self_test``.
- E6-NEW-16: a run record's ``models_tested`` counts only models
  that actually underwent a provider call (Option C), and the cost
  estimate prices only those, at the real suite size. An
  Option-A-only run loads static benchmark rows: it must record
  0 models tested and 0 cost, not a fabricated figure.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aether_engine.capability.test_suite import SELF_TEST_SUITE
from aether_engine.providers.discovery import DiscoveryResult
from aether_engine.scheduler import capability_jobs
from aether_engine.scheduler.capability_jobs import CapabilitySchedulerManager


@pytest.fixture()
def mgr(tmp_path):
    return CapabilitySchedulerManager(db_path=tmp_path / "caps.db")


def test_dead_prompt_suite_is_gone():
    assert not hasattr(capability_jobs, "_SELF_TEST_PROMPTS"), (
        "capability_jobs still carries its dead duplicate prompt "
        "suite; the live suite is capability.test_suite.SELF_TEST_SUITE"
    )


@pytest.mark.asyncio
async def test_option_a_only_run_records_no_models_and_no_cost(mgr):
    res = await mgr.run_now(method="Option A")
    assert res["status"] == "SUCCESS"
    assert res["models_tested"] == 0
    assert res["cost_estimate"] == 0.0
    # Option A still did its real work: the live benchmark rows were
    # loaded into model_capabilities.
    caps = mgr.get_model_capabilities("gemini-1.5-pro")
    assert caps is not None
    assert caps["benchmark_data"]["mmlu"] > 0


@pytest.mark.asyncio
async def test_option_c_run_counts_and_prices_real_calls(mgr):
    mock_store = MagicMock()
    mock_store.list_providers.return_value = ["google_gemini"]
    mock_store.load_provider.return_value = "fake-key"

    mock_discovery = AsyncMock(
        return_value=DiscoveryResult(models=["model-one", "model-two"])
    )
    mock_self_test = AsyncMock(return_value={
        "model_id": "model-one",
        "provider": "google_gemini",
        "pass_rate": 1.0,
        "avg_latency_ms": 100.0,
        "category_scores": {"chat": 1.0},
        "details": {},
    })

    with patch("aether_engine.secrets.storage.SecretStore", return_value=mock_store), \
         patch("aether_engine.providers.discovery.fetch_available_models", mock_discovery), \
         patch("aether_engine.capability.self_test.run_model_self_test", mock_self_test):
        res = await mgr.run_now(method="Option C")

    assert res["status"] == "SUCCESS"
    assert res["models_tested"] == 2
    # Priced at the real suite size, not an assumed 3 tests per model.
    assert res["cost_estimate"] == round(2 * len(SELF_TEST_SUITE) * 0.002, 4)
