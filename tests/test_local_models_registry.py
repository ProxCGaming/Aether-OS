"""Tests for Ollama local models manager and task-driven download pipeline."""
import json

import pytest

from aether_common.contracts import EventType
from aether_engine.models import local_models
from aether_engine.models.local_models import OllamaManager


@pytest.fixture
def ollama_mgr():
    return OllamaManager()


# --- Scripted healthy-daemon fake (E5-F09 realignment) ---------------------
# The download/delete tests below previously ran against NO daemon and
# passed only because failed operations were simulated into success (the
# E5-F09 defect). They now drive the real pull/delete paths against a
# scripted healthy daemon; the assertions are unchanged.
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


# Built at module level: inside _HealthyClient.stream the httpx-style
# ``json=`` parameter shadows the json module.
_HEALTHY_PULL_LINES = [
    json.dumps({"status": "pulling manifest"}),
    json.dumps({"status": "downloading sha256:abc", "total": 100, "completed": 100}),
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

    async def delete(self, url, json=None):
        return _FakeResponse(status_code=200)

    def stream(self, method, url, json=None):
        return _StreamContext(_FakeResponse(status_code=200, lines=_HEALTHY_PULL_LINES))


@pytest.fixture
def healthy_daemon(monkeypatch):
    monkeypatch.setattr(local_models.httpx, "AsyncClient", _HealthyClient)


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
    async def test_download_model_task_progress_events(self, ollama_mgr, tmp_path, healthy_daemon):
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
    async def test_delete_local_model(self, ollama_mgr, healthy_daemon):
        res = await ollama_mgr.delete_model("nonexistent-model")
        assert res is True

    @pytest.mark.asyncio
    async def test_delete_local_model_offline_is_not_success(self, ollama_mgr):
        # E5-F09: with no daemon reachable (the real state in this
        # environment), a delete must report failure, not True.
        res = await ollama_mgr.delete_model("nonexistent-model")
        assert res is False
