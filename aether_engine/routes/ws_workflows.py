"""Workflow websocket handlers (Full Furnish W-9).

The Settings → Workflows manager's and the Dashboard picker's
surface, split out of routes/websocket.py in the Wave 5 style:
the dispatch table there maps the WORKFLOW_* requests here.

Definitions and run records live in the workflow store
(``aether_engine/workflows/store.py``); runs are driven by
``aether_engine/workflows/runner.py``. Failures answer honestly
(success=False + error) — never a silent empty list, never a
fake success (a delete of a missing id says nothing was
deleted; a save against an unknown id says nothing was saved).

Patch doctrine (as in ws_calendar / ws_task_resume): the store
getter is resolved through this module at call time, and the
runner's seams resolve through the runner module, so suites can
patch either home.
"""

import asyncio
import logging
from typing import Any

from aether_common.contracts import Event, EventType
from aether_engine.routes.ws_connection import WsConnection
from aether_engine.workflows import runner
from aether_engine.workflows.store import get_workflow_store, public_run

logger = logging.getLogger("aether_engine.routes.ws_workflows")


async def _reply(
    conn: WsConnection, req_id: str | None, event_type: EventType,
    payload: dict[str, Any],
) -> None:
    await conn.ws.send_text(Event(
        type=event_type, request_id=req_id, payload=payload,
    ).to_json())


async def handle_workflow_list_request(
    conn: WsConnection, msg: Event,
) -> None:
    """All workflow definitions, in creation order."""
    req_id = msg.request_id
    try:
        workflows = await asyncio.to_thread(
            get_workflow_store().list_workflows)
        payload: dict[str, Any] = {"workflows": workflows}
    except Exception as exc:
        logger.exception("Workflow list failed")
        payload = {"workflows": [], "error": str(exc)}
    await _reply(conn, req_id, EventType.WORKFLOW_LIST_RESPONSE, payload)


async def handle_workflow_save_request(
    conn: WsConnection, msg: Event,
) -> None:
    """Create or update one definition (by id presence)."""
    req_id = msg.request_id
    data = (msg.payload or {}).get("workflow")
    try:
        saved = await asyncio.to_thread(
            get_workflow_store().save_workflow, data)
    except (ValueError, TypeError) as exc:
        await _reply(conn, req_id, EventType.WORKFLOW_SAVE_RESPONSE, {
            "success": False, "workflow": None, "error": str(exc)})
        return
    except Exception as exc:
        logger.exception("Workflow save failed")
        await _reply(conn, req_id, EventType.WORKFLOW_SAVE_RESPONSE, {
            "success": False, "workflow": None, "error": str(exc)})
        return
    if saved is None:
        await _reply(conn, req_id, EventType.WORKFLOW_SAVE_RESPONSE, {
            "success": False, "workflow": None,
            "error": "no workflow with that id — nothing was saved"})
        return
    await _reply(conn, req_id, EventType.WORKFLOW_SAVE_RESPONSE, {
        "success": True, "workflow": saved, "error": None})


async def handle_workflow_delete_request(
    conn: WsConnection, msg: Event,
) -> None:
    """Delete one definition by id — honestly."""
    req_id = msg.request_id
    workflow_id = (msg.payload or {}).get("workflow_id")
    if not workflow_id:
        await _reply(conn, req_id, EventType.WORKFLOW_DELETE_RESPONSE, {
            "success": False, "error": "workflow_id is required"})
        return
    try:
        removed = await asyncio.to_thread(
            get_workflow_store().delete_workflow, str(workflow_id))
    except Exception as exc:
        logger.exception("Workflow delete failed")
        await _reply(conn, req_id, EventType.WORKFLOW_DELETE_RESPONSE, {
            "success": False, "error": str(exc)})
        return
    if removed is None:
        await _reply(conn, req_id, EventType.WORKFLOW_DELETE_RESPONSE, {
            "success": False,
            "error": "no workflow with that id — nothing was deleted"})
        return
    await _reply(conn, req_id, EventType.WORKFLOW_DELETE_RESPONSE, {
        "success": True, "error": None})


async def handle_workflow_run_request(
    conn: WsConnection, msg: Event,
) -> None:
    """Start a run of one workflow. The run's steps then stream
    on this connection as ordinary tasks in the run's session,
    and WORKFLOW_RUN_STATUS events track the run itself."""
    req_id = msg.request_id
    workflow_id = (msg.payload or {}).get("workflow_id")
    if not workflow_id:
        await _reply(conn, req_id, EventType.WORKFLOW_RUN_RESPONSE, {
            "success": False, "run": None,
            "error": "workflow_id is required"})
        return
    store = get_workflow_store()
    # A stale active record from a dead process is closed as
    # interrupted before it can block or confuse this start.
    runner.close_interrupted_runs(store)
    workflow = await asyncio.to_thread(
        store.get_workflow, str(workflow_id))
    if workflow is None:
        await _reply(conn, req_id, EventType.WORKFLOW_RUN_RESPONSE, {
            "success": False, "run": None,
            "error": "no workflow with that id — nothing was started"})
        return
    try:
        record = await runner.start_run(conn, store, workflow)
    except runner.WorkflowRunRefused as exc:
        await _reply(conn, req_id, EventType.WORKFLOW_RUN_RESPONSE, {
            "success": False, "run": None, "error": str(exc)})
        return
    except Exception as exc:
        logger.exception("Workflow run start failed")
        await _reply(conn, req_id, EventType.WORKFLOW_RUN_RESPONSE, {
            "success": False, "run": None, "error": str(exc)})
        return
    await _reply(conn, req_id, EventType.WORKFLOW_RUN_RESPONSE, {
        "success": True, "run": public_run(record), "error": None})
    await runner.emit_run_status(conn, record)


async def handle_workflow_run_stop_request(
    conn: WsConnection, msg: Event,
) -> None:
    """Stop a live run: its current step task is cancelled and
    its later steps never start. The run record closes as
    ``stopped`` (watch WORKFLOW_RUN_STATUS for the close)."""
    req_id = msg.request_id
    run_id = (msg.payload or {}).get("run_id")
    if not run_id:
        await _reply(conn, req_id, EventType.WORKFLOW_RUN_STOP_RESPONSE, {
            "success": False, "error": "run_id is required"})
        return
    stopped, error = runner.stop_run(get_workflow_store(), str(run_id))
    await _reply(conn, req_id, EventType.WORKFLOW_RUN_STOP_RESPONSE, {
        "success": stopped, "error": error})


async def handle_workflow_run_list_request(
    conn: WsConnection, msg: Event,
) -> None:
    """Run records, newest first — the catch-up read for a
    surface that opened after transitions already happened.
    Stale active records are closed as interrupted first, so
    what this answers is never a run that silently died."""
    req_id = msg.request_id
    try:
        store = get_workflow_store()
        runner.close_interrupted_runs(store)
        runs = await asyncio.to_thread(store.list_runs)
        payload: dict[str, Any] = {
            "runs": [public_run(r) for r in runs]}
    except Exception as exc:
        logger.exception("Workflow run list failed")
        payload = {"runs": [], "error": str(exc)}
    await _reply(conn, req_id, EventType.WORKFLOW_RUN_LIST_RESPONSE, payload)
