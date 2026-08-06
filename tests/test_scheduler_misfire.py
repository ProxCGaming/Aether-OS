"""Tests for APScheduler capability check scheduling and startup misfire recovery."""
import asyncio
from pathlib import Path
import pytest

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
        from unittest.mock import patch, AsyncMock, MagicMock

        mock_store = MagicMock()
        mock_store.list_providers.return_value = ["google_gemini"]
        mock_store.load_provider.return_value = "fake-key"

        mock_discovery = AsyncMock(return_value=["gemini-2.0-flash", "gemini-flash-latest"])
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

