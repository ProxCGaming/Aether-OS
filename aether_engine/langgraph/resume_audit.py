"""Pre-resume audit for restart-interrupted tasks (D-1, option B).

The owner's D-1 ruling (consolidated spec §4, DECIDED 17 Nov
20:19 IST): "**Resume, with a pre-resume audit** — before resuming,
the engine must check what the task already did and resume from the
checkpoint so nothing overlaps." The audit "reconciles the last
checkpoint against the task ledger's existing sources — checkpoint
delegation_log (tool calls + statuses), audit.log events, and
on-disk artifact/file verification — and classifies the in-flight
step as DONE (evidence of completion → skip it, resume from the
next step), NOT DONE (no evidence → safe to run), or UNCERTAIN
(partial evidence, e.g. a file exists with no completion record, or
an external side effect that cannot be verified → resume stops and
asks the owner with specifics, never auto-reruns past it)".

This module is that audit. It is a pure read: it joins the ledger's
checkpoint slice (``task_ledger.checkpoint_slice_for_task``) and
audit-trail records (``task_ledger.read_audit_records``) and checks
the filesystem for the write targets the checkpoint names. Known
limits the ruling names are respected here: the executor tap is
in-memory and empty after restart (it is consulted only to refuse
a duplicate resume of a task this process is already running),
and audit payloads are redacted to key shape — checkpoint + audit
trail + disk state are the evidence, not the tap.

Verdicts:

- ``refused`` — resume must not start: the task is not recorded
  INTERRUPTED, has no checkpoint to resume from, is already active
  in this process, or is in fact waiting on an approval decision
  (the sweep deliberately leaves those tasks untouched, and a
  resume must never answer or bypass a pending approval).
- ``uncertain`` — the in-flight step's classification is UNCERTAIN;
  resume may proceed only with the owner's explicit confirmation.
- ``ready`` — every recorded step has completion evidence and the
  in-flight step (if any) is NOT DONE: safe to run.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from aether_common.contracts import TaskState
from aether_engine.task_ledger import (
    _WRITE_TOOL_NAMES,
    checkpoint_slice_for_task,
    read_audit_records,
    tap_snapshot,
)

logger = logging.getLogger("aether_engine.langgraph.resume_audit")

#: Tools whose only effect is reading. An in-flight call to one of
#: these has no completion record, but running it again changes
#: nothing — D-1's NOT DONE ("no evidence → safe to run").
_READ_ONLY_TOOLS = frozenset({
    "read_file", "find_files", "search_text", "workspace_search",
    "search_web", "fetch_url", "get_current_time", "list_events",
    "media_recall", "project_tree", "run_checks",
})

#: Tools that write to a store outside both the checkpoint and the
#: workspace filesystem (the calendar store). Whether such a call
#: ran cannot be verified from the audit's evidence — UNCERTAIN.
_EXTERNAL_STORE_TOOLS = frozenset({"add_event", "cancel_event"})

#: Cap on tool args carried into the audit payload (the UI shows
#: them as the "specifics" D-1 asks for; the full args stay in the
#: checkpoint).
_ARGS_JSON_CAP = 600


def _capped_args(args: Any) -> dict[str, Any]:
    """Tool args for the audit payload, size-capped per value."""
    if not isinstance(args, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > _ARGS_JSON_CAP:
            out[key] = value[:_ARGS_JSON_CAP] + "…"
        else:
            out[key] = value
    return out


def _classify_tool_step(
    call: dict[str, Any],
    *,
    pending_tool_result: bool,
) -> tuple[str, str]:
    """Classify one in-flight tool call: (classification, reason).

    The honesty rule: DONE is never returned here — a call with a
    committed result is not in flight (it is a completed step).
    What cannot be distinguished is named UNCERTAIN with the
    specific evidence that is missing, never guessed DONE.
    """
    name = str(call.get("tool_name") or "unknown")
    args = call.get("tool_args") or {}
    if pending_tool_result:
        return "uncertain", (
            f"the checkpoint holds an uncommitted result for the {name} "
            "call — it may already have completed, but no committed "
            "record proves it")
    if name in _WRITE_TOOL_NAMES:
        target = args.get("path") or args.get("file_path")
        if not target:
            return "uncertain", (
                f"the {name} call names no target path, so whether it "
                "wrote anything cannot be verified on disk")
        if Path(str(target)).exists():
            return "uncertain", (
                f"a file exists at {target} with no completion record — "
                "the write may have landed before the interruption "
                "(partial evidence)")
        return "not_done", (
            f"no file exists at {target} — the write left no trace on "
            "disk, so the call did not complete")
    if name == "delete_file":
        target = args.get("path") or args.get("file_path")
        if not target:
            return "uncertain", (
                "the delete_file call names no target path, so its "
                "effect cannot be verified on disk")
        if Path(str(target)).exists():
            return "not_done", (
                f"{target} still exists — the delete did not take effect")
        return "uncertain", (
            f"{target} is already gone with no completion record — the "
            "delete may have run before the interruption (partial "
            "evidence)")
    if name == "execute_shell":
        return "uncertain", (
            "a shell command's effects cannot be verified from the "
            "checkpoint or the disk — it may or may not have run before "
            "the interruption")
    if name in _READ_ONLY_TOOLS:
        return "not_done", (
            f"{name} only reads — it has no completion record, and "
            "running it again changes nothing")
    if name in _EXTERNAL_STORE_TOOLS:
        return "uncertain", (
            f"{name} writes to a store outside the checkpoint and the "
            "workspace, so whether it ran cannot be verified from the "
            "available evidence")
    return "uncertain", (
        f"the audit cannot verify what {name} does, so whether it ran "
        "cannot be determined from the checkpoint or the disk")


def _completed_steps(slice_: dict[str, Any]) -> list[dict[str, Any]]:
    """Steps with completion evidence (D-1's DONE), in record order.

    Delegations come from the checkpoint's delegation_log; tool
    calls from the checkpointed messages — the parent's, plus the
    in-flight subgraph's own completed calls (its pending call is
    the in-flight step, not a completion, and is excluded here).
    """
    steps: list[dict[str, Any]] = []
    for entry in slice_.get("delegations") or []:
        steps.append({
            "kind": "delegation",
            "label": f"Delegated to {entry.get('agent') or 'unknown agent'}",
            "detail": entry.get("reason") or "",
            "status": "denied" if entry.get("denied") else "completed",
        })
    calls: list[dict[str, Any]] = list(slice_.get("tool_calls") or [])
    for entry in slice_.get("in_flight") or []:
        calls.extend(entry.get("tool_calls") or [])
    seen: set[str] = set()
    for call in calls:
        if call.get("status") not in ("completed", "failed"):
            continue
        key = json.dumps(
            [call.get("tool_name"), call.get("tool_args") or {},
             call.get("status")],
            sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        steps.append({
            "kind": "tool_call",
            "label": str(call.get("tool_name") or "unknown tool"),
            "detail": "",
            "status": call["status"],
            "tool_args": _capped_args(call.get("tool_args")),
        })
    return steps


def _in_flight_step(slice_: dict[str, Any]) -> dict[str, Any] | None:
    """The step in flight at interruption, classified per D-1.

    Resolution order: the in-flight subgraph's staged tool call
    (``pending_tool_call`` — the specialist asked, the tool pipeline
    had not recorded a result); else the last tool call with no
    result in the subgraph's or the parent's messages; else the
    next graph node itself (the interruption landed between steps,
    so that step had not started — NOT DONE). None when the
    checkpoint shows nothing pending at all.
    """
    entries = slice_.get("in_flight") or []
    entry = entries[0] if entries else None
    if entry and entry.get("pending_tool_call"):
        staged = entry["pending_tool_call"]
        call = {"tool_name": staged.get("name"),
                "tool_args": staged.get("args") or {}}
        classification, reason = _classify_tool_step(
            call,
            pending_tool_result=bool(
                entry.get("pending_tool_result")
                or slice_.get("pending_tool_result")))
        return {
            "kind": "tool_call",
            "tool_name": call["tool_name"],
            "tool_args": _capped_args(call["tool_args"]),
            "node": entry.get("node"),
            "classification": classification,
            "reason": reason,
        }
    pending: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    if entry:
        pending.extend(
            (c, entry) for c in entry.get("tool_calls") or []
            if c.get("status") == "pending")
    pending.extend(
        (c, None) for c in slice_.get("tool_calls") or []
        if c.get("status") == "pending")
    if pending:
        call, call_entry = pending[-1]
        classification, reason = _classify_tool_step(
            call,
            pending_tool_result=bool(
                (call_entry or {}).get("pending_tool_result")
                or slice_.get("pending_tool_result")))
        return {
            "kind": "tool_call",
            "tool_name": call.get("tool_name"),
            "tool_args": _capped_args(call.get("tool_args")),
            "node": (call_entry or {}).get("node"),
            "classification": classification,
            "reason": reason,
        }
    next_nodes = (entry or {}).get("next_nodes") or slice_.get("next_nodes") or []
    if next_nodes:
        return {
            "kind": "node",
            "tool_name": None,
            "tool_args": {},
            "node": next_nodes[0],
            "classification": "not_done",
            "reason": (
                "the interruption landed between steps — this step "
                "had not started, so nothing overlaps"),
        }
    return None


async def build_resume_audit(
    task_id: str,
    *,
    session_id: str | None = None,
    audit_path: Path | str | None = None,
    checkpoint_db: Path | str | None = None,
) -> dict[str, Any]:
    """Build the pre-resume audit for one task (see module docstring).

    Always returns the audit dict; ``verdict`` says whether a resume
    may start. A task no writer knows audits as ``refused`` with the
    reason named — never as a fabricated clean bill.
    """
    task_id = str(task_id)
    audit_records = await asyncio.to_thread(
        read_audit_records, task_id, log_path=audit_path)
    transitions = audit_records["transitions"]
    last_state = transitions[-1].get("to_state") if transitions else None
    slice_, scan_capped = await checkpoint_slice_for_task(
        task_id, session_id=session_id, checkpoint_db=checkpoint_db)

    audit: dict[str, Any] = {
        "task_id": task_id,
        "session_id": (slice_ or {}).get("session_id") or session_id,
        "prompt": (slice_ or {}).get("prompt"),
        "state": last_state,
        "verdict": "refused",
        "refusal_reason": None,
        "completed_steps": [],
        "in_flight": None,
        "files": [],
        "pending_approvals": (slice_ or {}).get("pending_approvals") or [],
        "coverage": {
            "checkpoint": slice_ is not None,
            "audit_transitions": len(transitions),
            "checkpoint_scan_capped": scan_capped,
            "notes": [],
        },
    }

    def refuse(reason: str) -> dict[str, Any]:
        audit["refusal_reason"] = reason
        return audit

    # Gate 1: the interruption itself must be on record. The sweep
    # writes INTERRUPTED as the task's last transition; anything
    # else (still running when last heard from, already finished,
    # already resumed and settled) is not this surface's to resume.
    if last_state != TaskState.INTERRUPTED.value:
        if last_state is None:
            return refuse(
                "the audit trail records no interruption for this "
                "task — there is nothing to resume")
        return refuse(
            f"the task's last recorded state is {last_state}, not "
            "INTERRUPTED — only a restart-interrupted task can be "
            "resumed here")
    # Gate 2: a task this process is already running (e.g. a resume
    # launched moments ago) must not be resumed twice.
    tap = tap_snapshot(task_id)
    if tap and tap.get("state") not in (None, TaskState.INTERRUPTED.value):
        return refuse(
            f"this task is already active in the running engine "
            f"(state {tap['state']}) — it cannot be resumed twice")
    # Gate 3: without the checkpoint there is no saved position to
    # resume from — D-1 resumes "from the checkpoint", or not at all.
    if slice_ is None:
        return refuse(
            "no checkpointed state was found for this task"
            + (" within the bounded thread scan" if scan_capped else "")
            + " — there is no saved position to resume from")
    # Gate 4: a pending approval means the task is paused, not
    # orphaned (the sweep leaves those untouched by design). The
    # approval must be answered through the approval flow; a resume
    # here must never deliver, clear or bypass it.
    if audit["pending_approvals"]:
        tool = audit["pending_approvals"][0].get("tool_name") or "a tool"
        return refuse(
            f"this task is waiting on an approval decision for {tool} "
            "— answer the approval instead; resuming here would "
            "bypass it")

    audit["completed_steps"] = _completed_steps(slice_)
    audit["in_flight"] = _in_flight_step(slice_)
    audit["files"] = [
        {"path": path, "exists": Path(path).exists()}
        for path in slice_.get("files_touched") or []
    ]
    if audit["in_flight"] is None:
        audit["coverage"]["notes"].append(
            "the checkpoint shows every recorded step complete — "
            "resuming settles the task's record without re-running "
            "any step")
    audit["verdict"] = (
        "uncertain"
        if audit["in_flight"]
        and audit["in_flight"]["classification"] == "uncertain"
        else "ready")
    return audit
