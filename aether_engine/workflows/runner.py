"""Workflow runner (Full Furnish W-9).

Executes one workflow run: a named, ordered list of steps, each
step one ordinary Aether task in the run's own chat session.

Design notes:
- Steps go through the ordinary task path — the runner launches
  each step exactly as the START_TASK dispatch does (a launch
  chained onto ``conn.task_start_tail`` calling
  ``start_task_message``), so a step gets the normal routing,
  approvals, memory and fallback behaviour for free. Nothing
  about routing, delegation or the supervisor is touched here.
- Steps are strictly sequential: a step starts only after the
  previous step's pump has settled AND its outcome is known.
  Outcomes are read from the pump's own audit transitions (the
  task ledger's audit reader — written for every terminal event
  on both the fast-chat and graph paths), with the pending-
  approval scan and the session's messages as the fallback when
  the trail has nothing.
- Chaining is bounded: the next step's prompt is prefixed with
  an excerpt of the previous step's final text, capped at
  ``PREVIOUS_STEP_EXCERPT_CHARS`` with the trim visibly marked.
  The full result stays one click away in the run's session.
- A step that pauses for approval pauses the run
  (``awaiting_approval``); the ordinary approval flow resumes
  the task, and the runner simply observes the new pump and
  carries on when it settles. The runner never answers an
  approval itself.
- Runs live in-process (the registry below). A run whose driver
  is gone — engine restart, dropped connection — is NEVER
  silently continued: :func:`close_interrupted_runs` closes any
  persisted active run that has no live driver as
  ``interrupted`` (D-1 discipline).
- One live run per workflow and one live run per connection:
  the connection has a single task slot, so two runs sharing it
  would cancel each other's steps. A second start is refused
  with the reason, never queued or interleaved.

Patch doctrine (as in ws_task_exec / ws_task_resume): the task
launch and the pending-approvals scan resolve through
``aether_engine.routes.websocket`` at call time; the session
machinery and the audit reader are imported from their home
modules. Suites patch those homes.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from aether_common.contracts import Event, EventType, TaskState
from aether_engine.memory import session_store
from aether_engine.routes.ws_sessions import _create_bound_session
from aether_engine.task_ledger import read_audit_records
from aether_engine.workflows.store import WorkflowStore, public_run

logger = logging.getLogger("aether_engine.workflows.runner")

# The chain between steps carries at most this many characters
# of the previous step's final text (plus the trim marker).
PREVIOUS_STEP_EXCERPT_CHARS = 1000

# Settle-observation polling cadences. The pump's lifetime is
# measured in seconds; these only bound the runner's reaction
# time to a settlement / stop request.
_PUMP_POLL_SECONDS = 0.01
_RESUME_WAIT_POLL_SECONDS = 0.05


class WorkflowRunRefused(Exception):
    """A run start was refused; the message is the honest reason
    shown to the user (already running / connection busy)."""


@dataclass
class LiveRun:
    """One in-process run's driver state. The persisted record
    (in the store) is the truth clients read; this is only the
    machinery that advances it."""

    run_id: str
    workflow_id: str
    conn: Any
    stop_requested: bool = False
    driver: asyncio.Task | None = field(default=None)


_LIVE_RUNS: dict[str, LiveRun] = {}


def get_live_run(run_id: str) -> LiveRun | None:
    return _LIVE_RUNS.get(str(run_id))


def live_run_for_workflow(workflow_id: str) -> LiveRun | None:
    for live in _LIVE_RUNS.values():
        if live.workflow_id == str(workflow_id):
            return live
    return None


def live_run_on_connection(conn: Any) -> LiveRun | None:
    for live in _LIVE_RUNS.values():
        if live.conn is conn:
            return live
    return None


# ── Chaining ──────────────────────────────────────────────────


def excerpt_previous_result(text: str | None) -> str:
    """The bounded excerpt of a step's final text that the next
    step's prompt carries: at most PREVIOUS_STEP_EXCERPT_CHARS
    characters of the result, with the trim visibly marked when
    it happens."""
    text = (text or "").strip()
    if len(text) <= PREVIOUS_STEP_EXCERPT_CHARS:
        return text
    return (
        text[:PREVIOUS_STEP_EXCERPT_CHARS].rstrip()
        + "\n[… the rest of the previous step's result was trimmed "
        "to keep this step's prompt bounded — the full result is "
        "in this session's transcript]"
    )


def build_step_prompt(prompt: str, previous_title: str,
                      previous_text: str | None) -> str:
    """A step's effective prompt: the bounded excerpt of the
    previous step's result, then the step's own prompt in full.
    A previous step that produced no text chains nothing — the
    prompt runs as written."""
    excerpt = excerpt_previous_result(previous_text)
    if not excerpt:
        return prompt
    return (
        f"Result of the previous step (\"{previous_title}\"):\n"
        f"{excerpt}\n\n{prompt}"
    )


# ── Run lifecycle ─────────────────────────────────────────────


async def emit_run_status(conn: Any, record: dict[str, Any]) -> None:
    """Push the run's current shape to the client. Best-effort:
    the persisted record is the truth, this is only its live
    view — a dead socket must never kill a driver."""
    try:
        await conn.ws.send_text(Event(
            type=EventType.WORKFLOW_RUN_STATUS,
            request_id=record["run_id"],
            payload=public_run(record),
        ).to_json())
    except Exception:
        logger.debug("Workflow run status send failed", exc_info=True)


def close_interrupted_runs(store: WorkflowStore) -> list[str]:
    """Close persisted active runs that have no live driver as
    ``interrupted``; returns the run ids closed.

    This is where the restart discipline lands: runs are driven
    in-process, so an active record with no driver can only be
    a leftover from a dead process (or a dropped connection).
    It is called on the run read/start paths, so the close is
    visible the first time anyone looks — and a live run is
    never touched, because its driver is registered here.
    """
    closed: list[str] = []
    for record in store.active_runs():
        if record["run_id"] in _LIVE_RUNS:
            continue
        steps = record["steps"]
        for step in steps:
            if step.get("status") == "running":
                # The step in flight when the process died: it
                # will not continue. The run's own status carries
                # the interruption; the step reads as cancelled
                # (it neither completed nor failed on its own).
                step["status"] = "cancelled"
        store.update_run(
            record["run_id"], status="interrupted", steps=steps)
        closed.append(record["run_id"])
        logger.info(
            "Workflow run %s closed as interrupted (no live driver)",
            record["run_id"])
    return closed


async def start_run(conn: Any, store: WorkflowStore,
                    workflow: dict[str, Any]) -> dict[str, Any]:
    """Create a run of ``workflow`` and launch its driver.

    Raises WorkflowRunRefused when the workflow is already
    running or the connection already drives a run. Returns the
    persisted run record."""
    existing = live_run_for_workflow(workflow["id"])
    if existing is not None:
        raise WorkflowRunRefused(
            f"\"{workflow['name']}\" is already running — stop "
            "that run or wait for it to finish before starting "
            "another")
    busy = live_run_on_connection(conn)
    if busy is not None:
        raise WorkflowRunRefused(
            "another workflow run is already in progress on this "
            "connection — stop it or wait for it to finish first")
    session_id, _binding = await _create_bound_session(workflow["name"])
    record = {
        "run_id": uuid.uuid4().hex,
        "workflow_id": workflow["id"],
        "session_id": session_id,
        "status": "running",
        "current_step": 0,
        "steps": [
            {"title": step["title"], "status": "pending",
             "task_id": None}
            for step in workflow["steps"]
        ],
    }
    store.create_run(record)
    live = LiveRun(
        run_id=record["run_id"], workflow_id=workflow["id"], conn=conn)
    _LIVE_RUNS[live.run_id] = live
    # The definition is snapshotted for the run: editing the
    # workflow mid-run does not rewrite a run already in flight.
    snapshot = [dict(step) for step in workflow["steps"]]
    live.driver = conn.spawn_background(
        drive_run(live, store, snapshot))
    return store.get_run(live.run_id) or record


def stop_run(store: WorkflowStore, run_id: str) -> tuple[bool, str | None]:
    """Ask a live run to stop: flag it and cancel the step task
    currently occupying the connection (the CANCEL_TASK idiom).
    The driver observes the flag and closes the record as
    ``stopped`` — later steps never start. Returns (stopped,
    error): a run that is unknown or already finished is an
    honest error, never a fake success."""
    live = _LIVE_RUNS.get(str(run_id))
    if live is not None and not (
            live.driver is not None and live.driver.done()):
        live.stop_requested = True
        if _cancel_connection_task_work(live.conn):
            _send_task_cancelled(live.conn, live.run_id)
        # When the run is paused for approval there is no live
        # task work to cancel and no frame is sent: the paused
        # task stays in its session for the owner to answer —
        # the run simply stops waiting on it (its record closes
        # as stopped; the task itself is untouched).
        return True, None
    record = store.get_run(str(run_id))
    if record is None:
        return False, "no run with that id — nothing was stopped"
    return False, (
        f"that run is not live (it {record['status']}) — "
        "nothing was stopped")


def _cancel_connection_task_work(conn: Any) -> bool:
    """Cancel the connection's in-flight task work, exactly as
    the CANCEL_TASK handler does: a pending launch first, then
    the running pump. Returns True when anything was live."""
    cancelled = False
    tail = getattr(conn, "task_start_tail", None)
    if tail is not None and not tail.done():
        tail.cancel()
        cancelled = True
    runner = getattr(conn, "task_runner", None)
    if runner is not None and not runner.done():
        runner.cancel()
        cancelled = True
    return cancelled


def _send_task_cancelled(conn: Any, run_id: str) -> None:
    """Tell the client the step task was cancelled, mirroring
    the CANCEL_TASK handler's frame, so the run's session does
    not sit showing a task as running. Fire-and-forget."""
    async def _send() -> None:
        try:
            await conn.ws.send_text(Event(
                type=EventType.TASK_CANCELLED,
                request_id=f"workflow-{run_id}",
                payload={
                    "task_id": conn.active_task_id or "unknown",
                    "state": TaskState.CANCELLED.value,
                },
            ).to_json())
        except Exception:
            logger.debug(
                "Workflow stop: TASK_CANCELLED send failed",
                exc_info=True)

    try:
        asyncio.get_running_loop().create_task(_send())
    except RuntimeError:
        logger.debug("Workflow stop: no loop for TASK_CANCELLED send")


# ── The driver ────────────────────────────────────────────────


async def drive_run(live: LiveRun, store: WorkflowStore,
                    steps: list[dict[str, str]]) -> None:
    """Advance one run through its steps, in order. The single
    writer of the run record while the run is live."""
    conn = live.conn
    record = store.get_run(live.run_id)
    if record is None:  # the record was created by start_run; defensive
        _LIVE_RUNS.pop(live.run_id, None)
        return
    session_id = record["session_id"]

    def sync() -> None:
        store.update_run(
            live.run_id,
            status=record["status"],
            current_step=record["current_step"],
            steps=record["steps"],
            error=record.get("error"),
        )

    async def publish() -> None:
        sync()
        await emit_run_status(conn, record)

    try:
        previous_text: str | None = None
        previous_title = ""
        for index, step in enumerate(steps):
            if live.stop_requested:
                record["steps"][index]["status"] = "cancelled"
                for later in record["steps"][index + 1:]:
                    later["status"] = "skipped"
                record["status"] = "stopped"
                await publish()
                return
            record["current_step"] = index
            step_record = record["steps"][index]
            step_record["status"] = "running"
            await publish()

            prompt = step["prompt"]
            if index > 0:
                prompt = build_step_prompt(
                    prompt, previous_title, previous_text)

            on_task_id, on_pause, on_resume = _step_callbacks(
                record, step_record, publish)

            outcome = await _run_one_step(
                conn,
                session_id=session_id,
                prompt=prompt,
                request_id=f"workflow-{live.run_id}-{index}",
                live=live,
                on_task_id=on_task_id,
                on_pause=on_pause,
                on_resume=on_resume,
            )
            if outcome.task_id:
                step_record["task_id"] = outcome.task_id
            if outcome.kind == "completed":
                step_record["status"] = "completed"
                previous_text = outcome.final_text
                previous_title = step["title"]
                await publish()
                continue
            if outcome.kind == "failed":
                step_record["status"] = "failed"
                record["status"] = "failed"
                record["error"] = outcome.error
                for later in record["steps"][index + 1:]:
                    later["status"] = "skipped"
                await publish()
                return
            # cancelled — by the run's stop or by the owner
            # cancelling the step task in the session.
            step_record["status"] = "cancelled"
            record["status"] = "stopped"
            for later in record["steps"][index + 1:]:
                later["status"] = "skipped"
            await publish()
            return
        record["status"] = "completed"
        await publish()
    except asyncio.CancelledError:
        # The driver itself was cancelled (connection dropped /
        # engine going down): the record stays active with no
        # live driver, so the next close-check closes it as
        # interrupted — visible, never dressed as finished.
        raise
    except Exception as exc:  # a driver bug must still close the run honestly
        logger.exception("Workflow run %s failed internally", live.run_id)
        try:
            record["status"] = "failed"
            record["error"] = f"the workflow run failed internally: {exc}"
            sync()
            await emit_run_status(conn, record)
        except Exception:
            logger.debug(
                "Workflow run %s: failure close-out failed",
                live.run_id, exc_info=True)
    finally:
        _LIVE_RUNS.pop(live.run_id, None)


def _step_callbacks(record: dict[str, Any],
                    step_record: dict[str, Any], publish):
    """The driver's per-step observation callbacks for
    _run_one_step: the step's task id landing, the run pausing
    for an approval, the run resuming. A factory (not loop-body
    closures) so each step's callbacks bind its own record."""

    async def on_task_id(task_id: str) -> None:
        if step_record["task_id"] != task_id:
            step_record["task_id"] = task_id
            await publish()

    async def on_pause() -> None:
        record["status"] = "awaiting_approval"
        await publish()

    async def on_resume() -> None:
        record["status"] = "running"
        await publish()

    return on_task_id, on_pause, on_resume


# ── One step: launch + settle observation ─────────────────────


@dataclass
class _StepOutcome:
    kind: str  # "completed" | "failed" | "cancelled"
    task_id: str | None = None
    final_text: str = ""
    error: str | None = None


async def _run_one_step(
    conn: Any,
    *,
    session_id: str,
    prompt: str,
    request_id: str,
    live: LiveRun,
    on_task_id,
    on_pause,
    on_resume,
) -> _StepOutcome:
    """Launch one step task through the ordinary START_TASK path
    and wait until it truly settles — across approval pauses,
    which replace the pump — then classify the outcome."""
    from aether_engine.routes import websocket as ws_module

    before_count = await _session_message_count(session_id)
    msg = Event(
        type=EventType.START_TASK,
        request_id=request_id,
        payload={"prompt": prompt, "session_id": session_id},
    )
    # The START_TASK dispatch idiom: chain the launch after the
    # previous one so successive starts keep their order.
    previous_tail = conn.task_start_tail
    tail = asyncio.create_task(
        ws_module.start_task_message(conn, msg, request_id, previous_tail))
    conn.task_start_tail = tail

    task_id: str | None = None

    async def note_task_id(tid: str) -> None:
        nonlocal task_id
        if tid != task_id:
            task_id = tid
            await on_task_id(tid)

    seen_runner = conn.task_runner
    paused = False
    while True:
        if live.stop_requested:
            _cancel_connection_task_work(conn)
            return _StepOutcome("cancelled", task_id)
        runner = conn.task_runner
        if runner is not None and runner is not seen_runner:
            seen_runner = runner
            if paused:
                paused = False
                await on_resume()
            pump_cancelled = await _await_pump(
                conn, runner, live, note_task_id)
            if live.stop_requested:
                return _StepOutcome("cancelled", task_id)
            outcome = await _classify_settlement(
                task_id=task_id,
                session_id=session_id,
                before_count=before_count,
                pump_cancelled=pump_cancelled,
            )
            if outcome is not None:
                outcome.task_id = task_id or outcome.task_id
                return outcome
            # The step task paused for approval: the run waits
            # for the ordinary approval flow to resume it (a new
            # pump on this connection) and settles it there.
            paused = True
            await on_pause()
            continue
        if tail.done() and not paused and task_id is None:
            # The launch finished without ever creating a pump:
            # the start was answered with an error event instead
            # (e.g. the provider could not be built). No task
            # exists to wait for.
            exc = tail.exception() if not tail.cancelled() else None
            detail = f": {exc}" if exc is not None else ""
            return _StepOutcome(
                "failed", task_id,
                error="the step task could not be started" + detail)
        await asyncio.sleep(_RESUME_WAIT_POLL_SECONDS)


async def _await_pump(conn: Any, runner: asyncio.Task, live: LiveRun,
                      note_task_id) -> bool:
    """Wait out one task pump, capturing the task id it names
    (the pump records it on the connection at TASK_CREATED) and
    honouring the run's stop flag. Returns True when the pump
    was cancelled. Polling — not awaiting — so a cancelled pump
    never raises into the driver."""
    while not runner.done():
        if conn.active_task_id:
            await note_task_id(str(conn.active_task_id))
        if live.stop_requested:
            runner.cancel()
        await asyncio.sleep(_PUMP_POLL_SECONDS)
    if conn.active_task_id:
        await note_task_id(str(conn.active_task_id))
    if runner.cancelled():
        return True
    exc = runner.exception()
    if exc is not None:
        # The pump converts its own failures into ERROR events;
        # an exception escaping it is logged here and the
        # settlement classification decides the outcome.
        logger.warning("Workflow step pump ended with an error: %s", exc)
    return False


async def _classify_settlement(
    *,
    task_id: str | None,
    session_id: str,
    before_count: int,
    pump_cancelled: bool,
) -> _StepOutcome | None:
    """Decide a settled pump's outcome. Returns None when the
    step task has only paused for approval — the caller keeps
    waiting for its resume."""
    if pump_cancelled:
        return _StepOutcome("cancelled", task_id)
    transitions: list[dict[str, Any]] = []
    if task_id:
        records = await asyncio.to_thread(read_audit_records, task_id)
        transitions = records.get("transitions") or []
    if transitions:
        last = transitions[-1].get("to_state")
        if last == TaskState.SUCCEEDED.value:
            text = await _latest_assistant_text(session_id, before_count)
            return _StepOutcome("completed", task_id, final_text=text or "")
        if last == TaskState.FAILED.value:
            return _StepOutcome(
                "failed", task_id, error="the step task failed")
        if last == TaskState.CANCELLED.value:
            return _StepOutcome("cancelled", task_id)
        if last == TaskState.PENDING_APPROVAL.value:
            return None
    # The audit trail is one writer among several and may have
    # nothing for this task; check the live seams before
    # declaring an outcome.
    if await _has_pending_approval(session_id):
        return None
    text = await _latest_assistant_text(session_id, before_count)
    if text is not None:
        return _StepOutcome("completed", task_id, final_text=text)
    return _StepOutcome(
        "failed", task_id,
        error="the step task ended without a recorded outcome")


async def _has_pending_approval(session_id: str) -> bool:
    """Is a task on this session's thread waiting for an
    approval decision? Resolved through the websocket module,
    like the resume path's identical scan."""
    from aether_engine.routes import websocket as ws_module

    try:
        pending = await ws_module.get_pending_approvals()
    except Exception:
        logger.debug(
            "Workflow pending-approval scan failed", exc_info=True)
        return False
    return any(
        p.get("thread_id") == session_id for p in pending or [])


async def _session_message_count(session_id: str) -> int:
    session = await asyncio.to_thread(session_store.get_session, session_id)
    if not session:
        return 0
    return len(session.get("messages") or [])


async def _latest_assistant_text(
        session_id: str, before_count: int) -> str | None:
    """The newest assistant message the step added to its
    session, or None when it added none. The pump persists the
    final response as an assistant message on completion (both
    chat paths), so this is the step's final text."""
    session = await asyncio.to_thread(session_store.get_session, session_id)
    if not session:
        return None
    messages = session.get("messages") or []
    for message in reversed(messages[before_count:]):
        if message.get("role") == "assistant":
            content = message.get("content")
            return content if isinstance(content, str) else None
    return None
