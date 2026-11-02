"""Epic 2, E2-09: approval cache grants.

Contract: a grant is (path prefix, risk ceiling). It covers a later
"ask" call when the call's path is inside the prefix and its risk is
at or below the ceiling — a write grant covers reads under it, never
a riskier call, never a path outside, and never a floored path
(ADR 0029 D3). Grants persist across cache instances (engine
restarts) and revocation is immediate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aether_engine.tools.approval_cache import ApprovalCache


@pytest.fixture()
def cache(tmp_path):
    return ApprovalCache(tmp_path / "grants.db")


def test_grant_covers_same_risk_and_below(cache, tmp_path):
    folder = tmp_path / "proj"
    cache.grant(str(folder), "write", tool_name="write_file")
    assert cache.covering_grant(risk="write", args={"path": str(folder / "a.py")}) is not None
    assert cache.covering_grant(risk="read_only", args={"path": str(folder / "a.py")}) is not None


def test_grant_does_not_cover_higher_risk_or_outside(cache, tmp_path):
    folder = tmp_path / "proj"
    cache.grant(str(folder), "write")
    assert cache.covering_grant(risk="destructive", args={"path": str(folder / "a.py")}) is None
    assert cache.covering_grant(risk="execute", args={"path": str(folder / "a.py")}) is None
    assert cache.covering_grant(risk="write", args={"path": str(tmp_path / "other" / "a.py")}) is None
    # Sibling-prefix confusion: proj2 is not inside proj.
    assert cache.covering_grant(risk="write", args={"path": str(tmp_path / "proj2" / "a.py")}) is None
    # Calls without a path are never covered (e.g. shell).
    assert cache.covering_grant(risk="write", args={"command": "ls"}) is None


def test_file_scoped_grant_covers_exactly_that_file(cache, tmp_path):
    target = tmp_path / "proj" / "a.py"
    cache.grant(str(target), "write")
    assert cache.covering_grant(risk="write", args={"path": str(target)}) is not None
    assert cache.covering_grant(risk="write", args={"path": str(tmp_path / "proj" / "b.py")}) is None


def test_floored_paths_are_ungrantable(cache):
    ssh = Path.home() / ".ssh"
    cache.grant(str(ssh), "execute")
    assert cache.covering_grant(risk="read_only", args={"path": str(ssh / "id_ed25519")}) is None
    assert cache.covering_grant(risk="write", args={"path": "/etc/hostname"}) is None


def test_revoke_and_persistence(tmp_path):
    db = tmp_path / "grants.db"
    first = ApprovalCache(db)
    grant_id = first.grant(str(tmp_path / "proj"), "write")
    assert first.covering_grant(risk="write", args={"path": str(tmp_path / "proj" / "x")}) is not None

    # A fresh instance on the same DB (an engine restart) sees the grant.
    second = ApprovalCache(db)
    assert second.covering_grant(risk="write", args={"path": str(tmp_path / "proj" / "x")}) is not None
    assert second.list_grants()[0]["id"] == grant_id

    assert second.revoke(grant_id) is True
    assert second.covering_grant(risk="write", args={"path": str(tmp_path / "proj" / "x")}) is None
    assert second.revoke(grant_id) is False


@pytest.mark.asyncio
async def test_registry_execute_honours_grant(tmp_path):
    from aether_engine.tools.registry import ToolExecutionError, build_tool_registry

    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.txt"
    registry = build_tool_registry()
    cache = ApprovalCache(tmp_path / "grants.db")

    # Without a grant, an unapproved write raises the approval error.
    with pytest.raises(ToolExecutionError, match="requires approval"):
        await registry.execute(
            "write_file", {"path": str(target), "content": "x"},
            workspace_roots=[str(ws)], policies={}, approval_cache=cache,
        )

    cache.grant(str(ws), "write", tool_name="write_file")
    # Route through the in-process worker substitute like other suites:
    # the grant path is what matters here, and the sandbox needs pywin32.
    from aether_engine.workers import sandbox
    from aether_worker.__main__ import run_worker

    async def _in_process(tool_name, args, task_class="standard", audit_logger=None):
        return await run_worker({"tool_name": tool_name, "args": args})

    from unittest import mock

    with mock.patch.object(sandbox, "execute_tool_in_worker", _in_process):
        result = await registry.execute(
            "write_file", {"path": str(target), "content": "x"},
            workspace_roots=[str(ws)], policies={}, approval_cache=cache,
        )
    assert result["written"] is True
    assert target.read_text(encoding="utf-8") == "x"
