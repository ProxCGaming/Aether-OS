"""Tests for Ollama local models manager and task-driven download pipeline."""
import asyncio
from pathlib import Path
import pytest

from aether_common.contracts import EventType
from aether_engine.models.local_models import OllamaManager


@pytest.fixture
def ollama_mgr():
    return OllamaManager()


class TestLocalModelsRegistry:
    @pytest.mark.asyncio
    async def test_list_models_returns_sorted_catalog(self, ollama_mgr):
        models = await ollama_mgr.list_models()
        assert isinstance(models, list)
        assert len(models) >= 1
        # Verify schema
        first = models[0]
        assert "name" in first
        assert "size" in first
        assert "downloaded" in first

    @pytest.mark.asyncio
    async def test_download_model_task_progress_events(self, ollama_mgr, tmp_path):
        dest_dir = str(tmp_path / "models")
        events = []

        async for ev in ollama_mgr.download_model_task(
            model_name="test-model:latest",
            destination_dir=dest_dir,
            request_id="req-test-123",
        ):
            events.append(ev)

        assert events[0].type == EventType.TASK_CREATED
        assert events[0].request_id == "req-test-123"

        progress_events = [e for e in events if e.type == EventType.LOCAL_MODEL_DOWNLOAD_PROGRESS]
        assert len(progress_events) > 0
        assert progress_events[-1].payload["download_percent"] == 100.0

        completed = [e for e in events if e.type == EventType.TASK_COMPLETED]
        assert len(completed) == 1
        assert completed[0].payload["model"] == "test-model:latest"

    @pytest.mark.asyncio
    async def test_delete_local_model(self, ollama_mgr):
        res = await ollama_mgr.delete_model("nonexistent-model")
        assert res is True
