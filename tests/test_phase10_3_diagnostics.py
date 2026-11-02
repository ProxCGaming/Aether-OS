"""Phase 10.3, D2: direct tests for
``aether_engine.validation.diagnostics.run_environment_diagnostics``.

No direct tests existed — the function was only run through the
websocket ``/diagnostics`` route. The checks that can be made
deterministic are tested with mocks (Ollama, terminal failure); the
checks that depend on this machine (Python, SQLite) are tested for
real, and the environment-dependent ones (Ollama actually running,
TAVILY key present) are asserted only on structure, never on a
specific status.
"""
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from aether_engine.validation import diagnostics
from aether_engine.validation.diagnostics import run_environment_diagnostics

EXPECTED_TARGETS = [
    "Python Environment",
    "Terminal Access",
    "SQLite Storage",
    "Ollama Local Service",
    "Web Search API",
]


def by_target(logs):
    return {entry["target_name"]: entry for entry in logs}


class TestStructure:
    def test_returns_one_entry_per_check_in_order(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        logs = run_environment_diagnostics()
        assert [entry["target_name"] for entry in logs] == EXPECTED_TARGETS

    def test_every_entry_has_status_details_and_timestamp(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        for entry in run_environment_diagnostics():
            assert entry["status"] in ("OK", "WARN", "FAILED")
            assert isinstance(entry["details"], str) and entry["details"]
            assert isinstance(entry["timestamp"], float)


class TestRealChecks:
    def test_python_environment_reports_the_running_version(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        entry = by_target(run_environment_diagnostics())["Python Environment"]
        version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        assert entry["status"] == "OK"
        assert entry["details"] == f"v{version}"

    def test_terminal_access_succeeds_on_this_machine(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        entry = by_target(run_environment_diagnostics())["Terminal Access"]
        assert entry["status"] == "OK"
        assert entry["details"] == "Permissions granted"

    def test_sqlite_storage_verifies_read_write_and_cleans_up(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        diag_db = Path.home() / ".aether" / "diag_test.db"
        if diag_db.exists():
            diag_db.unlink()
        entry = by_target(run_environment_diagnostics())["SQLite Storage"]
        assert entry["status"] == "OK"
        assert entry["details"] == "Read/Write verified"
        assert not diag_db.exists(), "diagnostics must remove its scratch db"


class TestWebSearchApi:
    def test_warns_when_no_tavily_key(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        entry = by_target(run_environment_diagnostics())["Web Search API"]
        assert entry["status"] == "WARN"
        assert entry["details"] == "Missing TAVILY_API_KEY"

    def test_ok_when_tavily_key_present(self, monkeypatch):
        # E5-F18 realignment: this test previously set a fake key and
        # asserted OK / "Key verified" with no probe at all — it
        # pinned the audited defect (presence labelled as verified).
        # The runner now verifies the key with a real probe; the
        # OK / "Key verified" outcome is asserted against a probe
        # that answers 200. The rejected/unreachable outcomes are
        # pinned in tests/test_epic5_minors_batch.py.
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
        response = MagicMock()
        response.status = 200
        response.__enter__.return_value = response

        def fake_urlopen(req, timeout=None):
            if "tavily" in getattr(req, "full_url", ""):
                return response
            raise urllib.error.URLError("refused")

        with patch.object(diagnostics.urllib.request, "urlopen", fake_urlopen):
            entry = by_target(run_environment_diagnostics())["Web Search API"]
        assert entry["status"] == "OK"
        assert entry["details"] == "Key verified"


class TestMockedChecks:
    def test_ollama_failed_when_connection_refused(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        with patch.object(diagnostics.urllib.request, "urlopen",
                          side_effect=urllib.error.URLError("refused")):
            entry = by_target(run_environment_diagnostics())["Ollama Local Service"]
        assert entry["status"] == "FAILED"
        assert entry["details"] == "Connection refused"

    def test_ollama_ok_when_service_answers_200(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        response = MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        with patch.object(diagnostics.urllib.request, "urlopen", return_value=response):
            entry = by_target(run_environment_diagnostics())["Ollama Local Service"]
        assert entry["status"] == "OK"
        assert entry["details"] == "Service running"

    def test_ollama_warn_on_non_200_response(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        response = MagicMock()
        response.status = 503
        response.__enter__.return_value = response
        with patch.object(diagnostics.urllib.request, "urlopen", return_value=response):
            entry = by_target(run_environment_diagnostics())["Ollama Local Service"]
        assert entry["status"] == "WARN"
        assert entry["details"] == "HTTP 503"

    def test_terminal_failure_is_reported_not_raised(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        with patch.object(diagnostics.subprocess, "run",
                          side_effect=OSError("no shell")):
            entry = by_target(run_environment_diagnostics())["Terminal Access"]
        assert entry["status"] == "FAILED"
        assert entry["details"] == "no shell"

    def test_one_failing_check_does_not_abort_the_rest(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        with patch.object(diagnostics.subprocess, "run",
                          side_effect=OSError("no shell")), \
             patch.object(diagnostics.urllib.request, "urlopen",
                          side_effect=urllib.error.URLError("refused")):
            logs = run_environment_diagnostics()
        assert len(logs) == 5
        assert by_target(logs)["Terminal Access"]["status"] == "FAILED"
        assert by_target(logs)["Ollama Local Service"]["status"] == "FAILED"
        assert by_target(logs)["Python Environment"]["status"] == "OK"
