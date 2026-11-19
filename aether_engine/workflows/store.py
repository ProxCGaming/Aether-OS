"""Workflow store (Full Furnish W-9).

Definitions and run records for Aether's workflows, persisted as
one JSON document at ``~/.aether/workflows.json``. The idioms
mirror the calendar store (``aether_engine/calendar/store.py``):
validation raises with a plain-language reason (``TypeError``
for wrong-typed input, ``ValueError`` for bad values),
reads/deletes of a missing id return ``None`` rather than faking
success, audit logging is best-effort and never blocks a write,
and a shared default store is handed out by
:func:`get_workflow_store`.

Design notes:
- The whole document is rewritten on every mutation, atomically
  (write a sibling temp file, then ``os.replace``), under a lock —
  at personal scale (a handful of workflows, a bounded run
  history) a full rewrite is the simplest durability story that
  cannot half-write. Definitions are unbounded; run records are
  capped at ``RUN_HISTORY_CAP`` newest-first so the file cannot
  grow without limit.
- Validation is the store's job, not the caller's: a name is
  required, a workflow needs at least one step, and every step's
  prompt must be non-empty — the engine refuses prompt-less task
  starts (E1-13), so a workflow may not smuggle one in. A step
  with a blank title takes its title from its prompt (first line,
  truncated), so every step is nameable in the UI.
- Run records carry two internal fields beyond the wire shape
  (``created_at`` / ``updated_at`` / ``error``); :func:`public_run`
  projects a record onto exactly the contract's run shape.
"""

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from aether_engine.audit import AuditLogger

logger = logging.getLogger("aether_engine.workflows.store")
_audit_logger = AuditLogger()

DEFAULT_STORE_PATH = Path.home() / ".aether" / "workflows.json"

# Run records kept on disk, newest first. A run record is small
# (titles + statuses); 100 is far beyond any plausible need and
# keeps the document bounded.
RUN_HISTORY_CAP = 100

# A step title derived from its prompt is truncated to this.
STEP_TITLE_MAX_CHARS = 60

RUN_STATUSES = (
    "running", "awaiting_approval", "completed", "failed",
    "stopped", "interrupted",
)
# Statuses a run can be in while it is still live (or was, when
# the process died — see runner.close_interrupted_runs).
ACTIVE_RUN_STATUSES = ("running", "awaiting_approval")


def _audit(event_type: str, details: dict[str, Any]) -> None:
    """Audit best-effort: a logging failure must never kill a write."""
    try:
        _audit_logger.log_event(event_type, details)
    except Exception:
        logger.debug("Workflow audit failed", exc_info=True)


def _default_step_title(prompt: str) -> str:
    """A step's display title from its prompt: the first
    non-empty line, truncated with an ellipsis when cut."""
    for line in prompt.splitlines():
        line = line.strip()
        if line:
            if len(line) > STEP_TITLE_MAX_CHARS:
                return line[: STEP_TITLE_MAX_CHARS - 1].rstrip() + "…"
            return line
    return "Step"


def validate_workflow(data: Any) -> dict[str, Any]:
    """Normalise + validate one workflow definition.

    Returns the canonical ``{"name", "description", "steps"}``
    (no id — identity is the store's concern). Raises TypeError
    for wrong-typed input and ValueError for bad values, always
    with a plain-language reason; nothing is silently dropped or
    coerced beyond trimming and the title default documented
    above.
    """
    if not isinstance(data, dict):
        raise TypeError("a workflow must be an object with a name and steps")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a workflow needs a name")
    description = data.get("description") or ""
    if not isinstance(description, str):
        raise TypeError("a workflow description must be text")
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("a workflow needs at least one step")
    steps: list[dict[str, str]] = []
    for index, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            raise TypeError(f"step {index} must be an object with a prompt")
        prompt = raw.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(
                f"step {index} has an empty prompt — every step needs "
                "a prompt, because a step runs as a task and a task "
                "cannot start without one")
        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            title = _default_step_title(prompt)
        steps.append({"title": title.strip(), "prompt": prompt.strip()})
    return {
        "name": name.strip(),
        "description": description.strip(),
        "steps": steps,
    }


def public_run(run: dict[str, Any]) -> dict[str, Any]:
    """A run record projected onto exactly the contract shape."""
    return {
        "run_id": run["run_id"],
        "workflow_id": run["workflow_id"],
        "session_id": run["session_id"],
        "status": run["status"],
        "current_step": run["current_step"],
        "steps": [
            {
                "title": step["title"],
                "status": step["status"],
                "task_id": step.get("task_id"),
            }
            for step in run["steps"]
        ],
    }


class WorkflowStore:
    """JSON-backed store for workflow definitions + run records.

    One document, rewritten atomically per mutation. Construction
    loads the file: a missing file is an empty store; a corrupt
    file raises — existing data is never silently discarded.
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else DEFAULT_STORE_PATH
        self._lock = threading.Lock()
        self._workflows: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"the workflow store at {self.path} could not be "
                f"read ({exc}); refusing to start from an empty "
                "store and discard it") from exc
        if not isinstance(doc, dict):
            raise TypeError(
                f"the workflow store at {self.path} is not a "
                "workflow document; refusing to discard it")
        workflows = doc.get("workflows") or {}
        runs = doc.get("runs") or {}
        if isinstance(workflows, dict):
            self._workflows = {
                str(k): v for k, v in workflows.items()
                if isinstance(v, dict)}
        if isinstance(runs, dict):
            self._runs = {
                str(k): v for k, v in runs.items()
                if isinstance(v, dict)}

    def _save_locked(self) -> None:
        """Rewrite the whole document atomically. Caller holds
        the lock (or is the constructor)."""
        runs = sorted(
            self._runs.values(),
            key=lambda r: r.get("created_at") or 0, reverse=True)
        doc = {
            "workflows": self._workflows,
            "runs": {r["run_id"]: r for r in runs[:RUN_HISTORY_CAP]},
        }
        self._runs = doc["runs"]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(doc, indent=2, ensure_ascii=False),
            encoding="utf-8")
        tmp.replace(self.path)

    # ── Definitions ───────────────────────────────────────────

    def list_workflows(self) -> list[dict[str, Any]]:
        """All definitions, oldest first (creation order)."""
        with self._lock:
            items = [dict(w) for w in self._workflows.values()]
        items.sort(key=lambda w: (w.get("created_at") or 0, w["name"]))
        return items

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        with self._lock:
            workflow = self._workflows.get(str(workflow_id))
            return dict(workflow) if workflow is not None else None

    def save_workflow(self, data: Any) -> dict[str, Any] | None:
        """Create (no id / unknown-free id absent) or update (id
        present) one definition. Returns the stored workflow, or
        None when an update names an id that does not exist —
        never a silent create-instead. Raises TypeError /
        ValueError on invalid definitions."""
        normalised = validate_workflow(data)
        workflow_id = data.get("id") if isinstance(data, dict) else None
        now = time.time()
        with self._lock:
            if workflow_id:
                existing = self._workflows.get(str(workflow_id))
                if existing is None:
                    return None
                stored = {
                    **normalised,
                    "id": existing["id"],
                    "created_at": existing["created_at"],
                    "updated_at": now,
                }
            else:
                stored = {
                    **normalised,
                    "id": uuid.uuid4().hex,
                    "created_at": now,
                    "updated_at": now,
                }
            self._workflows[stored["id"]] = stored
            self._save_locked()
        _audit("WORKFLOW_SAVED", {
            "workflow_id": stored["id"], "name": stored["name"],
            "steps": len(stored["steps"]),
            "created": not bool(workflow_id),
        })
        return dict(stored)

    def delete_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """Delete one definition; returns the removed workflow,
        or None when there was nothing with that id."""
        with self._lock:
            removed = self._workflows.pop(str(workflow_id), None)
            if removed is None:
                return None
            self._save_locked()
        _audit("WORKFLOW_DELETED", {
            "workflow_id": removed["id"], "name": removed["name"]})
        return dict(removed)

    # ── Run records ───────────────────────────────────────────

    def create_run(self, run: dict[str, Any]) -> dict[str, Any]:
        """Persist a new run record (the runner builds it)."""
        record = dict(run)
        now = time.time()
        record.setdefault("created_at", now)
        record["updated_at"] = now
        with self._lock:
            self._runs[record["run_id"]] = record
            self._save_locked()
        return dict(record)

    def update_run(self, run_id: str, **fields: Any) -> dict[str, Any] | None:
        """Merge fields into a run record; returns the record, or
        None when the run does not exist."""
        with self._lock:
            record = self._runs.get(str(run_id))
            if record is None:
                return None
            record.update(fields)
            record["updated_at"] = time.time()
            self._save_locked()
            return dict(record)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._runs.get(str(run_id))
            return dict(record) if record is not None else None

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Run records, newest first."""
        with self._lock:
            records = [dict(r) for r in self._runs.values()]
        records.sort(
            key=lambda r: r.get("created_at") or 0, reverse=True)
        return records[: max(0, limit)]

    def active_runs(self) -> list[dict[str, Any]]:
        """Run records still in a live status (running /
        awaiting approval) — after a restart these are exactly
        the records the runner must close as interrupted."""
        return [
            r for r in self.list_runs(limit=RUN_HISTORY_CAP)
            if r.get("status") in ACTIVE_RUN_STATUSES]


_default_store: WorkflowStore | None = None


def get_workflow_store(path: Path | None = None) -> WorkflowStore:
    """The shared store over ``~/.aether/workflows.json`` (or
    ``path`` once — an explicit path always builds a fresh
    store)."""
    global _default_store
    if path is not None:
        return WorkflowStore(path)
    if _default_store is None:
        _default_store = WorkflowStore()
    return _default_store
