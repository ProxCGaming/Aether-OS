"""Learned routing (programme P3a) — dataset, empirical scorer, shadow mode.

RouteLLM's pattern on Aether's own data: replace S14's keyword/length
complexity guess with a small scorer trained on Aether's recorded
delegation outcomes. This module is Phases 1 and 2 of the P3a plan.
**Phase 3 (activation) is deliberately NOT implemented**: Gate 5 is
the owner's decision on shadow-mode evidence, no config value makes
this scorer serve a run, and ``learned_routing`` accepts only
``"off"`` (the default) and ``"shadow"`` — anything else, including
``"active"``, loads as ``"off"``.

What exists here:

* **The dataset builder** (:func:`build_dataset`) — one labelled row
  per delegation, joined from the three stores the plan names: the
  task's ``delegation_log`` history (the supervisor's decision
  entries carry the agent and the task text), the S3 Task Ledger's
  ``model_routing`` records for the task (which model actually
  served, at which tier, and whether it escalated), and S14's
  outcome rows from ``model_routing.db`` (success/failure per model
  round, joined by agent + time window, the ledger's own join
  rule). Verifier verdicts (P2a's labels, the highest-quality ones)
  are read when a task record carries them and are absent — never
  fabricated — until P2a exists.
* **The training pipeline** (:func:`train_scorer`,
  :func:`run_training_pipeline`) — builds per-(agent, feature
  bucket) empirical success tables over the tier that actually
  served, the plan's "simplest scorer that can win", and runs a
  deterministic held-out bake-off against the incumbent heuristic.
  **The honesty gate is real**: fewer than
  :data:`MIN_TRAINING_ROWS` labelled delegations and training
  refuses, returning a report that says how many rows exist and
  what is missing. A scorer trained on 60 rows is astrology; P0/P1
  running for a few weeks is what makes this plan real. As of this
  build the recorded history is nowhere near the gate — the
  pipeline is proven end-to-end on synthetic fixtures in the tests,
  and real training unlocks when the export reaches the gate with
  a reasonable tier spread.
* **The scorer** (:class:`LearnedScorer`) — given a delegation's
  signals, it answers the one question Phase 3 would ask it: which
  tier does a task like this need? Its answer always carries the
  plain-language evidence sentence generated from the table behind
  the score ("tasks like this succeeded on standard tier 9 of the
  last 10 times") — a scorer that cannot produce that sentence is
  not eligible for activation, however good its numbers.
* **Shadow mode** (:func:`run_shadow`) — on every auto-routed
  delegation, with ``learned_routing = "shadow"`` and a trained
  artefact present, the scorer computes what :func:`route` WOULD
  choose with the learned need and publishes it as NODE_ACTIVITY
  with ``source: "learned-shadow"`` (its choice alongside the
  heuristic's). **The heuristic's choice is always the one that
  serves.** The shadow path returns nothing to its caller, is
  published only after the served choice's own activity record
  (so the ledger's model annotation can never mistake the shadow
  pick for the model that ran), and every failure inside it is
  swallowed — fail-open, like every S14 input. With no artefact
  on disk, shadow mode is inert: nothing is computed or logged.

Retraining is a manual, versioned act: a dataset snapshot goes in,
a JSON artefact (format 1) comes out, stored wherever config
points (``learned_router_artefact``, default
``~/.aether/learned_router.json``) and swapped by config. There is
no online self-modifying loop.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from aether_common.contracts import Event, EventType
from aether_engine.event_bus import event_bus
from aether_engine.langgraph.model_routing import (
    LONG_TASK_CHARS,
    SHORT_TASK_CHARS,
    TIER_NAMES,
    Candidate,
    History,
    RoutingChoice,
    TaskSignals,
    _complexity_need,
    route,
    tier_for_model,
)
from aether_engine.routing.signals import text_signals

logger = logging.getLogger("aether_engine.langgraph.learned_router")

#: The plan's honesty gate: an export holding fewer labelled
#: delegations than this cannot train a scorer worth shadowing.
MIN_TRAINING_ROWS = 500

#: A (bucket, tier) cell needs at least this many delegations
#: before its record may ground a scorer opinion — below it the
#: scorer has no opinion on that tier, never a guess.
MIN_BUCKET_SAMPLES = 5

#: The empirical success rate a tier's record must clear for the
#: scorer to call it sufficient for tasks in the bucket. Verifier
#: rows count double in the rate (they are in the raw counts and
#: again in the v_* counts): a test suite's opinion outweighs a
#: bare "the round completed".
SUCCESS_BAR = 0.8

#: Step count at or above which a task counts as multi-step in a
#: feature bucket (mirrors _complexity_need's own threshold).
MULTI_STEP_MIN = 3

#: Artefact format written by save_artefact / read by load_scorer.
ARTEFACT_FORMAT = 1

_TIER_BY_NAME = {name: tier for tier, name in TIER_NAMES.items()}

_DEFAULT_ARTEFACT_PATH = Path.home() / ".aether" / "learned_router.json"


# ── Feature buckets ───────────────────────────────────────────────


def length_band(char_length: int) -> str:
    """The briefing-length band for a character count.

    Uses S14's own bounds (``SHORT_TASK_CHARS`` / ``LONG_TASK_CHARS``)
    so the dataset's bands mean the same thing the heuristic's
    thresholds mean.
    """
    if char_length < SHORT_TASK_CHARS:
        return "short"
    if char_length > LONG_TASK_CHARS:
        return "long"
    return "standard"


def feature_bucket(
    agent: str, *, wants_code: bool, wants_reasoning: bool,
    step_count: int, char_length: int,
) -> str:
    """The serialised bucket key for one delegation's features.

    One function for the training rows and the live signals, so a
    bucket named in the artefact is exactly the bucket a runtime
    delegation hashes to.
    """
    return "|".join((
        agent,
        "r1" if wants_reasoning else "r0",
        "c1" if wants_code else "c0",
        "multi" if step_count >= MULTI_STEP_MIN else "single",
        length_band(char_length),
    ))


def _bucket_for_text(agent: str, task_text: str) -> str:
    sig = text_signals(task_text or "")
    return feature_bucket(
        agent, wants_code=sig.wants_code,
        wants_reasoning=sig.wants_reasoning,
        step_count=sig.step_count, char_length=sig.char_length)


# ── The dataset ───────────────────────────────────────────────────


@dataclass(frozen=True)
class DelegationRow:
    """One delegation in the plan's labelled training format.

    Features come from the delegation itself; the label side names
    what actually served and how it ended. ``None`` on a label
    field means *not recorded* — an unlabelled row is counted in
    the export report and excluded from training, never filled in
    with a guess.
    """

    agent: str
    task_text: str
    wants_code: bool
    wants_reasoning: bool
    step_count: int
    char_length: int
    length_band: str
    has_images: bool | None
    explicit_tier: int | None  # the supervisor's tier label (P1a), when recorded
    provider: str | None
    model: str | None
    served_tier: int | None
    success: bool | None
    verifier_verdict: str | None  # "pass" | "fail" (P2a), when recorded
    cost_usd: float | None
    latency_ms: float | None
    escalated: bool
    timestamp: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "task_text": self.task_text,
            "wants_code": self.wants_code,
            "wants_reasoning": self.wants_reasoning,
            "step_count": self.step_count,
            "char_length": self.char_length,
            "length_band": self.length_band,
            "has_images": self.has_images,
            "explicit_tier": self.explicit_tier,
            "provider": self.provider,
            "model": self.model,
            "served_tier": self.served_tier,
            "success": self.success,
            "verifier_verdict": self.verifier_verdict,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "escalated": self.escalated,
            "timestamp": self.timestamp,
        }


@dataclass
class DatasetExport:
    """The built dataset plus the counts that judge it."""

    rows: list[DelegationRow] = field(default_factory=list)
    denied_excluded: int = 0

    def labelled_rows(self) -> list[DelegationRow]:
        """Rows training may use: an outcome AND a served tier."""
        return [r for r in self.rows
                if r.success is not None and r.served_tier is not None]

    def report(self) -> dict[str, Any]:
        """The export's honest summary (the plan's Phase-1 gate input).

        States the label mix (verifier rows vs bare outcome rows)
        and per-model coverage, because a scorer that only ever
        sees the incumbent is a finding, not a success.
        """
        labelled = self.labelled_rows()
        return {
            "delegations": len(self.rows),
            "denied_excluded": self.denied_excluded,
            "labelled": len(labelled),
            "unlabelled": len(self.rows) - len(labelled),
            "tier_spread": {
                TIER_NAMES.get(t, str(t)): n
                for t, n in sorted(Counter(
                    r.served_tier for r in labelled).items())
            },
            "label_mix": {
                "verifier": sum(
                    1 for r in labelled if r.verifier_verdict in ("pass", "fail")),
                "bare_outcome": sum(
                    1 for r in labelled if r.verifier_verdict not in ("pass", "fail")),
            },
            "coverage": {
                f"{p}:{m}": n for (p, m), n in sorted(Counter(
                    (r.provider, r.model) for r in labelled).items())
            },
        }


def _tier_value(value: Any) -> int | None:
    """A tier as recorded (int, or a TIER_NAMES name) → int | None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value in TIER_NAMES else None
    if isinstance(value, str):
        return _TIER_BY_NAME.get(value.strip().lower())
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_dataset(
    task_records: Iterable[dict[str, Any]],
    outcome_rows: Iterable[dict[str, Any]] = (),
) -> DatasetExport:
    """Build the labelled dataset from delegation history.

    ``task_records`` is an iterable of per-task dicts in the shapes
    the engine already keeps:

    * ``delegation_log`` — the task's delegation log entries. The
      supervisor's decision entries (``decision.next`` naming a
      specialist) are the delegations; END/supervisor decisions,
      denials (counted, then excluded — a denied delegation never
      ran, so it has no outcome to learn from) and decision-less
      entries (S13 records, P2c's offered/called records) are not.
    * ``model_routing`` — the task's S3 ledger routing records
      (agent, provider, model, tier, routing_source, escalated_from,
      timestamp): which model actually served each delegation.
    * ``has_images`` (optional) — whether the task's turn carried
      images; ``verifier_verdicts`` (optional) — a mapping from a
      delegation entry's timestamp to ``"pass"`` / ``"fail"`` (the
      P2a label slot; absent until P2a records verdicts).

    ``outcome_rows`` are S14 outcome-store rows (provider_name,
    model_name, agent_name, outcome, timestamp — see
    ``ModelOutcomeStore.rows``). A delegation's outcome is the LAST
    outcome row for its serving (provider, model, agent) inside
    the delegation's window (its own timestamp up to the next
    delegation's): the final round is how the delegation ended.
    A delegation no outcome row joins stays unlabelled.
    """
    outcomes = [r for r in outcome_rows if isinstance(r, dict)]
    export = DatasetExport()
    for record in task_records or []:
        if not isinstance(record, dict):
            continue
        export.rows.extend(_rows_for_task(record, outcomes, export))
    return export


def _rows_for_task(
    record: dict[str, Any],
    outcomes: list[dict[str, Any]],
    export: DatasetExport,
) -> list[DelegationRow]:
    log = record.get("delegation_log") or []
    decisions: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry in log:
        if not isinstance(entry, dict):
            continue
        decision = entry.get("decision")
        if not isinstance(decision, dict):
            continue
        target = str(decision.get("next") or "").strip().lower()
        if not target or target in ("end", "supervisor"):
            continue
        if decision.get("denied"):
            export.denied_excluded += 1
            continue
        decisions.append((entry, decision))
    routing_records = sorted(
        (r for r in (record.get("model_routing") or []) if isinstance(r, dict)),
        key=lambda r: _as_float(r.get("timestamp")) or 0.0)
    verdicts = record.get("verifier_verdicts") or {}
    task_images = record.get("has_images")
    rows: list[DelegationRow] = []
    for idx, (entry, decision) in enumerate(decisions):
        agent = str(decision.get("next") or "")
        ts = _as_float(entry.get("timestamp"))
        window_end = (
            _as_float(decisions[idx + 1][0].get("timestamp"))
            if idx + 1 < len(decisions) else None)

        def _in_window(stamp: Any) -> bool:
            stamp_f = _as_float(stamp)
            if stamp_f is None:
                return ts is None
            if ts is not None and stamp_f < ts:
                return False
            return window_end is None or stamp_f < window_end

        in_window = [
            r for r in routing_records
            if str(r.get("agent") or "") == agent and _in_window(r.get("timestamp"))]
        served = next(
            (r for r in in_window
             if (r.get("routing_source") or "auto") != "escalation"
             and not r.get("escalated_from")),
            in_window[0] if in_window else None)
        escalated = any(
            (r.get("routing_source") == "escalation" or r.get("escalated_from"))
            for r in in_window)
        provider = str(served.get("provider")) if served and served.get("provider") else None
        model = str(served.get("model")) if served and served.get("model") else None
        served_tier = _tier_value(served.get("tier")) if served else None

        agent_outcomes = [
            r for r in outcomes
            if str(r.get("agent_name") or "") == agent
            and _in_window(r.get("timestamp"))]
        if provider and model:
            exact = [r for r in agent_outcomes
                     if str(r.get("provider_name") or "") == provider
                     and str(r.get("model_name") or "") == model]
            if exact:
                agent_outcomes = exact
        success: bool | None = None
        if agent_outcomes:
            last = max(
                agent_outcomes,
                key=lambda r: _as_float(r.get("timestamp")) or 0.0)
            if last.get("outcome") in ("success", "failure"):
                success = last.get("outcome") == "success"
            if provider is None and last.get("provider_name"):
                provider = str(last.get("provider_name"))
            if model is None and last.get("model_name"):
                model = str(last.get("model_name"))
        if served_tier is None and provider and model:
            served_tier = tier_for_model(provider, model, None)

        text = " ".join(
            part for part in (decision.get("reason", ""), decision.get("briefing", ""))
            if isinstance(part, str) and part).strip()
        sig = text_signals(text)
        verdict = verdicts.get(ts) if ts is not None else None
        if verdict not in ("pass", "fail"):
            verdict = None
        rows.append(DelegationRow(
            agent=agent,
            task_text=text,
            wants_code=sig.wants_code,
            wants_reasoning=sig.wants_reasoning,
            step_count=sig.step_count,
            char_length=sig.char_length,
            length_band=length_band(sig.char_length),
            has_images=task_images if isinstance(task_images, bool) else None,
            explicit_tier=_tier_value(decision.get("tier")),
            provider=provider,
            model=model,
            served_tier=served_tier,
            success=success,
            verifier_verdict=verdict,
            cost_usd=_as_float(served.get("cost_usd")) if served else None,
            latency_ms=_as_float(served.get("latency_ms")) if served else None,
            escalated=escalated,
            timestamp=ts,
        ))
    return rows


# ── The scorer ────────────────────────────────────────────────────

# One (bucket, tier) cell: raw outcome counts, with the verifier
# subset counted again in v_* (the double weight in the rate).
_Cell = dict[str, int]


def _empty_cell() -> _Cell:
    return {"ok": 0, "bad": 0, "v_ok": 0, "v_bad": 0}


def _cell_rate(cell: _Cell) -> float:
    """Success rate with verifier rows weighted double."""
    total = cell["ok"] + cell["bad"] + cell["v_ok"] + cell["v_bad"]
    if total <= 0:
        return 0.0
    return (cell["ok"] + cell["v_ok"]) / total


class LearnedScorer:
    """Per-(agent, feature bucket) empirical success tables.

    The plan's simplest scorer: for each bucket, the recorded
    success of each tier that actually served tasks like this.
    :meth:`need_tier` answers with the cheapest tier whose record
    clears :data:`SUCCESS_BAR` (or, when none clears it, the tier
    with the best record) — and with the evidence sentence built
    from that tier's raw counts. A bucket with no cell at or above
    :data:`MIN_BUCKET_SAMPLES` yields NO opinion (``None``): the
    scorer never guesses, and every consumer fails open to the
    heuristic.
    """

    def __init__(
        self,
        tables: dict[str, dict[int, _Cell]] | None = None,
        *,
        trained_on_rows: int = 0,
        min_bucket_samples: int = MIN_BUCKET_SAMPLES,
        success_bar: float = SUCCESS_BAR,
    ):
        self._tables = tables or {}
        self.trained_on_rows = trained_on_rows
        self.min_bucket_samples = min_bucket_samples
        self.success_bar = success_bar

    # ── Artefact (the versioned, config-swapped form) ────────────

    def to_artefact(self) -> dict[str, Any]:
        return {
            "format": ARTEFACT_FORMAT,
            "kind": "p3a-empirical-tier-tables",
            "trained_on_rows": self.trained_on_rows,
            "min_bucket_samples": self.min_bucket_samples,
            "success_bar": self.success_bar,
            "tables": {
                bucket: {str(tier): dict(cell) for tier, cell in tiers.items()}
                for bucket, tiers in self._tables.items()
            },
        }

    @classmethod
    def from_artefact(cls, artefact: Any) -> "LearnedScorer":
        """Rebuild a scorer from its JSON artefact.

        Raises ValueError on anything that is not a format-1
        empirical-tables artefact — callers (the artefact loader)
        treat that as "no scorer", never as a run failure.
        """
        if not isinstance(artefact, dict):
            raise ValueError("artefact is not a mapping")
        if artefact.get("format") != ARTEFACT_FORMAT:
            raise ValueError("unsupported artefact format")
        if artefact.get("kind") != "p3a-empirical-tier-tables":
            raise ValueError("unsupported artefact kind")
        raw_tables = artefact.get("tables")
        if not isinstance(raw_tables, dict):
            raise ValueError("artefact carries no tables")
        tables: dict[str, dict[int, _Cell]] = {}
        for bucket, tiers in raw_tables.items():
            if not isinstance(tiers, dict):
                continue
            parsed: dict[int, _Cell] = {}
            for tier_key, cell in tiers.items():
                # JSON object keys are strings: "1" in an artefact
                # is the tier int, never a tier name.
                try:
                    tier: int | None = int(tier_key)
                except (TypeError, ValueError):
                    tier = _tier_value(tier_key)
                if tier is None or tier not in TIER_NAMES or not isinstance(cell, dict):
                    continue
                parsed[tier] = {
                    key: int(cell.get(key) or 0)
                    for key in ("ok", "bad", "v_ok", "v_bad")
                }
            if parsed:
                tables[str(bucket)] = parsed
        return cls(
            tables,
            trained_on_rows=int(artefact.get("trained_on_rows") or 0),
            min_bucket_samples=int(
                artefact.get("min_bucket_samples") or MIN_BUCKET_SAMPLES),
            success_bar=float(artefact.get("success_bar") or SUCCESS_BAR),
        )

    # ── The one question ─────────────────────────────────────────

    def need_tier(
        self, signals: TaskSignals, incumbent_tier: int,
    ) -> tuple[int, str] | None:
        """The tier tasks like this one need, and the evidence why.

        Returns ``(tier, basis_sentence)``, or ``None`` when this
        bucket has no cell with enough recorded delegations to
        ground an opinion. ``incumbent_tier`` is accepted for
        signature parity with the heuristic it would one day
        replace (``_complexity_need``); the empirical answer does
        not depend on it — the record is the record.
        """
        bucket = _bucket_for_text(signals.agent, signals.task_text or "")
        cells = self._tables.get(bucket)
        if not cells:
            return None
        usable = {
            tier: cell for tier, cell in cells.items()
            if cell["ok"] + cell["bad"] >= self.min_bucket_samples
        }
        if not usable:
            return None
        clearing = [t for t in sorted(usable) if _cell_rate(usable[t]) >= self.success_bar]
        if clearing:
            tier = clearing[0]
        else:
            tier = max(sorted(usable), key=lambda t: _cell_rate(usable[t]))
        cell = usable[tier]
        total = cell["ok"] + cell["bad"]
        basis = (
            f"tasks like this succeeded on {TIER_NAMES[tier]} tier "
            f"{cell['ok']} of the last {total} times")
        return tier, basis


# ── Training pipeline (offline; manual, versioned) ────────────────


@dataclass
class TrainingReport:
    """The training run's honest outcome.

    ``status`` is ``"trained"`` (an artefact is attached) or
    ``"insufficient_data"`` (the honesty gate refused; ``reason``
    states the counts). No status in between exists: a report
    never calls a scorer trained that the gate refused.
    """

    status: str
    reason: str
    labelled_rows: int
    min_rows: int
    train_rows: int = 0
    holdout_rows: int = 0
    label_mix: dict[str, int] = field(default_factory=dict)
    coverage: dict[str, int] = field(default_factory=dict)
    bakeoff: dict[str, Any] | None = None
    artefact: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "labelled_rows": self.labelled_rows,
            "min_rows": self.min_rows,
            "train_rows": self.train_rows,
            "holdout_rows": self.holdout_rows,
            "label_mix": self.label_mix,
            "coverage": self.coverage,
            "bakeoff": self.bakeoff,
            "artefact": self.artefact,
        }


def _row_label(row: DelegationRow) -> tuple[bool, bool] | None:
    """(success, is_verifier) for a row, or None when unlabelled.

    A verifier verdict, when recorded, IS the label — it outranks
    the bare round outcome it judges.
    """
    if row.verifier_verdict in ("pass", "fail"):
        return row.verifier_verdict == "pass", True
    if row.success is not None:
        return row.success, False
    return None


def _build_tables(rows: Iterable[DelegationRow]) -> dict[str, dict[int, _Cell]]:
    tables: dict[str, dict[int, _Cell]] = {}
    for row in rows:
        label = _row_label(row)
        if label is None or row.served_tier is None:
            continue
        ok, is_verifier = label
        bucket = feature_bucket(
            row.agent, wants_code=row.wants_code,
            wants_reasoning=row.wants_reasoning,
            step_count=row.step_count, char_length=row.char_length)
        cell = tables.setdefault(bucket, {}).setdefault(
            row.served_tier, _empty_cell())
        cell["ok" if ok else "bad"] += 1
        if is_verifier:
            cell["v_ok" if ok else "v_bad"] += 1
    return tables


def _success_rate(rows: list[DelegationRow]) -> float | None:
    labelled = [r for r in rows if _row_label(r) is not None]
    if not labelled:
        return None
    return sum(1 for r in labelled if _row_label(r)[0]) / len(labelled)


def _bakeoff(scorer: LearnedScorer, holdout: list[DelegationRow]) -> dict[str, Any]:
    """Held-out comparison: learned need vs the heuristic's need.

    Observational by construction and labelled as such: a held-out
    row's outcome only speaks about the tier that ACTUALLY served
    it, so a need (learned or heuristic) is scored only on the
    rows where it matches the served tier. Counterfactual outcomes
    — what the other tier would have done — are unknowable from
    this data and are never estimated here.
    """
    opinions = 0
    agreements = 0
    scorer_rows: list[DelegationRow] = []
    heuristic_rows: list[DelegationRow] = []
    for row in holdout:
        signals = TaskSignals(
            agent=row.agent, task_text=row.task_text,
            has_images=bool(row.has_images))
        heuristic_tier, _why = _complexity_need(signals, 1)
        if heuristic_tier == row.served_tier:
            heuristic_rows.append(row)
        learned = scorer.need_tier(signals, 1)
        if learned is not None:
            opinions += 1
            if learned[0] == heuristic_tier:
                agreements += 1
            if learned[0] == row.served_tier:
                scorer_rows.append(row)
    return {
        "holdout_rows": len(holdout),
        "scorer_opinions": opinions,
        "agreement_rate": (agreements / opinions) if opinions else None,
        "scorer_scorable_rows": len(scorer_rows),
        "scorer_scorable_success_rate": _success_rate(scorer_rows),
        "heuristic_scorable_rows": len(heuristic_rows),
        "heuristic_scorable_success_rate": _success_rate(heuristic_rows),
        "note": (
            "Observational: a held-out row scores only the need "
            "that matches the tier actually served; counterfactual "
            "outcomes are unknowable and are not estimated."),
    }


def train_scorer(
    rows: Iterable[DelegationRow], *, min_rows: int = MIN_TRAINING_ROWS,
) -> TrainingReport:
    """Train the empirical scorer on dataset rows (offline).

    The honesty gate runs first: fewer than ``min_rows`` labelled
    delegations (an outcome AND a served tier) and the report is
    ``insufficient_data`` with the counts — no artefact, no
    bake-off, nothing that could be mistaken for a trained scorer.
    ``min_rows`` defaults to the plan's gate
    (:data:`MIN_TRAINING_ROWS`); a smaller value exists so the
    pipeline can be exercised end-to-end on fixtures, and the
    threshold actually used is recorded in the report and is not
    the production gate unless it equals it.

    The split is deterministic on a fixed snapshot (rows sorted by
    timestamp/agent/model/text; every fifth row is held out), so
    the same input always produces the same report.
    """
    all_rows = list(rows)
    labelled = [r for r in all_rows
                if _row_label(r) is not None and r.served_tier is not None]
    label_mix = {
        "verifier": sum(1 for r in labelled if _row_label(r)[1]),
        "bare_outcome": sum(1 for r in labelled if not _row_label(r)[1]),
    }
    coverage = {
        f"{p}:{m}": n for (p, m), n in sorted(Counter(
            (r.provider, r.model) for r in labelled).items())
    }
    if len(labelled) < min_rows:
        return TrainingReport(
            status="insufficient_data",
            reason=(
                f"{len(labelled)} labelled delegations is below the "
                f"{min_rows}-row honesty gate; a scorer trained on "
                f"this little data would be astrology. The gate "
                f"unlocks when the export reaches {min_rows} "
                f"labelled delegations with a reasonable tier spread "
                f"(P0/P1/P2 outcome data accumulating over weeks)."),
            labelled_rows=len(labelled),
            min_rows=min_rows,
            label_mix=label_mix,
            coverage=coverage,
        )
    ordered = sorted(
        labelled,
        key=lambda r: (r.timestamp or 0.0, r.agent, r.provider or "",
                       r.model or "", r.task_text))
    holdout = [r for i, r in enumerate(ordered) if i % 5 == 4]
    train = [r for i, r in enumerate(ordered) if i % 5 != 4]
    scorer = LearnedScorer(
        _build_tables(train), trained_on_rows=len(train))
    return TrainingReport(
        status="trained",
        reason=(
            f"Trained empirical tier tables on {len(train)} "
            f"delegations (held out {len(holdout)} for the bake-off)."),
        labelled_rows=len(labelled),
        min_rows=min_rows,
        train_rows=len(train),
        holdout_rows=len(holdout),
        label_mix=label_mix,
        coverage=coverage,
        bakeoff=_bakeoff(scorer, holdout),
        artefact=scorer.to_artefact(),
    )


def run_training_pipeline(
    task_records: Iterable[dict[str, Any]],
    outcome_rows: Iterable[dict[str, Any]] = (),
    *,
    min_rows: int = MIN_TRAINING_ROWS,
    artefact_path: Path | str | None = None,
) -> TrainingReport:
    """The offline pipeline end to end: export → gate → train → save.

    The artefact is written ONLY when the report says ``trained`` —
    an insufficient-data run leaves any existing artefact untouched,
    so a refused retrain can never silently replace a good scorer
    with nothing.
    """
    export = build_dataset(task_records, outcome_rows)
    report = train_scorer(export.rows, min_rows=min_rows)
    if report.status == "trained" and artefact_path is not None:
        save_artefact(report.artefact, artefact_path)
    return report


# ── Artefact storage ──────────────────────────────────────────────


def save_artefact(artefact: dict[str, Any] | None, path: Path | str) -> Path:
    """Write a trained artefact as JSON (the versioned swap unit)."""
    if not artefact:
        raise ValueError("no artefact to save (training did not produce one)")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(artefact, indent=2, sort_keys=True))
    return target


def load_scorer(path: Path | str) -> LearnedScorer | None:
    """Load a scorer from an artefact file; None when unusable.

    Missing file, unreadable JSON, wrong format — all read as "no
    scorer" (shadow mode inert), never as an error: the artefact
    is an input, not a gate.
    """
    try:
        data = json.loads(Path(path).read_text())
        return LearnedScorer.from_artefact(data)
    except Exception:
        logger.debug("Learned router artefact unusable: %s", path, exc_info=True)
        return None


def artefact_path(user_config: Any | None) -> Path:
    """The configured artefact path (default ~/.aether/learned_router.json)."""
    configured = getattr(user_config, "learned_router_artefact", "") or ""
    return Path(configured) if configured else _DEFAULT_ARTEFACT_PATH


_scorer_cache: dict[str, Any] | None = None


def get_scorer(user_config: Any | None) -> LearnedScorer | None:
    """The artefact-backed scorer, cached by (path, mtime).

    A swapped artefact (a retrain landing mid-process) is picked
    up on the next delegation whose mtime check sees the change.
    """
    global _scorer_cache
    path = artefact_path(user_config)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _scorer_cache = {"path": str(path), "mtime": None, "scorer": None}
        return None
    if (_scorer_cache and _scorer_cache.get("path") == str(path)
            and _scorer_cache.get("mtime") == mtime):
        return _scorer_cache.get("scorer")
    scorer = load_scorer(path)
    _scorer_cache = {"path": str(path), "mtime": mtime, "scorer": scorer}
    return scorer


def reset_scorer_cache() -> None:
    """Drop the cached scorer (tests; a manual artefact swap)."""
    global _scorer_cache
    _scorer_cache = None


# ── Shadow mode (Phase 2) ─────────────────────────────────────────


def publish_shadow_activity(
    state: dict[str, Any],
    agent: str,
    served_choice: RoutingChoice,
    shadow_choice: RoutingChoice | None,
    basis: str | None,
) -> None:
    """Publish one shadow decision as S12-shaped NODE_ACTIVITY.

    Same payload shape as the served routing activity, with
    ``source: "learned-shadow"`` and the heuristic's choice carried
    alongside — the pair the Phase-2 agreement analysis reads. A
    ``None`` shadow choice is the scorer's recorded no-opinion
    (its bucket has no usable record yet), published so coverage
    is measurable from the log. Observational: a publish failure
    never affects the run.
    """
    task_id = state.get("task_id")
    if not task_id:
        return
    payload: dict[str, Any] = {
        "task_id": task_id,
        "session_id": state.get("session_id"),
        "from_node": agent,
        "to_node": agent,
        "action": "model_routing",
        "reason": basis or "the learned scorer has no record for tasks like this yet",
        "briefing": "",
        "input_payload": {"agent": agent},
        "output_payload": {
            "provider": shadow_choice.provider if shadow_choice else None,
            "model": shadow_choice.model if shadow_choice else None,
            "tier": (TIER_NAMES.get(shadow_choice.tier, str(shadow_choice.tier))
                     if shadow_choice else None),
            "source": "learned-shadow",
            "escalated_from": None,
            "reason": basis,
            "heuristic_provider": served_choice.provider,
            "heuristic_model": served_choice.model,
            "heuristic_tier": TIER_NAMES.get(
                served_choice.tier, str(served_choice.tier)),
            "agree": bool(
                shadow_choice is not None
                and (shadow_choice.provider, shadow_choice.model)
                == (served_choice.provider, served_choice.model)),
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
        logger.debug("Learned shadow activity publish failed", exc_info=True)


def run_shadow(
    *,
    state: dict[str, Any],
    agent: str,
    signals: TaskSignals,
    candidates: list[Candidate],
    history: History,
    served_choice: RoutingChoice,
    user_config: Any | None,
) -> None:
    """Compute and log the learned choice for one delegation.

    SHADOW ONLY, and the contract is absolute: this function
    returns nothing, the choice it computes is published as a log
    record and then discarded, and the heuristic's
    ``served_choice`` — already applied by the caller — is the
    only choice that ever serves. Called by the S14 seam only
    after the served choice's own activity is published, so the
    ledger can never annotate a delegation with the shadow model.
    Inert when no trained artefact exists; every failure inside
    is swallowed (fail-open, like every S14 input).
    """
    try:
        scorer = get_scorer(user_config)
        if scorer is None:
            return
        basis_box: dict[str, str] = {}

        def _need(sig: TaskSignals, incumbent_tier: int) -> tuple[int, str]:
            learned = scorer.need_tier(sig, incumbent_tier)
            if learned is None:
                # No opinion: fall back to the heuristic need so the
                # shadow choice is still a full route() decision —
                # it will agree with the served choice by
                # construction, and the published basis says why.
                tier, why = _complexity_need(sig, incumbent_tier)
                basis_box["basis"] = (
                    "no empirical record for tasks like this yet; "
                    f"shadow follows the heuristic ({why})")
                return tier, why
            basis_box["basis"] = learned[1]
            return learned

        shadow_choice = route(signals, candidates, history, need_fn=_need)
        publish_shadow_activity(
            state, agent, served_choice, shadow_choice,
            basis_box.get("basis"))
    except Exception:
        logger.debug("Learned shadow routing failed", exc_info=True)
