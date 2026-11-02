"""E2-F04: the approval cache must survive the engine's real thread mix.

The component suite (test_epic2_approval_cache.py) drives the cache
from one thread and passes. Production does not: the process-wide
singleton is created on whichever thread touches it first (the WS
event loop, via the scoped-approval grant at approval time), while
grant list/revoke run through ``asyncio.to_thread`` worker threads
and ``covering_grant`` is called from the graph router / executor.
With a default-affinity SQLite connection every one of those
cross-thread calls raised "SQLite objects created in a thread can
only be used in that same thread" — the WS handlers died without
responding and revoke silently left the grant in place.

These tests reproduce that shape: the cache is created on one
thread and used from others, exactly as the routes call it.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.tools import approval_cache as approval_cache_module
from aether_engine.tools.approval_cache import ApprovalCache


@pytest.fixture()
def cache(tmp_path):
    # Created on the test's main thread — the stand-in for the event
    # loop thread that first touches the singleton in production.
    return ApprovalCache(tmp_path / "grants.db")


async def test_list_and_revoke_from_worker_thread(cache, tmp_path):
    """The WS route shape: record on the creator thread, list and
    revoke through asyncio.to_thread (a different thread)."""
    grant_id = cache.grant(str(tmp_path / "proj"), "write", tool_name="write_file")

    grants = await asyncio.to_thread(cache.list_grants)
    assert [g["id"] for g in grants] == [grant_id]

    revoked = await asyncio.to_thread(cache.revoke, grant_id)
    assert revoked is True

    # The revoke must be real: the grant is gone for every reader.
    assert await asyncio.to_thread(cache.list_grants) == []
    assert cache.covering_grant(risk="write", args={"path": str(tmp_path / "proj" / "x.py")}) is None


async def test_covering_grant_from_worker_thread(cache, tmp_path):
    """The graph-router shape: lookup runs on a non-creator thread."""
    cache.grant(str(tmp_path / "proj"), "write", tool_name="write_file")

    found = await asyncio.to_thread(
        cache.covering_grant, risk="write", args={"path": str(tmp_path / "proj" / "x.py")}
    )
    assert found is not None
    assert found["prefix"] == str((tmp_path / "proj").resolve())


async def test_mixed_concurrent_access_from_many_threads(cache, tmp_path):
    """Router, executor and Settings traffic overlap: grants, lookups
    and lists from a thread pool must neither raise nor corrupt."""
    folder = tmp_path / "proj"

    def work(i: int) -> None:
        if i % 3 == 0:
            cache.grant(str(folder), "write", tool_name="write_file")
        elif i % 3 == 1:
            cache.covering_grant(risk="write", args={"path": str(folder / "x.py")})
        else:
            cache.list_grants()

    with ThreadPoolExecutor(max_workers=8) as pool:
        # fut.result() re-raises any worker exception (the cross-thread
        # ProgrammingError before the fix).
        for fut in [pool.submit(work, i) for i in range(60)]:
            fut.result()

    assert len(cache.list_grants()) == 20


async def test_cross_thread_revoke_survives_reopen(cache, tmp_path):
    """A revoke issued from a worker thread persists: a fresh cache
    instance (engine restart) must not resurrect the grant."""
    grant_id = cache.grant(str(tmp_path / "proj"), "write")
    assert await asyncio.to_thread(cache.revoke, grant_id) is True

    reopened = ApprovalCache(tmp_path / "grants.db")
    assert reopened.list_grants() == []
    assert reopened.covering_grant(risk="write", args={"path": str(tmp_path / "proj" / "x")}) is None


def test_default_cache_singleton_is_one_instance_across_threads(tmp_path, monkeypatch):
    """get_default_cache() raced from two threads must hand back a
    single shared cache, not one per caller."""
    monkeypatch.setattr(
        approval_cache_module, "default_cache_path", lambda: tmp_path / "shared.db"
    )
    monkeypatch.setattr(approval_cache_module, "_default_cache", None)

    results: list[ApprovalCache] = []

    def grab() -> None:
        results.append(approval_cache_module.get_default_cache())

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    assert all(r is results[0] for r in results)


def _ws_roundtrip(ws, event: Event) -> Event:
    ws.send_text(event.to_json())
    return Event.from_json(ws.receive_text())


def test_ws_grants_list_and_revoke_route(tmp_path, monkeypatch):
    """End to end over the real websocket route: a grant recorded on
    the creator thread is listed and revoked via the Settings
    packets, and the revoke actually removes it.

    The approval-cache singleton is seeded with a cache created HERE,
    on the test's main thread, while the route handlers run on the
    TestClient portal thread and their asyncio.to_thread calls on
    pool threads — the creator/user thread split that broke in
    production.

    The session runs in a daemon thread with a bounded join: before
    the fix the list handler died on the cross-thread SQLite error
    and no response was ever sent, so a same-thread receive would
    block forever instead of failing."""
    from aether_engine.app import app, engine_state

    seeded = ApprovalCache(tmp_path / "ws-grants.db")
    monkeypatch.setattr(approval_cache_module, "_default_cache", seeded)
    grant_id = seeded.grant(str(tmp_path / "proj"), "write", tool_name="write_file")

    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    engine_state.initialize_auth()

    outcome: dict = {}

    def run_session() -> None:
        try:
            with (
                mock.patch(
                    "aether_engine.memory.session_store.DB_PATH", tmp_path / "test_memory.db"
                ),
                TestClient(app, raise_server_exceptions=False) as client,
            ):
                # The app lifespan re-runs initialize() at startup
                # and regenerates the auth token, so read the live one.
                token = engine_state.auth_token
                with client.websocket_connect(
                    "/ws/tasks", headers={"Authorization": f"Bearer {token}"}
                ) as ws:
                    hello = Event.from_json(ws.receive_text())
                    assert hello.type == EventType.HELLO
                    outcome["listed"] = _ws_roundtrip(
                        ws, Event(type=EventType.APPROVAL_GRANTS_REQUEST)
                    )
                    outcome["revoked"] = _ws_roundtrip(
                        ws,
                        Event(
                            type=EventType.APPROVAL_GRANT_REVOKE_REQUEST,
                            payload={"id": grant_id},
                        ),
                    )
        except BaseException as exc:  # noqa: BLE001 — captured, then re-raised as a test assertion below
            outcome["error"] = exc

    session = threading.Thread(target=run_session, daemon=True)
    session.start()
    session.join(timeout=30)

    assert not session.is_alive(), "WS grants route never responded (handler died?)"
    assert "error" not in outcome, f"WS grants route raised: {outcome.get('error')!r}"

    listed = outcome["listed"]
    assert listed.type == EventType.APPROVAL_GRANTS_RESPONSE
    assert [g["id"] for g in listed.payload["grants"]] == [grant_id]

    revoked = outcome["revoked"]
    assert revoked.type == EventType.APPROVAL_GRANT_REVOKE_RESPONSE
    assert revoked.payload["revoked"] is True
    assert revoked.payload["grants"] == []

    # And the grant is really gone, not just reported gone.
    assert seeded.list_grants() == []
    assert seeded.covering_grant(risk="write", args={"path": str(tmp_path / "proj" / "x")}) is None
