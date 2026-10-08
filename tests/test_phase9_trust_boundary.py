"""Phase 9 trust-boundary tests.

Each test reproduces a finding from the Phase 9 audit: it fails on the
pre-fix code and passes after the fix. See docs/AETHER_Phase9_Agent_Prompt.md
and docs/adr/0022-phase-9-trust-boundary-hardening.md.
"""
import pytest
from fastapi.testclient import TestClient

from aether_engine.app import app

client = TestClient(app, raise_server_exceptions=False)


class TestLegacyHttpRoutesRemoved:
    """A1: the seven unauthenticated HTTP management routes must be gone.

    They bypassed the ADR 0003 token handshake; their only callers were the
    retired PySide6 UI and a test for that retired path.
    """

    @pytest.mark.parametrize("method,path", [
        ("GET", "/plugins"),
        ("POST", "/plugins/install"),
        ("POST", "/plugins/confirm"),
        ("DELETE", "/plugins/some-plugin"),
        ("PATCH", "/plugins/some-plugin/toggle"),
        ("GET", "/agents/nodes"),
        ("PATCH", "/agents/nodes/research/toggle"),
    ])
    def test_legacy_route_absent(self, method, path):
        resp = client.request(method, path, json={"enabled": True, "source": "x"})
        assert resp.status_code == 404, (
            f"{method} {path} should not exist (got {resp.status_code}); "
            "unauthenticated management routes must stay deleted per ADR 0003"
        )


class TestSandboxFailClosed:
    """A3: if resource limits cannot be enforced, risky tools must NOT run.

    The old code logged a warning and continued with job_obj=None, i.e.
    silently unlimited execution — the opposite of ADR 0010's guarantee.
    """

    def test_worker_refuses_when_job_object_fails(self):
        import asyncio
        from unittest.mock import MagicMock, patch

        from aether_engine.workers.sandbox import execute_tool_in_worker

        audit = MagicMock()
        with patch(
            "aether_engine.workers.sandbox.WorkerJobObject",
            side_effect=RuntimeError("pywin32 is not installed or available."),
        ):
            with pytest.raises(RuntimeError, match="Refusing to execute"):
                asyncio.run(
                    execute_tool_in_worker(
                        tool_name="execute_shell",
                        args={"command": "echo hi"},
                        task_class="standard",
                        audit_logger=audit,
                    )
                )
        audit.log_event.assert_called_once()
        event_name = audit.log_event.call_args[0][0]
        assert event_name == "WORKER_EXECUTION_REFUSED", event_name
