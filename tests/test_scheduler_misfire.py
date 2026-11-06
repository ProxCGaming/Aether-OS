"""Tests for APScheduler capability check scheduling and startup misfire recovery."""
import pytest

from aether_engine.providers.discovery import DiscoveryResult
from aether_engine.scheduler.capability_jobs import CapabilitySchedulerManager


@pytest.fixture
def temp_scheduler_mgr(tmp_path):
    db_path = tmp_path / "test_capabilities.db"
    mgr = CapabilitySchedulerManager(db_path=db_path)
    return mgr


class TestSchedulerMisfire:
    def test_init_creates_tables(self, temp_scheduler_mgr):
        history = temp_scheduler_mgr.get_history()
        assert isinstance(history, list)
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_run_now_creates_record(self, temp_scheduler_mgr):
        res = await temp_scheduler_mgr.run_now(method="Option A")
        assert res["status"] == "SUCCESS"
        assert res["method"] == "Option A"

        history = temp_scheduler_mgr.get_history()
        assert len(history) == 1
        assert history[0]["method"] == "Option A"

    @pytest.mark.asyncio
    async def test_schedule_interval_configuration(self, temp_scheduler_mgr):
        temp_scheduler_mgr.start()
        try:
            temp_scheduler_mgr.set_schedule("daily", "Option C")
            assert temp_scheduler_mgr.current_schedule == "daily"
            assert temp_scheduler_mgr.current_method == "Option C"

            summary = temp_scheduler_mgr.get_status_summary()
            assert summary["schedule"] == "daily"
            assert summary["method"] == "Option C"
        finally:
            temp_scheduler_mgr.stop()

    @pytest.mark.asyncio
    async def test_misfire_grace_time_configured_on_job(self, temp_scheduler_mgr):
        temp_scheduler_mgr.start()
        try:
            temp_scheduler_mgr.set_schedule("weekly", "Both")
            if temp_scheduler_mgr.scheduler:
                job = temp_scheduler_mgr.scheduler.get_job("capability_check_periodic")
                assert job is not None
                assert job.misfire_grace_time is None
        finally:
            temp_scheduler_mgr.stop()

    @pytest.mark.asyncio
    async def test_run_option_c_with_discovered_models(self, temp_scheduler_mgr):
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_store = MagicMock()
        mock_store.list_providers.return_value = ["google_gemini"]
        mock_store.load_provider.return_value = "fake-key"

        mock_discovery = AsyncMock(return_value=DiscoveryResult(models=["gemini-2.0-flash", "gemini-flash-latest"]))
        mock_self_test = AsyncMock(return_value={
            "model_id": "gemini-2.0-flash",
            "provider": "google_gemini",
            "pass_rate": 1.0,
            "avg_latency_ms": 220.0,
            "category_scores": {"chat": 1.0, "reasoning": 1.0, "code": 1.0},
            "details": {},
        })

        with patch("aether_engine.secrets.storage.SecretStore", return_value=mock_store), \
             patch("aether_engine.providers.discovery.fetch_available_models", mock_discovery), \
             patch("aether_engine.capability.self_test.run_model_self_test", mock_self_test):

            res = await temp_scheduler_mgr.run_now(method="Option C")
            assert res["status"] == "SUCCESS"
            assert res["method"] == "Option C"
            assert res["models_tested"] == 2

            caps = temp_scheduler_mgr.get_model_capabilities("gemini-2.0-flash")
            assert caps is not None
            assert caps["provider"] == "google_gemini"
            assert "chat" in caps["capabilities"]



class TestOptionCReachabilityVerdict:
    """E6-NEW-07: the persisted is_reachable must reflect whether the
    model actually answered its self-test. A model whose every probe
    errored is NOT reachable, however the run is labelled; a model that
    answered — even one that failed every grading — IS reachable."""

    async def _run_with_results(self, temp_scheduler_mgr, results_by_model):
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_store = MagicMock()
        mock_store.list_providers.return_value = ["google_gemini"]
        mock_store.load_provider.return_value = "fake-key"

        mock_discovery = AsyncMock(
            return_value=DiscoveryResult(models=list(results_by_model))
        )

        async def fake_self_test(provider_name, model_id, api_key):
            return results_by_model[model_id]

        with patch("aether_engine.secrets.storage.SecretStore", return_value=mock_store), \
             patch("aether_engine.providers.discovery.fetch_available_models", mock_discovery), \
             patch("aether_engine.capability.self_test.run_model_self_test", side_effect=fake_self_test):
            res = await temp_scheduler_mgr.run_now(method="Option C")
        assert res["status"] == "SUCCESS"
        return res

    @pytest.mark.asyncio
    async def test_model_that_never_answers_is_persisted_unreachable(self, temp_scheduler_mgr):
        dead = {
            "model_id": "dead-model",
            "provider": "google_gemini",
            "total_tests": 2,
            "passed_tests": 0,
            "pass_rate": 0.0,
            "avg_latency_ms": 0.0,
            "category_scores": {"reasoning": 0.0, "code": 0.0},
            "details": {
                "arithmetic_reasoning": {
                    "passed": False,
                    "latency_ms": 8000.0,
                    "error": "TimeoutError: no response",
                },
                "code_generation": {
                    "passed": False,
                    "latency_ms": 8000.0,
                    "error": "TimeoutError: no response",
                },
            },
        }
        await self._run_with_results(temp_scheduler_mgr, {"dead-model": dead})

        caps = temp_scheduler_mgr.get_model_capabilities("dead-model")
        assert caps is not None
        assert caps["is_reachable"] == 0

    @pytest.mark.asyncio
    async def test_answering_models_are_persisted_reachable(self, temp_scheduler_mgr):
        passing = {
            "model_id": "good-model",
            "provider": "google_gemini",
            "total_tests": 2,
            "passed_tests": 2,
            "pass_rate": 1.0,
            "avg_latency_ms": 120.0,
            "category_scores": {"reasoning": 1.0, "code": 1.0},
            "details": {
                "arithmetic_reasoning": {
                    "passed": True,
                    "latency_ms": 110.0,
                    "response_preview": "42",
                },
                "code_generation": {
                    "passed": True,
                    "latency_ms": 130.0,
                    "response_preview": "def add",
                },
            },
        }
        # Reachable but incapable: the provider answered every probe
        # (no per-test error) yet failed every grading.
        answering_but_failing = {
            "model_id": "weak-model",
            "provider": "google_gemini",
            "total_tests": 2,
            "passed_tests": 0,
            "pass_rate": 0.0,
            "avg_latency_ms": 250.0,
            "category_scores": {"reasoning": 0.0, "code": 0.0},
            "details": {
                "arithmetic_reasoning": {
                    "passed": False,
                    "latency_ms": 240.0,
                    "response_preview": "I do not know",
                },
                "code_generation": {
                    "passed": False,
                    "latency_ms": 260.0,
                    "response_preview": "no idea",
                },
            },
        }
        await self._run_with_results(
            temp_scheduler_mgr,
            {"good-model": passing, "weak-model": answering_but_failing},
        )

        good = temp_scheduler_mgr.get_model_capabilities("good-model")
        assert good is not None
        assert good["is_reachable"] == 1

        weak = temp_scheduler_mgr.get_model_capabilities("weak-model")
        assert weak is not None
        assert weak["is_reachable"] == 1
