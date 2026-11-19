"""The shared ``CoderBackend`` contract (P4a; P4b conforms to it).

One interface, every coder backend: the native specialist loop is
the implicit third implementation (it predates the contract and is
not wrapped in it), mini-SWE-agent is the cheap tier (P4a), and
OpenHands will be the strong tier (P4b) — P2a's cascade chooses
between backends purely through this shape, so the shapes here are
the plan's, verbatim, and do not get improvised per backend:

- **Input:** ``{ task_briefing, repo_path, model_alias, budget
  (tokens + USD + wall-clock), attempt_context? }`` —
  attempt_context carries the prior attempt's diff + verifier
  rejection reason when P2a escalates into this backend.
- **Output:** ``{ status (done / failed / budget-stopped), diff,
  summary, files_touched, cost_actual, transcript_ref }``.

Rules for every backend (the plan's, enforced by the wrappers):

- runs only inside the delegated workspace; every file write is
  visible in the returned diff (no silent side effects outside it);
- the wrapper — never the backend — enforces budget and timeout;
- cancellation actually kills the child process.

``run()`` is synchronous: a backend run is a blocking subprocess
wait, and async callers (the coder node) invoke it via
``asyncio.to_thread`` so the graph's event loop never stalls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_BUDGET_STOPPED = "budget-stopped"


@dataclass(frozen=True)
class CoderBudget:
    """The three budget axes the wrapper enforces. ``None`` on an
    axis means that axis is not enforced by the wrapper (the other
    axes still bind); the shared default below is what the coder
    node passes until P2a supplies per-attempt budgets."""

    tokens: int | None = None
    usd: float | None = None
    wall_clock_seconds: float | None = None


#: The node's default delegation budget. The USD figure mirrors
#: mini-SWE-agent's own default cost limit (3.0); the wall clock is
#: generous for a cheap tier because its steps are shell commands
#: with their own per-command timeout underneath.
DEFAULT_CODER_BUDGET = CoderBudget(
    tokens=200_000, usd=3.0, wall_clock_seconds=900.0)


@dataclass(frozen=True)
class AttemptContext:
    """A prior attempt's remains, handed to an escalated retry."""

    prior_diff: str = ""
    rejection_reason: str = ""


@dataclass(frozen=True)
class CoderTaskRequest:
    task_briefing: str
    repo_path: str
    #: A LiteLLM model string (providers.litellm_provider
    #: .resolve_litellm_model produces these from Aether model ids).
    model_alias: str
    budget: CoderBudget = field(default_factory=lambda: DEFAULT_CODER_BUDGET)
    attempt_context: AttemptContext | None = None


@dataclass(frozen=True)
class CoderTaskResult:
    status: str  # STATUS_DONE | STATUS_FAILED | STATUS_BUDGET_STOPPED
    diff: str
    summary: str
    files_touched: list[str]
    #: USD as priced by LiteLLM's cost calculator; 0.0 when the model
    #: is unpriced (local/custom routes) — tokens_actual still counts.
    cost_actual: float
    #: Path of the run's complete transcript JSON on local disk.
    transcript_ref: str
    #: Which backend produced this ("miniswe"); stamped by the
    #: wrapper so delegation-log entries can carry it verbatim.
    backend: str = ""
    #: Total tokens across the run's model calls (observability
    #: beyond the plan's output shape; the budget's token axis is
    #: judged against it).
    tokens_actual: int = 0


@runtime_checkable
class CoderBackend(Protocol):
    """One coder backend. Implementations own a subprocess (or an
    equivalent isolation boundary) and the full wrapper rule set in
    this module's docstring."""

    name: str

    def run(self, request: CoderTaskRequest) -> CoderTaskResult:
        """Execute one delegated coding task to completion (or to a
        budget/timeout/cancel stop) and return its full record."""
        ...

    def cancel(self) -> None:
        """Kill the in-flight run's child process, if any. Safe to
        call when idle; ``run()`` then returns a failed result whose
        summary says the run was cancelled."""
        ...
