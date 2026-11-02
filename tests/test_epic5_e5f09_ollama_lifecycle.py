"""E5-F09 — Ollama pull/delete must report honest lifecycle results.

Failed lifecycle operations (daemon offline, connection failure, non-200,
streamed ``error`` event, truncated stream) were acknowledged as successful
completion: pulls fell back to a simulated 0-100% progress stream that
recorded a downloaded path and emitted TASK_COMPLETED, and deletes cleared
metadata and returned True on every failure path. These tests drive the
real ``OllamaManager`` seams against a scripted fake httpx client (no real
Ollama daemon exists in this environment) and pin the truthful behaviour:

- any pull failure -> exactly one TASK_FAILED carrying the real cause,
  no TASK_COMPLETED, and no downloaded-path metadata recorded;
- a healthy pull (200 + progress lines + terminal success status) still
  streams real progress in order, completes, and records the path;
- delete returns True and clears metadata ONLY on daemon confirmation
  (200/204); every failure path returns False and RETAINS the metadata.
"""
import json
from typing import ClassVar

import httpx
import pytest

from aether_common.contracts import EventType
from aether_engine.models import local_models
from aether_engine.models.local_models import OllamaManager

MODEL = "synthetic-model:latest"


# --------------------------------------------------------------------------
# Scripted fake httpx layer (drop-in replacement for httpx.AsyncClient)
# --------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code=200, lines=None, json_data=None, text=""):
        self.status_code = status_code
        self._lines = lines or []
        self._json_data = json_data or {}
        self.text = text

    def json(self):
        return self._json_data

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return self.text.encode()


class _StreamContext:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    async def __aenter__(self):
        if self._exc is not None:
            raise self._exc
        return self._response

    async def __aexit__(self, *exc_info):
        return False


class FakeAsyncClient:
    """httpx.AsyncClient stand-in driven by the class-level ``script``.

    script keys: "get", "delete", "stream" — each either an Exception
    instance (raised) or a callable returning the fake result.
    """

    script: ClassVar[dict] = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url):
        handler = self.script["get"]
        if isinstance(handler, Exception):
            raise handler
        return handler(url)

    async def delete(self, url, json=None):
        handler = self.script["delete"]
        if isinstance(handler, Exception):
            raise handler
        return handler(url, json)

    def stream(self, method, url, json=None):
        handler = self.script["stream"]
        if isinstance(handler, Exception):
            return _StreamContext(exc=handler)
        return handler(method, url, json)


def _daemon_up(url):
    return FakeResponse(status_code=200, text="Ollama is running")


@pytest.fixture
def fake_httpx(monkeypatch):
    FakeAsyncClient.script = {}
    monkeypatch.setattr(local_models.httpx, "AsyncClient", FakeAsyncClient)
    return FakeAsyncClient


def _script_daemon_down():
    FakeAsyncClient.script = {
        "get": httpx.ConnectError("connection refused", request=httpx.Request("GET", "http://x")),
        "delete": httpx.ConnectError("connection refused", request=httpx.Request("DELETE", "http://x")),
        "stream": httpx.ConnectError("connection refused", request=httpx.Request("POST", "http://x")),
    }


def _script_pull(lines=None, status_code=200, body_text=""):
    def _stream(method, url, json):
        return _StreamContext(response=FakeResponse(
            status_code=status_code, lines=lines or [], text=body_text,
        ))

    FakeAsyncClient.script = {
        "get": _daemon_up,
        "stream": _stream,
        "delete": lambda url, json: FakeResponse(status_code=200),
    }


async def _collect(mgr, dest):
    events = []
    async for ev in mgr.download_model_task(
        model_name=MODEL, destination_dir=dest, task_id="dl-1", request_id="req-1",
    ):
        events.append(ev)
    return events


def _types(events):
    return [e.type for e in events]


# --------------------------------------------------------------------------
# Pull — failure modes must fail honestly
# --------------------------------------------------------------------------
class TestPullFailuresAreHonest:
    @pytest.mark.asyncio
    async def test_pull_offline_fails_without_completion_or_metadata(self, fake_httpx, tmp_path):
        _script_daemon_down()
        mgr = OllamaManager()
        dest = str(tmp_path / "models")
        events = await _collect(mgr, dest)

        assert events[0].type == EventType.TASK_CREATED
        assert EventType.TASK_COMPLETED not in _types(events)
        failed = [e for e in events if e.type == EventType.TASK_FAILED]
        assert len(failed) == 1
        assert "not running" in failed[0].payload["error"].lower() \
            or "unreachable" in failed[0].payload["error"].lower()
        assert mgr._downloaded_paths == {}
        assert not (tmp_path / "models").exists()

    @pytest.mark.asyncio
    async def test_pull_connection_failure_after_probe_fails(self, fake_httpx, tmp_path):
        _script_pull()  # daemon answers the probe...
        # ...but the pull connection itself dies on entry.
        FakeAsyncClient.script["stream"] = httpx.ConnectError(
            "connection reset", request=httpx.Request("POST", "http://x"),
        )
        mgr = OllamaManager()
        events = await _collect(mgr, str(tmp_path / "models"))

        assert EventType.TASK_COMPLETED not in _types(events)
        failed = [e for e in events if e.type == EventType.TASK_FAILED]
        assert len(failed) == 1
        assert failed[0].payload["error"]
        assert mgr._downloaded_paths == {}

    @pytest.mark.asyncio
    async def test_pull_http_503_fails_with_status_in_error(self, fake_httpx, tmp_path):
        _script_pull(status_code=503, body_text="service unavailable")
        mgr = OllamaManager()
        events = await _collect(mgr, str(tmp_path / "models"))

        assert EventType.TASK_COMPLETED not in _types(events)
        failed = [e for e in events if e.type == EventType.TASK_FAILED]
        assert len(failed) == 1
        assert "503" in failed[0].payload["error"]
        assert mgr._downloaded_paths == {}

    @pytest.mark.asyncio
    async def test_pull_streamed_error_fails_and_records_nothing(self, fake_httpx, tmp_path):
        lines = [
            json.dumps({"status": "pulling manifest"}),
            json.dumps({"error": "pull model manifest: file does not exist"}),
        ]
        _script_pull(lines=lines)
        mgr = OllamaManager()
        events = await _collect(mgr, str(tmp_path / "models"))

        assert EventType.TASK_COMPLETED not in _types(events)
        failed = [e for e in events if e.type == EventType.TASK_FAILED]
        assert len(failed) == 1
        assert "file does not exist" in failed[0].payload["error"]
        assert mgr._downloaded_paths == {}

    @pytest.mark.asyncio
    async def test_pull_truncated_stream_fails(self, fake_httpx, tmp_path):
        # Stream ends mid-download: progress lines but no success status.
        lines = [
            json.dumps({"status": "downloading sha256:abc", "total": 100, "completed": 40}),
        ]
        _script_pull(lines=lines)
        mgr = OllamaManager()
        events = await _collect(mgr, str(tmp_path / "models"))

        assert EventType.TASK_COMPLETED not in _types(events)
        failed = [e for e in events if e.type == EventType.TASK_FAILED]
        assert len(failed) == 1
        assert mgr._downloaded_paths == {}

    def test_no_simulated_download_stream_remains(self):
        # The simulated-progress fallback must be removed, not re-labelled.
        assert not hasattr(OllamaManager, "_simulated_download_stream")


# --------------------------------------------------------------------------
# Pull — healthy path still works end to end
# --------------------------------------------------------------------------
class TestPullHealthyPath:
    @pytest.mark.asyncio
    async def test_healthy_pull_streams_real_progress_and_completes(self, fake_httpx, tmp_path):
        lines = [
            json.dumps({"status": "pulling manifest"}),
            json.dumps({"status": "downloading sha256:abc", "total": 100, "completed": 40}),
            json.dumps({"status": "downloading sha256:abc", "total": 100, "completed": 100}),
            json.dumps({"status": "success"}),
        ]
        _script_pull(lines=lines)
        mgr = OllamaManager()
        dest = str(tmp_path / "models")
        events = await _collect(mgr, dest)

        assert events[0].type == EventType.TASK_CREATED
        progress = [e for e in events if e.type == EventType.LOCAL_MODEL_DOWNLOAD_PROGRESS]
        percents = [e.payload["download_percent"] for e in progress]
        # Real progress, in the order the daemon reported it.
        assert percents == sorted(percents)
        assert 40.0 in percents and 100.0 in percents
        assert percents[-1] == 100.0

        completed = [e for e in events if e.type == EventType.TASK_COMPLETED]
        assert len(completed) == 1
        assert completed[0].payload["model"] == MODEL
        assert EventType.TASK_FAILED not in _types(events)
        assert mgr._downloaded_paths[MODEL] == str(tmp_path / "models" / MODEL)


# --------------------------------------------------------------------------
# Delete — success only on daemon confirmation; metadata retained on failure
# --------------------------------------------------------------------------
class TestDeleteHonesty:
    @pytest.mark.asyncio
    async def test_delete_offline_returns_false_and_retains_metadata(self, fake_httpx):
        _script_daemon_down()
        mgr = OllamaManager()
        mgr._downloaded_paths[MODEL] = "/somewhere/models/" + MODEL

        assert await mgr.delete_model(MODEL) is False
        assert mgr._downloaded_paths[MODEL] == "/somewhere/models/" + MODEL

    @pytest.mark.asyncio
    async def test_delete_http_503_returns_false_and_retains_metadata(self, fake_httpx):
        FakeAsyncClient.script = {
            "get": _daemon_up,
            "delete": lambda url, json: FakeResponse(status_code=503),
        }
        mgr = OllamaManager()
        mgr._downloaded_paths[MODEL] = "/somewhere/models/" + MODEL

        assert await mgr.delete_model(MODEL) is False
        assert mgr._downloaded_paths[MODEL] == "/somewhere/models/" + MODEL

    @pytest.mark.asyncio
    async def test_delete_connection_failure_returns_false_and_retains_metadata(self, fake_httpx):
        FakeAsyncClient.script = {
            "get": _daemon_up,
            "delete": httpx.ConnectError(
                "connection reset", request=httpx.Request("DELETE", "http://x"),
            ),
        }
        mgr = OllamaManager()
        mgr._downloaded_paths[MODEL] = "/somewhere/models/" + MODEL

        assert await mgr.delete_model(MODEL) is False
        assert mgr._downloaded_paths[MODEL] == "/somewhere/models/" + MODEL

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [200, 204])
    async def test_delete_confirmed_clears_metadata_and_returns_true(self, fake_httpx, status_code):
        FakeAsyncClient.script = {
            "get": _daemon_up,
            "delete": lambda url, json: FakeResponse(status_code=status_code),
        }
        mgr = OllamaManager()
        mgr._downloaded_paths[MODEL] = "/somewhere/models/" + MODEL

        assert await mgr.delete_model(MODEL) is True
        assert MODEL not in mgr._downloaded_paths
