"""Task Ledger — the Mission Control read model (master build S3).

There is no single task ledger store, and this module does not create
one. A finished (or running) task's record is written by five existing
writers, each owning one slice of the truth:

1. **Checkpointed graph state** (``langgraph/checkpointer.py`` →
   ``checkpoints.db``): ``delegation_log``, the shared blackboard,
   ``artifact_paths``, ``structured_outputs`` and the message channel
   (which carries every tool call and its result). Keyed by *thread*
   (the session id), not by task — see the scan note below.
2. **The executor's event stream** (``langgraph/executor.py``, run and
   resume paths): ``NODE_ACTIVITY`` / ``TOOL_ACTIVITY`` translations,
   approvals and terminal events. This stream is fire-and-forget to
   the UI — nothing persisted it. The bounded in-memory tap in this
   module (``record_executor_event``) is the one new retention S3
   adds, and it is deliberately small: at most
   ``EVENT_TAP_TASK_CAP`` tasks × ``EVENT_TAP_EVENT_CAP`` tool/node
   records. It is a live-view cache, never a store of record: an
   engine restart empties it, and the summary says so.
3. **Episodic memory** (via ``memory/facade.py``): the outcome record
   written on completion — prompt, summary, full outcome, tags.
4. **The audit trail** (``audit.py`` → ``~/.aether/logs/audit.log``):
   ``TASK_TRANSITION`` lines (payloads arrive redacted to their key
   shape by design, so transitions contribute state + timestamps,
   never payload contents) and ``APPROVAL_REQUESTED`` events written
   by the websocket pump.
5. **The usage ledger** (``cost/ledger.py``): per-call rows joined by
   ``task_id`` (+ ``agent_name``) — models used and what they cost.

``task_summary()`` joins those five for one task;
``recent_tasks()`` lists the tasks the episodic spine + the live tap
know about; ``task_briefing()`` renders a summary as bounded text for
the supervisor (the ``blackboard_briefing()`` pattern — S4 wires it
into prompts; this module only formats).

Honesty rules, mirroring the writers' own contracts:

- An unknown task returns ``found: False`` with empty sections —
  never a fabricated record dressed up as a task.
- Every section names its source(s). Anything a writer does not
  retain is reported as absent, not reconstructed by guesswork:
  tool-call records come from checkpointed messages or the live tap;
  a task whose checkpoint thread falls outside the bounded thread
  scan, whose episode is older than the bounded episode scan, or
  which ran before the tap existed will show a partial summary with
  the gap named in ``coverage.notes``.
- Only the live audit file is read. Rotated archives (``audit.log.N.gz``)
  are not scanned, so very old transitions age out of this view while
  remaining on disk for forensics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

logger = logging.getLogger("aether_engine.task_ledger")

# ── Bounds (everything this module retains or scans is capped) ──────
#: Tasks held by the in-memory executor tap (LRU — oldest evicted).
EVENT_TAP_TASK_CAP = 64
#: Tool + node records kept per tapped task.
EVENT_TAP_EVENT_CAP = 200
#: Episodes scanned (newest first) when looking up one task's outcome.
EPISODE_SCAN_LIMIT = 500
#: Checkpoint threads scanned when no session/thread hint is given.
THREAD_SCAN_LIMIT = 64
#: Audit records (transitions + approvals + other events) kept per task.
AUDIT_RECORD_CAP = 200
#: Default / maximum sizes for recent_tasks().
RECENT_TASKS_DEFAULT = 20
RECENT_TASKS_CAP = 100
#: Bounded text fields in a summary (the full outcome lives in the
#: episode record itself; the ledger carries an excerpt).
OUTCOME_EXCERPT_CHARS = 1000
BRIEFING_MAX_CHARS = 1500

_WRITE_TOOL_NAMES = frozenset({"write_file", "edit_file", "create_directory"})
_TEST_TOOL_NAMES = frozenset({"run_checks"})
_TEST_COMMAND_MARKERS = ("pytest", "jest", "npm test", "npm run test", "vitest")


# ── 2. The executor event tap (bounded, in-memory, live-only) ───────
#
# Both executor paths (run_langgraph_task's emit funnel + direct
# terminal yields, and resume_langgraph_task's segment funnel + direct
# terminal yields) call record_executor_event() for every event they
# hand to their caller. Recording is observational: it never raises
# and never alters the event.

_TAP: OrderedDict[str, dict[str, Any]] = OrderedDict()


def _tap_entry(task_id: str) -> dict[str, Any]:
    entry = _TAP.get(task_id)
    if entry is None:
        entry = {
            "task_id": task_id,
            "first_ts": time.time(),
            "last_ts": time.time(),
            "session_id": None,
            "state": None,
            "latency_ms": None,
            "error": None,
            "delegations": [],
            "tool_calls": [],
            "node_activity": [],
            "approvals": [],
        }
        _TAP[task_id] = entry
        while len(_TAP) > EVENT_TAP_TASK_CAP:
            _TAP.popitem(last=False)
    else:
        _TAP.move_to_end(task_id)
    entry["last_ts"] = time.time()
    return entry


def _tap_append(entry: dict[str, Any], key: str, record: dict[str, Any]) -> None:
    records = entry[key]
    records.append(record)
    if len(records) > EVENT_TAP_EVENT_CAP:
        del records[: len(records) - EVENT_TAP_EVENT_CAP]


def record_executor_event(task_id: str | None, event: Any) -> None:
    """Record one executor-emitted event in the bounded live tap.

    Called from BOTH executor paths (run + resume). Best-effort by
    doctrine, like every observational writer in the executor: a tap
    failure is logged at debug and never affects the task.
    """
    if not task_id or event is None:
        return
    try:
        from aether_common.contracts import EventType

        etype = event.type
        payload = dict(event.payload or {})
        entry = _tap_entry(str(task_id))
        if payload.get("session_id"):
            entry["session_id"] = payload["session_id"]
        ts = payload.get("timestamp") or time.time()

        if etype == EventType.NODE_ACTIVITY:
            action = payload.get("action")
            # A supervisor decision of "END" grows delegation_log too,
            # but it is a finish decision, not a delegation — it is
            # recorded as plain node activity, never as a delegation.
            if (action in ("delegation", "delegation_denied")
                    and str(payload.get("to_node") or "").lower() != "end"):
                decision = (payload.get("output_payload") or {}).get("decision") or {}
                _tap_append(entry, "delegations", {
                    "agent": payload.get("to_node"),
                    "reason": payload.get("reason", ""),
                    "briefing": payload.get("briefing", ""),
                    "denied": bool(decision.get("denied")) or action == "delegation_denied",
                    "timestamp": ts,
                    "source": "executor_event",
                })
            else:
                _tap_append(entry, "node_activity", {
                    "from_node": payload.get("from_node"),
                    "to_node": payload.get("to_node"),
                    "action": action,
                    "status": payload.get("status"),
                    "timestamp": ts,
                    "source": "executor_event",
                })
        elif etype == EventType.TOOL_ACTIVITY:
            record = {
                "tool_name": payload.get("tool_name", "unknown"),
                "tool_args": payload.get("tool_args") or {},
                "status": payload.get("status"),
                "node": payload.get("node") or payload.get("from_node"),
                "edited": bool(payload.get("edited")),
                "timestamp": ts,
                "source": "executor_event",
            }
            if record["status"] == "pending":
                _tap_append(entry, "tool_calls", record)
            else:
                # Complete the matching pending record (the executor's
                # own record_thought merges the same way); with no
                # pending twin — e.g. a resume path tool result whose
                # pending half predates the tap — keep it standalone.
                for prev in reversed(entry["tool_calls"]):
                    if (prev["tool_name"] == record["tool_name"]
                            and prev["status"] == "pending"
                            and prev.get("node") == record.get("node")):
                        prev["status"] = record["status"]
                        prev["edited"] = record["edited"]
                        if record["tool_args"]:
                            prev["tool_args"] = record["tool_args"]
                        break
                else:
                    _tap_append(entry, "tool_calls", record)
        elif etype == EventType.TOOL_APPROVAL_REQUEST:
            _tap_append(entry, "approvals", {
                "tool_name": payload.get("tool_name"),
                "timestamp": ts,
                "pending": True,
                "source": "executor_event",
            })
        elif etype in (EventType.TASK_COMPLETED, EventType.TASK_FAILED,
                       EventType.TASK_INTERRUPTED, EventType.TASK_CANCELLED):
            entry["state"] = payload.get("state") or etype.value
            if payload.get("latency_ms") is not None:
                entry["latency_ms"] = payload["latency_ms"]
            if etype == EventType.TASK_FAILED:
                entry["error"] = payload.get("error")
    except Exception:
        logger.debug("Task ledger tap failed to record an event", exc_info=True)


def tap_snapshot(task_id: str) -> dict[str, Any] | None:
    """A copy of one task's live-tap entry, or None if not tapped."""
    entry = _TAP.get(str(task_id))
    if entry is None:
        return None
    return json.loads(json.dumps(entry, default=str))


def tap_task_ids() -> list[str]:
    """Task ids currently held by the tap, most recently active first."""
    return list(reversed(_TAP))


def clear_tap(task_id: str | None = None) -> None:
    """Drop tap state (tests; an engine restart does this naturally)."""
    if task_id is None:
        _TAP.clear()
    else:
        _TAP.pop(str(task_id), None)


# ── 4. Audit trail reader ────────────────────────────────────────────

def read_audit_records(
    task_id: str, *, log_path: Path | str | None = None,
) -> dict[str, Any]:
    """Read one task's records from the live audit log file.

    Returns {"transitions": [...], "approvals": [...],
    "events": [...]}. Only the live file is read (rotated ``.gz``
    archives are forensics, not this view). A missing/unreadable log
    yields empty lists — the audit trail is one writer of five, and
    its absence must not fail a summary.
    """
    from aether_engine.audit import DEFAULT_AUDIT_LOG_PATH

    path = Path(log_path) if log_path else DEFAULT_AUDIT_LOG_PATH
    out: dict[str, list[dict[str, Any]]] = {
        "transitions": [], "approvals": [], "events": []}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or f'"{task_id}"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") == "TASK_TRANSITION" and record.get("task_id") == task_id:
            out["transitions"].append({
                "from_state": record.get("from_state"),
                "to_state": record.get("to_state"),
                "timestamp": record.get("ts"),
                "source": "audit_log",
            })
        elif record.get("type") == "ENGINE_EVENT":
            details = record.get("details") or {}
            if details.get("task_id") != task_id:
                continue
            if record.get("action") == "APPROVAL_REQUESTED":
                out["approvals"].append({
                    "tool_name": details.get("tool_name"),
                    "timestamp": record.get("ts"),
                    "pending": None,  # the trail records the ask, not the answer
                    "source": "audit_log",
                })
            else:
                out["events"].append({
                    "action": record.get("action"),
                    "timestamp": record.get("ts"),
                    "source": "audit_log",
                })
        for key, records in out.items():
            if len(records) > AUDIT_RECORD_CAP:
                out[key] = records[-AUDIT_RECORD_CAP:]
    return out


# ── 3. Episodic outcome reader ───────────────────────────────────────

def episode_for_task(
    task_id: str, *, backend: Any | None = None, user_config: Any | None = None,
) -> dict[str, Any] | None:
    """One task's episode record via the memory facade, or None.

    The facade's read API lists episodes newest-first with no by-id
    getter, so this scans at most ``EPISODE_SCAN_LIMIT`` recent
    episodes; an older episode is reported as absent and the summary
    coverage names the bound. An unavailable backend (the facade's
    loud-degradation contract) also yields None.
    """
    if backend is None:
        from aether_engine.memory.facade import backend_or_none

        backend = backend_or_none(user_config, context="task ledger episode read")
        if backend is None:
            return None
    try:
        episodes = backend.list_episodes(limit=EPISODE_SCAN_LIMIT)
    except Exception:
        logger.debug("Task ledger episode scan failed", exc_info=True)
        return None
    for episode in episodes or []:
        if isinstance(episode, dict) and episode.get("task_id") == task_id:
            return episode
    return None


# ── 5. Usage ledger reader ───────────────────────────────────────────

def usage_for_task(
    task_id: str, *, ledger: Any | None = None,
) -> dict[str, Any]:
    """One task's usage-ledger join: rows + totals + per-model rollup.

    Uses ``UsageLedger.query(task_id=...)`` — the existing query API,
    whose totals aggregate the same filter as its entries.
    """
    if ledger is None:
        from aether_engine.cost.ledger import get_usage_ledger

        ledger = get_usage_ledger()
    try:
        result = ledger.query(task_id=task_id)
    except Exception:
        logger.debug("Task ledger usage query failed", exc_info=True)
        return {"entries": [], "totals": {}, "models": []}
    entries = result.get("entries") or []
    models: dict[tuple, dict[str, Any]] = {}
    for row in entries:
        key = (row.get("provider_name"), row.get("model_name"), row.get("agent_name"))
        rollup = models.setdefault(key, {
            "provider": row.get("provider_name"),
            "model": row.get("model_name"),
            "agent": row.get("agent_name"),
            "calls": 0, "cost_usd": 0.0,
            "input_tokens": 0, "output_tokens": 0,
        })
        rollup["calls"] += 1
        rollup["cost_usd"] = round(rollup["cost_usd"] + float(row.get("cost_usd") or 0.0), 10)
        rollup["input_tokens"] += int(row.get("input_tokens") or 0)
        rollup["output_tokens"] += int(row.get("output_tokens") or 0)
    return {
        "entries": entries,
        "totals": result.get("totals") or {},
        "models": sorted(models.values(), key=lambda m: (str(m["agent"]), str(m["model"]))),
    }


# ── 1. Checkpointed graph state reader ───────────────────────────────

def _tool_calls_from_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """Reconstruct tool calls from the checkpointed message channel.

    Assistant messages carry the calls (``tool_calls``); tool-role
    messages carry each call's args + result. Pairing is sequential,
    exactly as the executor's translation reads them; a result whose
    text starts with "Error"/"Validation failed" is a failure — the
    same test the executor applies.
    """
    calls: list[dict[str, Any]] = []
    pending_by_name: dict[str, list[dict[str, Any]]] = {}
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args_raw = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw or {})
                except (TypeError, ValueError):
                    args = {}
                record = {
                    "tool_name": fn.get("name", "unknown"),
                    "tool_args": args,
                    "status": "pending",
                    "node": None,
                    "edited": False,
                    "timestamp": None,
                    "source": "checkpoint_state",
                }
                calls.append(record)
                pending_by_name.setdefault(record["tool_name"], []).append(record)
        elif msg.get("role") == "tool":
            name = msg.get("name", "unknown")
            result = msg.get("content", "")
            record = None
            queue = pending_by_name.get(name) or []
            if queue:
                record = queue.pop(0)
            if record is None:
                record = {
                    "tool_name": name,
                    "tool_args": msg.get("args") or {},
                    "status": "pending",
                    "node": None,
                    "edited": bool(msg.get("edited")),
                    "timestamp": None,
                    "source": "checkpoint_state",
                }
                calls.append(record)
            record["status"] = (
                "failed" if isinstance(result, str)
                and result.startswith(("Error", "Validation failed"))
                else "completed")
            record["edited"] = bool(msg.get("edited"))
            if msg.get("args"):
                record["tool_args"] = msg["args"]
    return calls


def _state_slice(values: dict[str, Any]) -> dict[str, Any]:
    """The ledger's slice of one checkpointed graph state."""
    from aether_engine.langgraph.state import blackboard_briefing

    delegations = []
    for entry in values.get("delegation_log") or []:
        if not isinstance(entry, dict):
            continue
        decision = entry.get("decision") or {}
        # The log also records the supervisor's finish decision
        # (next="END") — a decision, not a delegation; excluded here
        # exactly as in the live tap.
        if not decision.get("next") or str(decision["next"]).upper() == "END":
            continue
        delegations.append({
            "agent": decision.get("next"),
            "reason": decision.get("reason", ""),
            "briefing": decision.get("briefing", ""),
            "denied": bool(decision.get("denied")),
            "timestamp": entry.get("timestamp"),
            "source": "checkpoint_state",
        })
    errors = []
    for agent, output in (values.get("structured_outputs") or {}).items():
        if isinstance(output, dict) and output.get("error"):
            errors.append({
                "source": "checkpoint_state",
                "agent": agent,
                "message": str(output["error"])[:500],
            })
    board = values.get("blackboard") or {}
    files = [str(p) for p in (board.get("artifacts") or [])]
    for path in (values.get("artifact_paths") or {}).values():
        if str(path) not in files:
            files.append(str(path))
    return {
        "session_id": values.get("session_id"),
        "prompt": values.get("original_prompt"),
        "phase": values.get("current_phase"),
        "plan": values.get("plan"),
        "active_specialist": values.get("active_specialist"),
        # S4 (additive): the authoritative per-turn delegation
        # counter, when the checkpoint carries one (checkpoints from
        # before S4 have no such field and report None).
        "delegation_count": values.get("delegation_count"),
        "delegations": delegations,
        "tool_calls": _tool_calls_from_messages(values.get("messages") or []),
        "files_touched": files,
        "errors": errors,
        "blackboard_briefing": blackboard_briefing(values) or None,
        "run_config": values.get("run_config"),
    }


async def checkpoint_slice_for_task(
    task_id: str, *, session_id: str | None = None,
    checkpoint_db: Path | str | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Find one task's checkpointed state slice.

    Returns (slice, scan_capped). Checkpoints are keyed by thread
    (session id); with a ``session_id`` hint only that thread is read,
    otherwise distinct threads are scanned newest-activity-first up to
    ``THREAD_SCAN_LIMIT`` — a task whose thread falls outside the cap
    reports scan_capped=True so the summary can name the gap instead
    of silently omitting the writer. Follows the
    ``get_pending_approvals`` pattern in the executor (which this
    module cannot import — the executor imports this one for the tap).
    """
    import sqlite3

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from aether_engine.langgraph.checkpointer import DEFAULT_CHECKPOINT_DB
    from aether_engine.langgraph.graph import compile_graph

    db_path = Path(checkpoint_db) if checkpoint_db else DEFAULT_CHECKPOINT_DB
    if not db_path.exists():
        return None, False
    if session_id:
        thread_ids = [str(session_id)]
        scan_capped = False
    else:
        try:
            with sqlite3.connect(db_path) as conn:
                thread_ids = [str(r[0]) for r in conn.execute(
                    "SELECT thread_id FROM checkpoints "
                    "GROUP BY thread_id ORDER BY MAX(checkpoint_id) DESC")]
        except Exception:
            logger.debug("Task ledger thread scan failed", exc_info=True)
            return None, False
        scan_capped = len(thread_ids) > THREAD_SCAN_LIMIT
        thread_ids = thread_ids[:THREAD_SCAN_LIMIT]
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        graph = compile_graph(checkpointer=saver)
        for thread_id in thread_ids:
            try:
                state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            except Exception:  # per-thread isolation, as in get_pending_approvals
                logger.debug(
                    "Task ledger checkpoint read failed for thread %s",
                    thread_id, exc_info=True)
                continue
            values = state.values or {}
            if values.get("task_id") != task_id:
                continue
            slice_ = _state_slice(values)
            # A thread blocked on an approval carries its pending ask
            # as an interrupt — the checkpoint's approval record.
            for task in state.tasks or []:
                for intr in task.interrupts or []:
                    val = intr.value
                    if isinstance(val, dict) and val.get("type") == "approval_request":
                        slice_.setdefault("pending_approvals", []).append({
                            "tool_name": val.get("tool_name"),
                            "timestamp": None,
                            "pending": True,
                            "source": "checkpoint_state",
                        })
            return slice_, scan_capped
    return None, scan_capped


# ── Join helpers ─────────────────────────────────────────────────────

def _is_test_call(call: dict[str, Any]) -> bool:
    if call.get("tool_name") in _TEST_TOOL_NAMES:
        return True
    if call.get("tool_name") == "execute_shell":
        command = str((call.get("tool_args") or {}).get("command", ""))
        return any(marker in command for marker in _TEST_COMMAND_MARKERS)
    return False


def _files_from_tool_calls(calls: list[dict[str, Any]]) -> list[str]:
    files: list[str] = []
    for call in calls:
        if call.get("tool_name") in _WRITE_TOOL_NAMES and call.get("status") == "completed":
            args = call.get("tool_args") or {}
            path = args.get("path") or args.get("file_path")
            if path and str(path) not in files:
                files.append(str(path))
    return files


def _merge_approvals(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge approval records of the same ask across writers.

    The same approval appears in the tap (executor event), the audit
    trail (pump record) and — while pending — the checkpoint. Writers
    are paired by occurrence: the n-th record for a tool from one
    writer merges with the n-th record for that tool from the others
    (timestamps cannot pair them — each writer stamps its own clock
    at its own moment). Sources are unioned so provenance survives.
    """
    merged: list[dict[str, Any]] = []
    seen: dict[tuple, int] = {}
    for group in groups:
        for record in group or []:
            tool = record.get("tool_name")
            occurrence = seen.get((record.get("source"), tool), 0)
            seen[(record.get("source"), tool)] = occurrence + 1
            same_tool = [m for m in merged if m["tool_name"] == tool]
            match = same_tool[occurrence] if occurrence < len(same_tool) else None
            if match is None:
                merged.append({
                    "tool_name": tool,
                    "timestamp": record.get("timestamp"),
                    "pending": record.get("pending"),
                    "sources": [record.get("source")],
                })
            else:
                if record.get("source") not in match["sources"]:
                    match["sources"].append(record.get("source"))
                if record.get("pending"):
                    match["pending"] = True
                if match.get("timestamp") is None:
                    match["timestamp"] = record.get("timestamp")
    return merged


# ── The joined summary ───────────────────────────────────────────────

async def task_summary(
    task_id: str, *, session_id: str | None = None,
    user_config: Any | None = None, backend: Any | None = None,
    ledger: Any | None = None, audit_path: Path | str | None = None,
    checkpoint_db: Path | str | None = None,
) -> dict[str, Any]:
    """Join the five writers into one per-task summary.

    ``found`` is True when at least one writer knows the task. For an
    unknown task every section is empty and ``found`` is False — an
    honest not-found, never fabricated data.
    """
    task_id = str(task_id)
    tap = tap_snapshot(task_id)
    audit = await asyncio.to_thread(read_audit_records, task_id, log_path=audit_path)
    episode = await asyncio.to_thread(
        episode_for_task, task_id, backend=backend, user_config=user_config)
    usage = await asyncio.to_thread(usage_for_task, task_id, ledger=ledger)
    checkpoint, scan_capped = await checkpoint_slice_for_task(
        task_id, session_id=session_id or (tap or {}).get("session_id")
        or (episode or {}).get("session_id"), checkpoint_db=checkpoint_db)

    delegations = (checkpoint or {}).get("delegations") or (tap or {}).get("delegations") or []
    tool_calls = (checkpoint or {}).get("tool_calls") or (tap or {}).get("tool_calls") or []
    files = list((checkpoint or {}).get("files_touched") or [])
    for path in _files_from_tool_calls(tool_calls):
        if path not in files:
            files.append(path)
    approvals = _merge_approvals(
        audit["approvals"], (tap or {}).get("approvals") or [],
        (checkpoint or {}).get("pending_approvals") or [])

    errors: list[dict[str, Any]] = list((checkpoint or {}).get("errors") or [])
    if tap and tap.get("error"):
        errors.append({"source": "executor_event", "message": str(tap["error"])[:500]})
    for call in tool_calls:
        if call.get("status") == "failed":
            errors.append({
                "source": call.get("source"),
                "message": f"Tool call failed: {call.get('tool_name')}",
            })

    # Outcome state: the live tap's terminal event is freshest, then
    # the audit trail's last transition, then a pending checkpoint
    # interrupt, then the episode (which only completed tasks write).
    state = None
    if tap and tap.get("state"):
        state = tap["state"]
    elif audit["transitions"]:
        state = audit["transitions"][-1].get("to_state")
    elif approvals and any(a.get("pending") for a in approvals):
        state = "PENDING_APPROVAL"
    elif episode is not None:
        state = "SUCCEEDED"

    found = bool(
        checkpoint or episode or tap or audit["transitions"]
        or audit["approvals"] or usage["entries"])

    notes: list[str] = []
    if checkpoint is None:
        notes.append(
            "No checkpointed graph state found for this task"
            + (" within the bounded thread scan" if scan_capped else "")
            + " — delegations/tool calls fall back to the live tap.")
    if tap is None:
        notes.append(
            "No live-tap records: the task ran before this build's tap "
            "existed or in a previous engine process (the tap is "
            "in-memory and bounded).")
    if episode is None:
        notes.append(
            f"No episode record within the {EPISODE_SCAN_LIMIT} most "
            "recent episodes — only completed tasks write one.")

    response = (episode or {}).get("outcome") or ""
    return {
        "task_id": task_id,
        "found": found,
        "session_id": (
            (checkpoint or {}).get("session_id")
            or (tap or {}).get("session_id")
            or (episode or {}).get("session_id")),
        "prompt": (
            (checkpoint or {}).get("prompt")
            or (episode or {}).get("user_prompt")),
        "state": state,
        "phase": (checkpoint or {}).get("phase"),
        "latency_ms": (tap or {}).get("latency_ms"),
        "outcome": {
            "status": state,
            "summary": (episode or {}).get("task_summary"),
            "response_excerpt": response[:OUTCOME_EXCERPT_CHARS] or None,
            "response_truncated": len(response) > OUTCOME_EXCERPT_CHARS,
            "tags": (episode or {}).get("tags"),
            "recorded_at": (episode or {}).get("timestamp"),
        },
        "delegations": delegations,
        # S4 (additive): the checkpointed delegation counter for the
        # task's turn, or None when no checkpoint carries one.
        "delegation_count": (checkpoint or {}).get("delegation_count"),
        "tool_calls": tool_calls,
        "tests": [
            {"tool_name": c.get("tool_name"), "status": c.get("status"),
             "node": c.get("node"), "source": c.get("source")}
            for c in tool_calls if _is_test_call(c)
        ],
        "approvals": approvals,
        "files_touched": files,
        "models": usage["models"],
        "costs": usage["totals"],
        "errors": errors,
        "transitions": audit["transitions"],
        "blackboard_briefing": (checkpoint or {}).get("blackboard_briefing"),
        "coverage": {
            "checkpoint": checkpoint is not None,
            "episode": episode is not None,
            "audit_records": (
                len(audit["transitions"]) + len(audit["approvals"])
                + len(audit["events"])),
            "usage_calls": len(usage["entries"]),
            "live_tap": tap is not None,
            "notes": notes,
        },
    }


def recent_tasks(
    limit: int = RECENT_TASKS_DEFAULT, *, backend: Any | None = None,
    user_config: Any | None = None, ledger: Any | None = None,
) -> list[dict[str, Any]]:
    """Recent tasks, newest first — the listing spine for Mission Control.

    "Recent" is honestly scoped: tasks with an episode record
    (completed graph/fast-chat tasks), plus tasks the live tap has
    seen in this engine process (running, paused or just-finished
    work that has not written an episode yet). A task that failed
    before writing an episode and was never tapped does not appear —
    it leaves its trace in the audit trail and usage ledger instead.
    Each row carries its usage-ledger cost total so a list can show
    spend without a per-task summary join.
    """
    try:
        limit = max(1, min(int(limit), RECENT_TASKS_CAP))
    except (TypeError, ValueError):
        limit = RECENT_TASKS_DEFAULT
    rows: dict[str, dict[str, Any]] = {}
    if backend is None:
        from aether_engine.memory.facade import backend_or_none

        backend = backend_or_none(user_config, context="task ledger recent list")
    episodes: list[dict[str, Any]] = []
    if backend is not None:
        try:
            episodes = backend.list_episodes(limit=max(limit, EPISODE_SCAN_LIMIT)) or []
        except Exception:
            logger.debug("Task ledger recent-list episode scan failed", exc_info=True)
            episodes = []
    for episode in episodes:
        if not isinstance(episode, dict) or not episode.get("task_id"):
            continue
        rows[episode["task_id"]] = {
            "task_id": episode["task_id"],
            "session_id": episode.get("session_id"),
            "prompt": episode.get("user_prompt"),
            "summary": episode.get("task_summary"),
            "state": "SUCCEEDED",
            "timestamp": episode.get("timestamp"),
            "sources": ["episodic_memory"],
        }
    for task_id in tap_task_ids():
        tap = tap_snapshot(task_id) or {}
        row = rows.setdefault(task_id, {
            "task_id": task_id,
            "session_id": tap.get("session_id"),
            "prompt": None,
            "summary": None,
            "state": tap.get("state"),
            "timestamp": tap.get("last_ts"),
            "sources": [],
        })
        if "executor_event" not in row["sources"]:
            row["sources"].append("executor_event")
        if tap.get("state"):
            row["state"] = tap["state"]
        row["timestamp"] = max(row.get("timestamp") or 0, tap.get("last_ts") or 0)
    ordered = sorted(
        rows.values(), key=lambda r: float(r.get("timestamp") or 0), reverse=True)
    result = ordered[:limit]
    for row in result:
        try:
            row["cost_usd"] = usage_for_task(row["task_id"], ledger=ledger)["totals"].get(
                "total_cost_usd", 0.0)
        except Exception:  # noqa: BLE001 — cost is an enrichment, not the listing
            row["cost_usd"] = None
    return result


def task_briefing(summary: dict[str, Any], *, max_chars: int = BRIEFING_MAX_CHARS) -> str:
    """Render one task summary as bounded briefing text.

    The ``blackboard_briefing()`` pattern: a compact, hard-capped
    rendering a supervisor can read instead of the raw joined record.
    A not-found summary briefs as exactly that — one honest line.
    """
    if not summary or not summary.get("found"):
        return f"Task {summary.get('task_id') if summary else '?'}: no record found."
    parts: list[str] = []
    prompt = (summary.get("prompt") or "").strip()
    header = f"Task {summary['task_id']}: {summary.get('state') or 'state unknown'}"
    if prompt:
        header += f" — {prompt[:80]}"
    parts.append(header)
    delegations = summary.get("delegations") or []
    if delegations:
        rendered = ", ".join(
            f"{d.get('agent') or '?'}"
            + (" (denied)" if d.get("denied") else "")
            + (f" ({str(d.get('reason'))[:60]})" if d.get("reason") else "")
            for d in delegations[:6])
        parts.append(f"Delegations: {rendered}")
    calls = summary.get("tool_calls") or []
    if calls:
        failed = sum(1 for c in calls if c.get("status") == "failed")
        names = ", ".join(str(c.get("tool_name")) for c in calls[:6])
        parts.append(
            f"Tools: {len(calls)} call(s)" + (f", {failed} failed" if failed else "")
            + f": {names}")
    if summary.get("files_touched"):
        parts.append("Files: " + ", ".join(summary["files_touched"][:6]))
    models = summary.get("models") or []
    if models:
        rendered = ", ".join(
            f"{m.get('agent') or '?'}/{m.get('model')} ×{m.get('calls')}"
            for m in models[:4])
        total = (summary.get("costs") or {}).get("total_cost_usd")
        parts.append(
            f"Models: {rendered}" + (f" — ${total:.4f} total" if isinstance(total, (int, float)) else ""))
    errors = summary.get("errors") or []
    if errors:
        parts.append("Errors: " + "; ".join(
            str(e.get("message"))[:80] for e in errors[:3]))
    outcome_summary = (summary.get("outcome") or {}).get("summary")
    if outcome_summary:
        parts.append(f"Outcome: {str(outcome_summary)[:200]}")
    return "\n".join(parts)[:max_chars]
