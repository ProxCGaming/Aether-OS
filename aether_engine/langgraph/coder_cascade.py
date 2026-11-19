"""P2a — the coder verifier cascade (cheap first, verifier decides,
escalate on rejection).

The FrugalGPT pattern applied where Aether is best built for it:
a coder delegation served by a :class:`CoderBackend` starts **one
tier down**, a deterministic verifier judges the *completed*
attempt, and only a rejection pays for the stronger model. Before
this slice, escalation reacted only to crashes (provider errors,
schema failures inside the native loop); a syntactically valid,
wrong result was accepted. The cascade generalises escalation
from *failure* to *rejection* — between backend invocations, on
the completed result, exactly where E5-01 permits a fresh
decision (it never touches fallback inside a call).

## The ladder

Attempts run through the shared ``CoderBackend`` shape only
(``langgraph/backends/coder_backend.py``), cheapest rung first:

1. the tier **below** the decided tier (floor: small — if the
   decided tier is already small, there is no cascade and the
   delegation is served exactly as decided, as before);
2. the **decided** tier — S14's decision for the delegation;
3. the tier **above** decided (ceiling: strong) — only when the
   owner enables the two-step ladder
   (``UserConfig.coder_cascade_two_step``, the Gate 3 switch).

Total attempts never exceed :data:`MAX_ATTEMPTS`, and the graph
recursion limit binds as always. A rejected attempt hands the
next rung an :class:`AttemptContext` — its diff plus the
verifier's rejection reason verbatim — through the interface, so
the retry is briefed on what to fix. When the ladder is exhausted
the last result stands and its rejection is surfaced to the
supervisor as part of the result — never silently swallowed.

## The verifiers (deterministic; no LLM judge, ever)

- **V1 — schema:** the ``CoderTaskResult`` is the backend's
  output schema, and the attempt must satisfy it in substance:
  status ``done``, a non-empty diff, and ``files_touched``
  consistent with that diff. A ``failed`` / ``budget-stopped``
  run, a done run that produced no changes, or a shape-inconsistent
  result is a rejection. (In the native loop this verdict exists
  as E1-10's structured-output validation; the backend's contract
  shape is its analogue.)
- **V2 — tests:** if the attempt itself ran tests, their
  recorded results must be passing — read from the run's own
  transcript (the wrapper's ``transcript.json``), **never
  re-run by the cascade**. The last recorded test run binds: a
  specialist that fixes and re-runs is judged on its final run.
  No recorded test run → V2 does not apply (it neither passes
  nor blocks); the cascade never invents a verifier.

Researcher/planner delegations are out of scope by design:
their outputs are prose, and verifying prose needs a model
judge — a different, weaker design the plan rejects.

## Guardrails (plan + 18 Nov audit notes)

- **Pins and off mode:** a pinned coder is never cascaded, and
  neither is routing ``off``. Enforced by the caller (the coder
  node) before a ladder is even built.
- **Approval cache (audit RA-16):** backend attempts do not
  traverse the live tree's approval pipeline at all — each runs
  in a fresh throwaway worktree and returns a *proposed* diff
  for the existing approval flow, so no scoped grant from a
  rejected attempt can silently cover a retry's writes. The
  note's substance — the retry must know a previous attempt
  ran — is carried by the attempt context, which the backend
  wrapper renders into the retry's task text verbatim
  ("A previous attempt at this task produced the diff below
  and was rejected…").
- **Prewalk (audit RA-21):** cascade rungs are explicit model
  aliases chosen by tier; there is no planning/execution model
  switch in the backend path for a retry to re-fire, so an
  escalated attempt starts on its rung's (execution) model by
  construction.
- **Tool-effect safety:** a rejected attempt may have produced
  a diff, but backend diffs are **never applied** — each attempt
  starts from the tree as it stands at HEAD, and the prior diff
  rides the attempt context as reference only, with the
  wrapper's task text saying exactly that. No tool executes
  twice against the live tree.
- **Recording:** every attempt is recorded by the caller — an
  S14 outcome row (the lower tier earns its demotion/promotion
  data honestly), a ``model_cascade`` NODE_ACTIVITY event per
  attempt (this module's :func:`publish_cascade_activity`), and
  a delegation-log entry; :func:`cascade_summary_line` renders
  the one-line trail ("coder: small → rejected (tests) →
  standard ✓") the Task Ledger summary shows.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aether_engine.langgraph.backends.coder_backend import (
    DEFAULT_CODER_BUDGET,
    STATUS_BUDGET_STOPPED,
    STATUS_DONE,
    AttemptContext,
    CoderBudget,
    CoderTaskRequest,
    CoderTaskResult,
)

logger = logging.getLogger("aether_engine.langgraph.coder_cascade")

#: The plan's per-delegation attempt cap: cheap + decided +
#: one above, and never more — whatever the ladder offers.
MAX_ATTEMPTS = 3

VERIFIER_SCHEMA = "schema"
VERIFIER_TESTS = "tests"

VERDICT_ACCEPTED = "accepted"
VERDICT_REJECTED = "rejected"


# ── The ladder ────────────────────────────────────────────────────


@dataclass(frozen=True)
class CascadeRung:
    """One ladder rung: a backend bound to one tier's model.

    ``run`` is the backend's bound ``run`` method — the cascade
    speaks only the shared ``CoderBackend`` shape, so P4b's
    strong backend slots in as another rung constructor without
    this module changing.
    """

    tier: int
    tier_name: str
    model_alias: str
    provider_name: str  # S14 outcome-row identity for this rung
    model: str
    backend_name: str
    run: Callable[[CoderTaskRequest], CoderTaskResult]


@dataclass
class CascadeOutcome:
    """The cascade's full record for one delegation."""

    result: CoderTaskResult  # the final attempt's result (stands as-is)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    accepted: bool = False
    #: Set when the ladder ran out with the last attempt rejected;
    #: the caller surfaces it to the supervisor with the result.
    final_rejection_reason: str | None = None

    @property
    def summary_line(self) -> str:
        return cascade_summary_line(self.attempts)


def cascade_summary_line(attempts: list[dict[str, Any]]) -> str:
    """The plan's one-line trail, e.g.
    ``coder: small → rejected (tests) → standard ✓``.

    Built from plain attempt records (``tier_name`` / ``verdict``
    / ``verifier`` keys) so the Task Ledger renders the same line
    from its tap records.
    """
    parts: list[str] = []
    for attempt in attempts:
        parts.append(str(attempt.get("tier_name") or attempt.get("tier") or "?"))
        verdict = attempt.get("verdict")
        if verdict == VERDICT_REJECTED:
            verifier = attempt.get("verifier")
            parts.append(f"rejected ({verifier})" if verifier else "rejected")
        elif verdict == VERDICT_ACCEPTED:
            parts[-1] = parts[-1] + " ✓"
    return "coder: " + " → ".join(parts) if parts else "coder:"


# ── V2: test outcomes from the run's own transcript ───────────────

_PYTEST_COUNTS = re.compile(r"(\d+)\s+(failed|passed|errors?)\b")
_JEST_LINE = re.compile(r"Tests:\s+(.+)")
_JEST_COUNT = re.compile(r"(\d+)\s+(failed|passed)\b")
_UNITTEST_FAIL = re.compile(r"FAILED\s*\(failures=(\d+)(?:,\s*errors=(\d+))?\)")
_CARGO_LINE = re.compile(r"test result:\s+(ok|FAILED)\.\s+(\d+) passed;\s+(\d+) failed")
_PYTEST_FAILED_NAME = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)


def _message_text(message: Any) -> str:
    """One transcript message's text, whatever shape it took."""
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(part.get("text") or "")
                if isinstance(part, dict) else str(part)
                for part in content)
        return ""
    return str(getattr(message, "content", "") or "")


def _one_test_run(text: str) -> dict[str, Any] | None:
    """The test-run outcome recorded in one message's text, or
    None when the text records no test run. Deterministic
    summary-line matching only — a message merely *discussing*
    tests carries no summary line and does not count."""
    found: dict[str, Any] | None = None

    def _record(failed: int, passed: int) -> None:
        nonlocal found
        found = {"failed": failed, "passed": passed,
                 "names": _PYTEST_FAILED_NAME.findall(text)[:5]}

    for match in _PYTEST_COUNTS.finditer(text):
        # A pytest summary line ("3 failed, 9 passed in 1.2s") —
        # several count fragments in one message belong to the
        # same run; accumulate them into one record.
        count, kind = int(match.group(1)), match.group(2)
        if found is None:
            found = {"failed": 0, "passed": 0,
                     "names": _PYTEST_FAILED_NAME.findall(text)[:5]}
        if kind == "failed":
            found["failed"] = count
        elif kind == "passed":
            found["passed"] = count
        else:  # errors / error — collection errors fail the run
            found["failed"] += count
    jest = _JEST_LINE.search(text)
    if jest:
        failed = passed = 0
        seen = False
        for count, kind in _JEST_COUNT.findall(jest.group(1)):
            seen = True
            if kind == "failed":
                failed = int(count)
            else:
                passed = int(count)
        if seen:
            _record(failed, passed)
    unittest_fail = _UNITTEST_FAIL.search(text)
    if unittest_fail:
        _record(int(unittest_fail.group(1))
                + int(unittest_fail.group(2) or 0), 0)
    elif re.search(r"^OK\s*$", text, re.MULTILINE) and "Ran " in text:
        _record(0, 0)
    cargo = _CARGO_LINE.search(text)
    if cargo:
        _record(int(cargo.group(3)), int(cargo.group(2)))
    return found


def test_verdict_from_transcript(
    transcript_ref: str,
) -> tuple[str, str | None]:
    """V2's verdict from the attempt's own transcript.

    Returns ``("pass", None)``, ``("fail", reason)`` or
    ``("absent", None)``. The LAST recorded test run binds.
    A missing/unreadable transcript is "absent", never a
    failure — the verifier judges recorded evidence only.
    """
    if not transcript_ref:
        return "absent", None
    try:
        data = json.loads(Path(transcript_ref).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "absent", None
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list):
        return "absent", None
    last_run: dict[str, Any] | None = None
    for message in messages:
        run = _one_test_run(_message_text(message))
        if run is not None:
            last_run = run
    if last_run is None:
        return "absent", None
    if last_run["failed"]:
        names = last_run["names"]
        detail = f": {', '.join(names)}" if names else ""
        return "fail", (
            f"tests the attempt itself ran failed "
            f"({last_run['failed']} failed, {last_run['passed']} passed)"
            f"{detail}")
    return "pass", None


# ── The verdict ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Verdict:
    accepted: bool
    #: The verifier that decided a rejection ("schema" | "tests");
    #: on acceptance, the strongest verifier that ran.
    verifier: str | None
    reason: str  # "" on acceptance


def verify_attempt(result: CoderTaskResult) -> Verdict:
    """Judge one completed backend attempt (V1, then V2).

    V1 — schema: the result must satisfy the backend contract in
    substance. V2 — tests: consulted only for a shape-valid
    attempt (a failed run's transcript proves nothing about the
    work it never finished).
    """
    if result.status != STATUS_DONE:
        label = ("budget-stopped" if result.status == STATUS_BUDGET_STOPPED
                 else "failed")
        return Verdict(False, VERIFIER_SCHEMA,
                       f"attempt {label}: {result.summary}".strip())
    if not result.diff.strip():
        return Verdict(False, VERIFIER_SCHEMA,
                       "attempt completed without producing any changes "
                       "(empty diff) — there is no work product to accept")
    if not result.files_touched:
        return Verdict(False, VERIFIER_SCHEMA,
                       "attempt's diff names no files (files_touched is "
                       "empty) — the result shape is inconsistent")
    tests, reason = test_verdict_from_transcript(result.transcript_ref)
    if tests == "fail":
        return Verdict(False, VERIFIER_TESTS, reason or "tests failed")
    return Verdict(True, VERIFIER_TESTS if tests == "pass" else VERIFIER_SCHEMA, "")


# ── The loop ──────────────────────────────────────────────────────


def run_cascade(
    *,
    rungs: list[CascadeRung],
    task_briefing: str,
    repo_path: str,
    budget: CoderBudget = DEFAULT_CODER_BUDGET,
    two_step: bool = False,
) -> CascadeOutcome:
    """Run the ladder for one delegation (synchronous; async
    callers wrap in ``asyncio.to_thread``, as the coder node does
    for a single backend run).

    ``rungs`` is ordered cheap → decided → above-decided. Without
    ``two_step`` at most two attempts run (the plan's default
    ladder); with it, up to :data:`MAX_ATTEMPTS`. Every attempt
    carries the caller's per-attempt ``budget`` — the backend
    wrapper enforces it per run — and every attempt after the
    first carries the previous attempt's diff + rejection reason
    as its :class:`AttemptContext`.
    """
    if not rungs:
        raise ValueError("run_cascade needs at least one rung")
    ladder = rungs[:MAX_ATTEMPTS] if two_step else rungs[:2]
    outcome = CascadeOutcome(result=None)  # type: ignore[arg-type]
    prior: CoderTaskResult | None = None
    prior_reason = ""
    for index, rung in enumerate(ladder):
        context = None
        if prior is not None:
            context = AttemptContext(
                prior_diff=prior.diff, rejection_reason=prior_reason)
        request = CoderTaskRequest(
            task_briefing=task_briefing, repo_path=repo_path,
            model_alias=rung.model_alias, budget=budget,
            attempt_context=context)
        result = rung.run(request)
        verdict = verify_attempt(result)
        outcome.attempts.append({
            "attempt": index + 1,
            "agent": "coder",
            "backend": result.backend or rung.backend_name,
            "tier": rung.tier,
            "tier_name": rung.tier_name,
            "model": rung.model_alias,
            "provider_name": rung.provider_name,
            "model_name": rung.model,
            "status": result.status,
            "verdict": VERDICT_ACCEPTED if verdict.accepted else VERDICT_REJECTED,
            "verifier": verdict.verifier,
            "reason": verdict.reason,
            "cost_actual": result.cost_actual,
            "tokens_actual": result.tokens_actual,
            "files_touched": list(result.files_touched),
            "transcript_ref": result.transcript_ref,
            "timestamp": time.time(),
        })
        outcome.result = result
        if verdict.accepted:
            outcome.accepted = True
            outcome.final_rejection_reason = None
            return outcome
        logger.info(
            "Coder cascade attempt %d (%s tier, %s) rejected by %s: %s",
            index + 1, rung.tier_name, rung.model_alias,
            verdict.verifier, verdict.reason)
        prior, prior_reason = result, verdict.reason
        outcome.final_rejection_reason = verdict.reason
    return outcome


# ── Recording ─────────────────────────────────────────────────────


def publish_cascade_activity(state: dict[str, Any], attempt: dict[str, Any]) -> None:
    """Publish one attempt as an S12-shaped NODE_ACTIVITY with
    action ``model_cascade`` — same payload shape as the routing
    events, so the executor's bus pump and the Task Ledger tap
    consume it unchanged. Observational: a publish failure never
    affects the run."""
    task_id = state.get("task_id")
    if not task_id:
        return
    from aether_common.contracts import Event, EventType

    from aether_engine.event_bus import event_bus

    payload: dict[str, Any] = {
        "task_id": task_id,
        "session_id": state.get("session_id"),
        "from_node": "coder",
        "to_node": "coder",
        "action": "model_cascade",
        "reason": attempt.get("reason") or "",
        "briefing": "",
        "input_payload": {"agent": "coder"},
        "output_payload": {
            "attempt": attempt.get("attempt"),
            "tier": attempt.get("tier_name"),
            "model": attempt.get("model"),
            "backend": attempt.get("backend"),
            "verifier": attempt.get("verifier"),
            "verdict": attempt.get("verdict"),
            "reason": attempt.get("reason") or "",
            "status": attempt.get("status"),
        },
        "status": "completed",
        "timestamp": int(time.time()),
    }
    try:
        event_bus.publish(task_id, Event(
            type=EventType.NODE_ACTIVITY,
            request_id=str(task_id),
            payload=payload,
        ))
    except Exception:
        logger.debug("Coder cascade activity publish failed", exc_info=True)
