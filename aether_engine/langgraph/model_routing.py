"""Automatic per-agent model routing + escalation (master build S14).

Audit G4/G5: model choice for a specialist was a static pin lookup —
``agent_models`` or the run's provider — hand-duplicated in the
supervisor resolver and the specialist runner, and no failure
anywhere stepped UP to a stronger model (provider fallback is
lateral; the structured-output retry re-asks the SAME model). This
module is the one router that decides what ``"inherit"`` resolves
to, per delegation, when the user turns it on.

Contract (the owner's pins always win):

* ``model_routing = "off"`` (default) — this module is never
  consulted on the run path; resolution and prompts are exactly
  what they were before S14.
* A pin — any ``agent_models`` value other than ``"inherit"`` —
  is a hard user decision. Routing never overrides it, and
  escalation never fires for a pinned agent. Callers resolve pins
  through the pre-S14 code path and only call here for the
  ``"inherit"`` case.
* ``auxiliary_models`` (vision / compression / advisor / title)
  keeps its own resolution; nothing here reads or writes it.
* The supervisor's own model is NOT routed: the adapter resolves
  it once per routing step through ``_resolve_supervisor_provider``
  and the supervisor is the thing doing the delegating — routing
  it per-step could swap the routing model mid-turn. The Settings
  report below says so plainly instead of pretending otherwise.
  Fast chat is likewise untouched (its own path, its own choice).

Signals (honest and cheap — no model call, no invented data):

* the delegation's own text (the supervisor's reason + briefing
  for this specialist, or the run's original prompt when the
  specialist state carries no delegation entry — the S13 child
  case): its length, and code / reasoning markers (the same
  keyword sets ``routing.policy.infer_task_requirements`` uses);
* whether the turn carries images — a HARD filter: a candidate
  without the registry's ``vision`` capability is never chosen
  for an image turn;
* recorded history: per-(model, agent) run outcomes this module
  records at the specialist seam (the ledger never stored
  failures — audit G4 — so S14 records its own, in
  ``~/.aether/model_routing.db``, latency-store house style),
  per-model usage/cost aggregates from the UsageLedger's real
  rows, and measured TTFT percentiles from the latency store.
  There is deliberately NO price table: cost enters only as the
  average cost of calls the ledger actually recorded.

Candidates come only from providers that are configured AND
currently available (the health manager the run-level policy
consults), from the model registry's chat-capable entries, plus
the run's own provider/model as the incumbent — the status quo is
always in the pool, so auto routing can only ever move a
delegation deliberately, and any failure in gathering inputs
degrades to the incumbent (routing must never break a run).
``gemini-2.5-flash`` is excluded by name: it 404s on the owner's
key (registry note E5-03), so it must never become a routed
choice even though the curated registry lists it.

Tiers derive from the registry's own capability vocabulary (see
:func:`tier_from_capabilities`): ``reasoning`` marks the strong
tier, ``fast`` the small tier, anything else standard. For a
model the registry does not know (a freshly discovered custom
model), the registry's own name-based capability inference
supplies the tier basis — documented inference, not a lookup
that pretends to knowledge it does not have.

Escalation: when an auto-routed specialist run fails — a provider
error before any output streamed, or a structured-output
validation failure — the runner retries ONCE on the next tier
up (see nodes/runner.py and structured.py). It never fires when
routing is off, when the agent is pinned, on a policy/approval
denial (those never reach the model seam), or twice in one
specialist invocation. Every decision and escalation is logged,
published as S12-shaped NODE_ACTIVITY, and folded into the S3
Task Ledger, so the summary can show which model ran each
delegation and why.

P3a shadow mode (see langgraph/learned_router.py): with
``learned_routing = "shadow"`` and a trained scorer artefact
present, each auto decision here is additionally shadowed — the
learned scorer computes the choice it WOULD have made and logs
it with ``source: "learned-shadow"``, after the served choice's
own record. The served choice is never the shadow's; there is
no "active" learned mode (activation is Gate 5).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aether_common.contracts import Event, EventType
from aether_engine.event_bus import event_bus
from aether_engine.routing.registry import _infer_capabilities, is_local_provider
from aether_engine.routing.signals import (
    CODE_KEYWORDS,
    text_signals,
)

logger = logging.getLogger("aether_engine.langgraph.model_routing")

# ── Tiers ─────────────────────────────────────────────────────────

TIER_SMALL = 0
TIER_STANDARD = 1
TIER_STRONG = 2
TIER_NAMES = {TIER_SMALL: "small", TIER_STANDARD: "standard", TIER_STRONG: "strong"}

#: Models that must never be routed to, however the tiers fall:
#: gemini-2.5-flash returns 404 on the owner's key (registry E5-03;
#: the curated registry keeps it listed for completeness and pins
#: its priority so discovery can never make it a default — routing
#: applies the same exclusion by name).
KNOWN_UNUSABLE_MODELS = frozenset({"gemini-2.5-flash"})


def _normalize_model_id(model: str) -> str:
    """The bare, comparable form of a model id: last path segment
    (``gemini/gemini-2.5-flash`` -> ``gemini-2.5-flash``), lowered."""
    return model.rsplit("/", 1)[-1].strip().lower()


def pin_targets_unusable_model(
    pin: str | None, *, learned: Any | None = None
) -> bool:
    """True when an ``agent_models`` pin names an unusable model.

    Unusable = on the static ``KNOWN_UNUSABLE_MODELS`` list OR in
    the learned set (models a provider reported as not-found at call
    time, persisted by :func:`learn_unusable_model`).

    Pins bypass routing's exclusion by design (S14: a pinned agent is
    never auto-routed) — which let a stale ``gemini-2.5-flash`` pin
    kill a task with a provider 404 on the owner's machine
    (15 Nov 2026): the pin constructed fine, the call died, and the
    whole run failed after other nodes had already produced output.
    The provider resolvers (specialist runner, supervisor) consult
    this BEFORE constructing a pinned provider and fall back to the
    run's default provider — the model the owner selected in
    Settings — never to a hardcoded substitute.

    Accepts the pin's stored forms: ``provider:model``, a bare model
    name, or a litellm-style ``provider/model`` suffix. ``learned``
    overrides the learned set (tests); None reads the persisted one.
    """
    if not pin or pin == "inherit":
        return False
    model = pin.split(":", 1)[1] if ":" in pin else pin
    model = _normalize_model_id(model)
    if model in KNOWN_UNUSABLE_MODELS:
        return True
    learned_set = learned if learned is not None else _learned_unusable_models()
    return model in learned_set


def note_pin_refused(agent_name: str, pin: str | None) -> None:
    """Mark a refused dead-model pin: warning log + audit event.

    The refusal itself happens in the resolvers; this is its durable
    receipt (the audit trail is what survives the run — a log line
    alone scrolls away). Best-effort: marking never breaks a run.
    """
    logger.warning(
        "Agent model pin refused for %s: %r names an unusable model; "
        "using the run's default provider (the model selected in Settings)",
        agent_name,
        pin,
    )
    try:
        from aether_engine.audit import AuditLogger

        AuditLogger().log_event(
            "AGENT_MODEL_PIN_REFUSED", {"agent": agent_name, "pin": pin}
        )
    except Exception:  # marking must never break a run
        logger.debug("pin-refusal audit write failed", exc_info=True)


# ── Learned unusable models ───────────────────────────────────────
#
# The static list above only knows models someone already added by
# hand. A model that dies later (retired by its provider, renamed,
# key loses access) is learned instead: when the LiteLLM provider's
# call ends in a terminal model-not-found, it records the model
# here, and from then on pin_targets_unusable_model refuses pins to
# it up front — the NEXT run never pays for the discovery.
#
# KNOWN LIMIT (marked 15 Nov 2026, owner's instruction): the FIRST
# failure of a not-yet-known dead model still fails that one call —
# in-graph provider fallback is deliberately blocked once tools
# have executed or output has streamed (E5-01), so there is no safe
# mid-run swap at that point. Learning converts every LATER hit
# into a clean up-front refusal; it does not resurrect the run that
# taught it. Second marked limit: refusal is a runtime act — the
# dead pin still sits in the owner's saved Settings (marked in the
# audit trail on every refusal) until they edit it; nothing here
# rewrites user config.


class LearnedUnusableStore:
    """Models learned as not-found at call time, SQLite.

    House style mirrors ModelOutcomeStore: a bounded SQLite file in
    the data home, short-lived connections, recording that never
    raises into the call it observes.
    """

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._initialized = False

    def _get_conn(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self, conn: sqlite3.Connection) -> None:
        if self._initialized:
            return
        conn.execute("""
            CREATE TABLE IF NOT EXISTS learned_unusable_models (
                model_name TEXT PRIMARY KEY,
                provider_name TEXT,
                learned_at INTEGER NOT NULL,
                source TEXT NOT NULL
            )
        """)
        self._initialized = True

    def record(
        self,
        model_name: str,
        *,
        provider_name: str | None = None,
        source: str = "provider-not-found",
    ) -> None:
        try:
            conn = self._get_conn()
            try:
                self._init_db(conn)
                conn.execute(
                    "INSERT OR IGNORE INTO learned_unusable_models "
                    "(model_name, provider_name, learned_at, source) "
                    "VALUES (?, ?, ?, ?)",
                    (model_name, provider_name, int(time.time()), source),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:  # learning must never raise
            logger.debug("learned-unusable write failed", exc_info=True)

    def all_models(self) -> set[str]:
        try:
            conn = self._get_conn()
            try:
                self._init_db(conn)
                rows = conn.execute(
                    "SELECT model_name FROM learned_unusable_models"
                ).fetchall()
                return {row["model_name"] for row in rows}
            finally:
                conn.close()
        except Exception:  # reads degrade to empty
            logger.debug("learned-unusable read failed", exc_info=True)
            return set()


_learned_store: LearnedUnusableStore | None = None
_learned_cache: set[str] | None = None


def _learned_unusable_models() -> set[str]:
    """The persisted learned set, loaded once and kept current by
    :func:`learn_unusable_model` in this process."""
    global _learned_cache, _learned_store
    if _learned_cache is None:
        if _learned_store is None:
            _learned_store = LearnedUnusableStore()
        _learned_cache = _learned_store.all_models()
    return _learned_cache


def learn_unusable_model(
    model_id: str | None,
    *,
    provider_name: str | None = None,
    store: LearnedUnusableStore | None = None,
) -> str | None:
    """Learn that *model_id* answered model-not-found at call time.

    Returns the normalized id recorded (or the static-list id, which
    needs no learning). Never raises: a failed write just means the
    next run learns it again.
    """
    if not model_id:
        return None
    normalized = _normalize_model_id(str(model_id))
    if not normalized or normalized in KNOWN_UNUSABLE_MODELS:
        return normalized or None
    try:
        if store is not None:
            store.record(normalized, provider_name=provider_name)
        else:
            cache = _learned_unusable_models()  # ensures the singleton
            assert _learned_store is not None
            _learned_store.record(normalized, provider_name=provider_name)
            cache.add(normalized)
    except Exception:  # learning must never raise
        logger.debug("learn_unusable_model failed", exc_info=True)
    return normalized

#: An outcome record needs at least this many samples for an agent
#: before a losing record demotes the model in ranking — one bad
#: run is noise, a pattern is signal.
DEMOTE_MIN_SAMPLES = 3

#: Text-length signal bounds for the complexity estimate (chars of
#: delegation reason + briefing, or of the original prompt).
LONG_TASK_CHARS = 1500
SHORT_TASK_CHARS = 140

#: Outcome rows retained in the routing store (newest win).
OUTCOME_ROW_CAP = 5000

_DEFAULT_DB_PATH = Path.home() / ".aether" / "model_routing.db"


def learned_routing_mode(user_config: Any | None) -> str:
    """The configured learned-router mode (P3a): "shadow" only
    when exactly that.

    "off" (the default) and "shadow" are the only modes that
    exist. There is deliberately no "active": P3a activation is
    Gate 5, an owner decision on shadow evidence, and no config
    value — including the literal string "active" — turns the
    learned scorer live. Anything unrecognised reads as "off".
    """
    mode = getattr(user_config, "learned_routing", "off")
    return "shadow" if mode == "shadow" else "off"


def _maybe_shadow_route(
    state: dict[str, Any],
    agent: str,
    signals: TaskSignals,
    candidates: list[Candidate],
    history: History,
    served_choice: RoutingChoice,
    user_config: Any | None,
) -> None:
    """P3a Phase 2 seam: let the learned scorer log its would-be
    choice for this delegation. SHADOW ONLY.

    Called only from :func:`resolve_for_specialist`, only after
    the served choice is fixed AND its own activity published (so
    the S3 ledger's join can never annotate the delegation with
    the shadow model instead of the one that ran). It cannot
    alter the served choice — it receives it as a finished fact
    and returns nothing — and every failure inside it is
    swallowed here and in the shadow runner itself: a broken
    scorer must be invisible to the run it observes.
    """
    try:
        if learned_routing_mode(user_config) != "shadow":
            return
        from aether_engine.langgraph import learned_router

        learned_router.run_shadow(
            state=state, agent=agent, signals=signals,
            candidates=candidates, history=history,
            served_choice=served_choice, user_config=user_config)
    except Exception:
        logger.debug("Learned shadow seam failed", exc_info=True)


def routing_mode(user_config: Any | None) -> str:
    """The configured routing mode: "auto" only when exactly that."""
    mode = getattr(user_config, "model_routing", "off")
    return "auto" if mode == "auto" else "off"


def tier_from_capabilities(capabilities: Any) -> int:
    """Tier for a capability list (the registry's vocabulary).

    ``reasoning`` marks the strong tier (the curated registry's
    pro / sonnet / reasoner class); ``fast`` without ``reasoning``
    marks the small tier (flash / mini / haiku class); everything
    else chat-capable is standard. A model claiming both is
    strong — when a model says it can reason, believe it for
    tiering purposes.
    """
    caps = set(capabilities or [])
    if "reasoning" in caps:
        return TIER_STRONG
    if "fast" in caps:
        return TIER_SMALL
    return TIER_STANDARD


def tier_for_model(provider: str, model: str, registry: Any | None) -> int:
    """Tier for a (provider, model): the registry entry when known,
    else the registry's name-based capability inference."""
    entry = None
    if registry is not None:
        try:
            entry = registry.get_model(model, provider=provider)
        except Exception:  # noqa: BLE001 — tiering must never break a run.
            entry = None
    if entry is not None:
        return tier_from_capabilities(entry.capabilities)
    return tier_from_capabilities(_infer_capabilities(model))


# ── Signals ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class TaskSignals:
    """What one delegation looks like to the router."""

    agent: str
    task_text: str = ""
    has_images: bool = False


def _state_has_images(state: dict[str, Any]) -> bool:
    """True when any state message carries an image part.

    Mirrors the executor's ``_messages_have_images`` (the M17
    resume check): checkpointed multimodal messages hold
    ``{"type": "image_url", ...}`` parts in a list content.
    """
    messages = state.get("messages") or []
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


def signals_for_state(agent: str, state: dict[str, Any]) -> TaskSignals:
    """Signals for the specialist run about to start in ``state``.

    The delegation's own text is the supervisor's reason +
    briefing for THIS agent (the newest matching delegation_log
    entry — the entry the S4 post-model hook just wrote). A run
    with no such entry — an S13 child, whose state starts fresh —
    falls back to the run's original prompt (for a child, the
    sub-task text), which is the honest description of its work.
    """
    text = ""
    for entry in reversed(state.get("delegation_log") or []):
        if not isinstance(entry, dict):
            continue
        decision = entry.get("decision") or {}
        if not isinstance(decision, dict):
            continue
        if str(decision.get("next") or "").lower() != agent:
            continue
        if decision.get("denied"):
            continue
        text = " ".join(
            part for part in (decision.get("reason", ""), decision.get("briefing", ""))
            if isinstance(part, str) and part).strip()
        break
    if not text:
        text = str(state.get("original_prompt") or "")
    return TaskSignals(agent=agent, task_text=text,
                       has_images=_state_has_images(state))


def _complexity_need(signals: TaskSignals, incumbent_tier: int) -> tuple[int, str]:
    """The tier this task warrants, and the plain-language why.

    The default is the incumbent's own tier: a standard task
    should run on the model the user already runs it on, and only
    a real signal moves the choice. Reasoning markers ask for the
    strong tier; a long brief or an explicitly multi-step one
    asks one tier up from standard; a short, marker-free sub-task
    is small-tier work.
    """
    text = signals.task_text or ""
    sig = text_signals(text)
    if sig.wants_reasoning:
        return TIER_STRONG, "the task calls for deep reasoning"
    if sig.char_length > LONG_TASK_CHARS or sig.step_count >= 3:
        return min(TIER_STRONG, max(incumbent_tier, TIER_STANDARD) + 1), \
            "the brief is long or explicitly multi-step"
    lower = text.lower()
    if sig.char_length < SHORT_TASK_CHARS and not any(kw in lower for kw in CODE_KEYWORDS):
        return TIER_SMALL, "the sub-task is short and simple"
    return incumbent_tier, "a standard task"


def _wanted_capability(signals: TaskSignals, need_tier: int) -> str | None:
    """A capability the task specifically wants, if any."""
    lower = (signals.task_text or "").lower()
    if signals.agent == "coder" or any(kw in lower for kw in CODE_KEYWORDS):
        return "code"
    if need_tier == TIER_STRONG:
        return "reasoning"
    return None


# ── Candidates ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    provider: str
    model: str
    tier: int
    capabilities: tuple[str, ...] = ()
    is_incumbent: bool = False


def collect_candidates(
    *,
    registry: Any | None,
    configured_providers: Any,
    health_manager: Any | None,
    incumbent_provider: str | None,
    incumbent_model: str | None,
    local_only: bool = False,
) -> list[Candidate]:
    """The routable pool for one decision.

    The incumbent (the run's own provider/model — what "inherit"
    resolves to today) is always in the pool: it is the status
    quo, and health/config churn must not make auto routing worse
    than off. Every other candidate must be a chat-capable
    registry model of a provider that is BOTH configured and
    currently available, must not be a known-unusable model, and
    — under a local-only run constraint — must be local.
    """
    pool: dict[tuple[str, str], Candidate] = {}

    def _add(provider: str, model: str, *, incumbent: bool = False) -> None:
        key = (provider, model)
        if key in pool:
            if incumbent:
                pool[key] = Candidate(
                    provider, model, pool[key].tier,
                    pool[key].capabilities, is_incumbent=True)
            return
        caps: tuple[str, ...] = ()
        if registry is not None:
            try:
                entry = registry.get_model(model, provider=provider)
            except Exception:  # noqa: BLE001 — a registry hiccup drops data, not the run.
                entry = None
            if entry is not None:
                caps = tuple(entry.capabilities or ())
        if not caps:
            caps = tuple(_infer_capabilities(model))
        pool[key] = Candidate(
            provider, model, tier_from_capabilities(caps), caps,
            is_incumbent=incumbent)

    if incumbent_provider and incumbent_model:
        _add(str(incumbent_provider), str(incumbent_model), incumbent=True)

    if registry is not None:
        for provider in configured_providers or []:
            provider = str(provider)
            if local_only and not is_local_provider(provider):
                continue
            if health_manager is not None:
                try:
                    if not health_manager.is_provider_available(provider):
                        continue
                except Exception:
                    logger.debug(
                        "Provider health check failed for %s; skipping "
                        "it for routing", provider, exc_info=True)
                    continue
            try:
                entries = registry.get_models_for_provider(provider)
            except Exception:
                logger.debug(
                    "Registry lookup failed for provider %s; skipping "
                    "it for routing", provider, exc_info=True)
                continue
            for entry in entries or []:
                caps = entry.capabilities or []
                if "chat" not in caps:
                    continue
                if entry.id in KNOWN_UNUSABLE_MODELS:
                    continue
                _add(provider, entry.id)
    return list(pool.values())


# ── Outcome history (S14's own recording) ─────────────────────────


class ModelOutcomeStore:
    """Per-(provider, model, agent) run outcomes, SQLite.

    The usage ledger records completed calls (cost, tokens) but
    never failures, and the health manager tracks providers, not
    models (audit G4) — so the router's reliability input is
    recorded here, at the specialist seam, one row per specialist
    model round: ``success`` when the round completed (including
    a structured output that validated), ``failure`` when the
    model call raised or its structured output failed validation
    past correction. House style mirrors the latency store: a
    bounded SQLite file in the data home, short-lived connections,
    recording that never raises into the run it observes.
    """

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._initialized = False
        self._writes = 0

    def _get_conn(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self, conn: sqlite3.Connection) -> None:
        if self._initialized:
            return
        conn.execute("""
            CREATE TABLE IF NOT EXISTS model_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                provider_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                outcome TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outcomes_model "
            "ON model_outcomes(provider_name, model_name, agent_name)")
        conn.commit()
        self._initialized = True

    def record(self, provider: str, model: str, agent: str, outcome: str) -> None:
        """Insert one outcome row. Never raises (observational)."""
        if outcome not in ("success", "failure"):
            return
        try:
            with self._get_conn() as conn:
                self._init_db(conn)
                conn.execute(
                    "INSERT INTO model_outcomes "
                    "(timestamp, provider_name, model_name, agent_name, outcome) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (int(time.time()), provider, model, agent, outcome))
                self._writes += 1
                if self._writes % 64 == 0:
                    # Bounded like every house store: prune to the
                    # newest OUTCOME_ROW_CAP rows, occasionally —
                    # not on the hot path of every write.
                    conn.execute(
                        "DELETE FROM model_outcomes WHERE id NOT IN "
                        "(SELECT id FROM model_outcomes "
                        " ORDER BY id DESC LIMIT ?)",
                        (OUTCOME_ROW_CAP,))
                conn.commit()
        except Exception:
            logger.debug("Model outcome recording failed", exc_info=True)

    def rows(self) -> list[dict[str, Any]]:
        """Every recorded outcome row, oldest first (P3a export).

        :meth:`stats` aggregates; the learned router's dataset
        builder needs the rows themselves, to join each outcome
        onto its delegation by agent + time. Read-only, and
        degrades to ``[]`` like every read in this store.
        """
        try:
            with self._get_conn() as conn:
                self._init_db(conn)
                found = conn.execute(
                    "SELECT timestamp, provider_name, model_name, "
                    "agent_name, outcome FROM model_outcomes "
                    "ORDER BY id").fetchall()
                return [dict(r) for r in found]
        except Exception:
            logger.debug("Model outcome rows unavailable", exc_info=True)
            return []

    def stats(self) -> dict[tuple[str, str, str], tuple[int, int]]:
        """{(provider, model, agent): (successes, failures)}."""
        try:
            with self._get_conn() as conn:
                self._init_db(conn)
                rows = conn.execute(
                    "SELECT provider_name, model_name, agent_name, "
                    "SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END) AS ok, "
                    "SUM(CASE WHEN outcome = 'failure' THEN 1 ELSE 0 END) AS bad "
                    "FROM model_outcomes "
                    "GROUP BY provider_name, model_name, agent_name")
                return {
                    (r["provider_name"], r["model_name"], r["agent_name"]):
                        (int(r["ok"] or 0), int(r["bad"] or 0))
                    for r in rows
                }
        except Exception:
            logger.debug("Model outcome stats unavailable", exc_info=True)
            return {}


_GLOBAL_OUTCOME_STORE: ModelOutcomeStore | None = None


def get_outcome_store() -> ModelOutcomeStore:
    global _GLOBAL_OUTCOME_STORE
    if _GLOBAL_OUTCOME_STORE is None:
        _GLOBAL_OUTCOME_STORE = ModelOutcomeStore()
    return _GLOBAL_OUTCOME_STORE


def record_model_outcome(
    provider: Any, model: Any, agent: str, outcome: str, *,
    store: ModelOutcomeStore | None = None,
) -> None:
    """Record one specialist round's outcome (never raises).

    A provider without a name/model identity (a test double) has
    no recordable history — the row is skipped, not fabricated.
    """
    if not provider or not model:
        return
    try:
        (store or get_outcome_store()).record(
            str(provider), str(model), agent, outcome)
    except Exception:
        logger.debug("Model outcome recording failed", exc_info=True)


# ── History assembly ──────────────────────────────────────────────


@dataclass
class History:
    """Everything recorded that a decision may consult."""

    # (provider, model, agent) -> (successes, failures)
    outcomes: dict[tuple[str, str, str], tuple[int, int]] = field(default_factory=dict)
    # (provider, model) -> {"calls", "cost_usd", "total_tokens"}
    usage: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    # (provider, model) -> measured TTFT p50 (ms)
    ttft_p50_ms: dict[tuple[str, str], float] = field(default_factory=dict)


def collect_history(
    candidates: list[Candidate],
    *,
    ledger: Any | None = None,
    outcome_store: ModelOutcomeStore | None = None,
    latency_store: Any | None = None,
) -> History:
    """Assemble the recorded history for a pool (fail-open).

    Every source is optional and independently guarded: a missing
    or broken source yields an empty slice, and the decision
    falls back to the static tier table — the documented cold
    start, not an error.
    """
    history = History()
    try:
        history.outcomes = (outcome_store or get_outcome_store()).stats()
    except Exception:  # noqa: BLE001 — history is an input, never a gate.
        history.outcomes = {}
    if ledger is None:
        try:
            from aether_engine.cost.ledger import get_usage_ledger

            ledger = get_usage_ledger()
        except Exception:  # noqa: BLE001 — same doctrine.
            ledger = None
    if ledger is not None:
        try:
            history.usage = ledger.model_usage_totals()
        except Exception:  # noqa: BLE001 — same doctrine.
            history.usage = {}
    if latency_store is None:
        try:
            from aether_engine.routing.latency import get_latency_store

            latency_store = get_latency_store()
        except Exception:  # noqa: BLE001 — same doctrine.
            latency_store = None
    if latency_store is not None and candidates:
        keys = [(c.provider, c.model) for c in candidates]
        try:
            batch = getattr(latency_store, "ttft_p50_ms_many", None)
            values = batch(keys) if callable(batch) else {}
            if not values:
                values = {}
                for key in keys:
                    value = latency_store.ttft_p50_ms(*key)
                    if value is not None:
                        values[key] = value
            history.ttft_p50_ms = {
                key: float(value) for key, value in (values or {}).items()
                if value is not None
            }
        except Exception:  # noqa: BLE001 — same doctrine.
            history.ttft_p50_ms = {}
    return history


# ── The decision ──────────────────────────────────────────────────


@dataclass(frozen=True)
class RoutingChoice:
    provider: str
    model: str
    tier: int
    source: str  # "auto" | "escalation"
    reason: str
    escalated_from: str | None = None  # "provider:model" stepped up from


def _demoted(candidate: Candidate, agent: str, history: History) -> bool:
    """A losing recorded record for THIS agent (pattern, not noise)."""
    successes, failures = history.outcomes.get(
        (candidate.provider, candidate.model, agent), (0, 0))
    return (successes + failures) >= DEMOTE_MIN_SAMPLES and failures > successes


def _rank_key(candidate: Candidate, agent: str, history: History) -> tuple:
    """Deterministic within-tier ranking.

    Order of evidence: a demoted model (losing record for this
    agent) sorts last; then the average recorded cost per call
    (cheaper first, unrecorded = infinity — no invented prices);
    then measured TTFT p50 (faster first, unmeasured last); then
    the incumbent (the status quo wins an honest tie); then names,
    so the result never depends on pool ordering.
    """
    usage = history.usage.get((candidate.provider, candidate.model))
    if usage and usage.get("calls"):
        cost_per_call = float(usage["cost_usd"]) / int(usage["calls"])
    else:
        cost_per_call = float("inf")
    ttft = history.ttft_p50_ms.get(
        (candidate.provider, candidate.model), float("inf"))
    return (
        1 if _demoted(candidate, agent, history) else 0,
        cost_per_call,
        ttft,
        0 if candidate.is_incumbent else 1,
        candidate.provider,
        candidate.model,
    )


def _tier_group(
    pool: list[Candidate], target_tier: int, want: str | None,
) -> tuple[list[Candidate], int]:
    """The candidate group at the best tier for ``target_tier``.

    Nearest available tier wins; on a distance tie the stronger
    tier is preferred when the target IS strong (a reasoning task
    should not silently land on a weaker model when an equal-
    distance stronger one exists) and the cheaper tier otherwise.
    Within the group, holders of the wanted capability are
    preferred when any exist.
    """
    tiers = sorted({c.tier for c in pool})
    if target_tier in tiers:
        chosen_tier = target_tier
    else:
        def _distance(tier: int) -> tuple[int, int]:
            secondary = -tier if target_tier == TIER_STRONG else tier
            return (abs(tier - target_tier), secondary)

        chosen_tier = min(tiers, key=_distance)
    group = [c for c in pool if c.tier == chosen_tier]
    if want:
        holders = [c for c in group if want in c.capabilities]
        if holders:
            group = holders
    return group, chosen_tier


def route(
    signals: TaskSignals,
    candidates: list[Candidate],
    history: History,
    *,
    need_fn: Any | None = None,
) -> RoutingChoice | None:
    """Choose the model for one delegation (pure, deterministic).

    Returns None when the pool is empty (the caller keeps the run
    provider — routing unavailable is not an error). The hard
    filters are applied here, before any preference: an image
    turn only ever sees vision-capable candidates, and when that
    filter would empty the pool the incumbent alone survives it
    if it is the only candidate (the run's own vision handling
    already vouched for it) — otherwise None.

    ``need_fn`` is the P3a seam: a callable with
    ``_complexity_need``'s signature supplying the tier need (and
    its plain-language why) in the heuristic's place. Everything
    else — candidate collection, hard filters, demotion, ranking,
    the reason string — is unchanged whichever need is supplied.
    The live path never passes it today: P3a Phase 2 (shadow mode)
    passes the learned scorer's need to compute the choice that
    WOULD have served, and Phase 3 activation (Gate 5, an owner
    decision) is the only thing that would ever pass it live.
    """
    pool = list(candidates)
    if not pool:
        return None
    if signals.has_images:
        vision = [c for c in pool if "vision" in c.capabilities]
        if vision:
            pool = vision
        elif len(pool) > 1:
            return None
    incumbent = next((c for c in pool if c.is_incumbent), None)
    incumbent_tier = incumbent.tier if incumbent is not None else TIER_STANDARD
    if need_fn is None:
        need_tier, why = _complexity_need(signals, incumbent_tier)
    else:
        need_tier, why = need_fn(signals, incumbent_tier)
    want = _wanted_capability(signals, need_tier)
    group, tier = _tier_group(pool, need_tier, want)
    if not group:
        return None
    chosen = min(group, key=lambda c: _rank_key(c, signals.agent, history))

    reason_parts = [why.capitalize()]
    if incumbent is not None and (chosen.provider, chosen.model) == (
            incumbent.provider, incumbent.model):
        reason_parts.append(
            f"your default model {chosen.provider}:{chosen.model} "
            f"({TIER_NAMES[tier]} tier) fits")
    else:
        reason_parts.append(
            f"auto routing picked {chosen.provider}:{chosen.model} "
            f"({TIER_NAMES[tier]} tier)")
        demoted_others = [
            c for c in group
            if (c.provider, c.model) != (chosen.provider, chosen.model)
            and _demoted(c, signals.agent, history)
        ]
        if demoted_others:
            worst = demoted_others[0]
            successes, failures = history.outcomes.get(
                (worst.provider, worst.model, signals.agent), (0, 0))
            reason_parts.append(
                f"{worst.provider}:{worst.model} has failed {failures} of "
                f"{successes + failures} recent {signals.agent} runs")
    return RoutingChoice(
        provider=chosen.provider, model=chosen.model, tier=tier,
        source="auto", reason=" — ".join(reason_parts) + ".")


def escalate(
    signals: TaskSignals,
    candidates: list[Candidate],
    history: History,
    *,
    current_provider: str,
    current_model: str,
    current_tier: int,
) -> RoutingChoice | None:
    """The next tier up from the model that just failed, or None.

    Same hard filters and ranking as :func:`route`, restricted to
    tiers strictly above the failed model's, the failed model
    itself excluded. None when already at the top of the pool —
    escalation is one step, not a promise of a stronger model
    that does not exist.
    """
    pool = [
        c for c in candidates
        if c.tier > current_tier
        and (c.provider, c.model) != (current_provider, current_model)
    ]
    if signals.has_images:
        pool = [c for c in pool if "vision" in c.capabilities]
    if not pool:
        return None
    want = _wanted_capability(signals, TIER_STRONG)
    group, tier = _tier_group(pool, min(c.tier for c in pool), want)
    if not group:
        return None
    chosen = min(group, key=lambda c: _rank_key(c, signals.agent, history))
    return RoutingChoice(
        provider=chosen.provider, model=chosen.model, tier=tier,
        source="escalation",
        reason=(
            f"Escalated from {current_provider}:{current_model} after a "
            f"failed run — next stronger tier: {chosen.provider}:"
            f"{chosen.model} ({TIER_NAMES[tier]} tier)."),
        escalated_from=f"{current_provider}:{current_model}")


# ── Application seam (used by the specialist runner) ──────────────


@dataclass
class RoutingContext:
    """One specialist invocation's routing state.

    Carried by the runner from resolution to the failure seams:
    the applied choice plus the inputs an escalation re-decision
    needs. ``choice`` is the model actually serving (it changes
    when an escalation fires; the runner updates it).
    """

    signals: TaskSignals
    candidates: list[Candidate]
    history: History
    choice: RoutingChoice


def publish_routing_activity(
    state: dict[str, Any], agent: str, choice: RoutingChoice,
) -> None:
    """Publish one routing decision as S12-shaped NODE_ACTIVITY.

    Same payload shape as the delegation / sub-agent events, so
    the executor's bus pump and the S3 ledger tap consume it
    unchanged; the tap records it under its own ``model_routing``
    list (see task_ledger). Observational: a publish failure
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
        "action": "model_escalation" if choice.source == "escalation"
                  else "model_routing",
        "reason": choice.reason,
        "briefing": "",
        "input_payload": {"agent": agent},
        "output_payload": {
            "provider": choice.provider,
            "model": choice.model,
            "tier": TIER_NAMES.get(choice.tier, str(choice.tier)),
            "source": choice.source,
            "escalated_from": choice.escalated_from,
            "reason": choice.reason,
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
        logger.debug("Model routing activity publish failed", exc_info=True)


def resolve_for_specialist(
    *,
    agent: str,
    state: dict[str, Any],
    config: dict[str, Any],
    run_provider: Any,
) -> tuple[Any, RoutingContext | None]:
    """Resolve one specialist invocation's provider under "auto".

    The runner calls this ONLY for the unpinned "inherit" case
    with routing in "auto" mode — pins and "off" never reach
    here. Returns ``(provider, context)``: the provider to serve
    the invocation (the run provider when the router keeps the
    incumbent or cannot improve on it) and the context the
    escalation seams need. Any failure in gathering inputs or
    constructing the chosen provider degrades to
    ``(run_provider, None)`` — routing unavailable for this
    invocation, exactly the pin path's failure doctrine.
    """
    configurable = config.get("configurable", {}) or {}
    user_config = configurable.get("user_config")
    incumbent_provider = getattr(run_provider, "provider_name", None)
    incumbent_model = getattr(run_provider, "model", None)
    if not incumbent_provider or not incumbent_model:
        return run_provider, None
    try:
        from aether_engine.state import engine_state

        candidates = collect_candidates(
            registry=engine_state.model_registry,
            configured_providers=engine_state.configured_providers,
            health_manager=engine_state.health_manager,
            incumbent_provider=str(incumbent_provider),
            incumbent_model=str(incumbent_model),
            local_only=bool(configurable.get("local_only")),
        )
        signals = signals_for_state(agent, state)
        history = collect_history(candidates)
        choice = route(signals, candidates, history)
        if choice is None:
            return run_provider, None
        # S13 retry step-up: a sub-agent's second attempt runs one
        # tier above its own fresh decision (the spawn seam sets
        # the flag only after a genuine child failure).
        if configurable.get("model_routing_escalate"):
            stepped = escalate(
                signals, candidates, history,
                current_provider=choice.provider,
                current_model=choice.model,
                current_tier=choice.tier)
            if stepped is not None:
                choice = stepped
        context = RoutingContext(
            signals=signals, candidates=candidates,
            history=history, choice=choice)
        if (choice.provider, choice.model) == (
                str(incumbent_provider), str(incumbent_model)):
            publish_routing_activity(state, agent, choice)
            _maybe_shadow_route(
                state, agent, signals, candidates, history, choice,
                user_config)
            return run_provider, context
        secret_store = configurable.get("secret_store")
        if secret_store is None:
            logger.warning(
                "Model routing picked %s:%s for %s but no secret "
                "store is available to construct it; keeping the "
                "run provider", choice.provider, choice.model, agent)
            return run_provider, None
        from aether_engine.orchestration.core import _create_provider_instance

        provider = _create_provider_instance(
            choice.provider, choice.model, secret_store,
            local_only=bool(configurable.get("local_only")),
            require_vision=bool(configurable.get("require_vision")),
            user_config=user_config,
        )
        publish_routing_activity(state, agent, choice)
        logger.info(
            "Model routing for %s: %s:%s (%s tier) — %s",
            agent, choice.provider, choice.model,
            TIER_NAMES.get(choice.tier, choice.tier), choice.reason)
        _maybe_shadow_route(
            state, agent, signals, candidates, history, choice,
            user_config)
        return provider, context
    except Exception:
        logger.warning(
            "Model routing unavailable for %s; keeping the run "
            "provider", agent, exc_info=True)
        return run_provider, None


def construct_choice_provider(
    choice: RoutingChoice, config: dict[str, Any],
) -> Any | None:
    """Construct the provider for an escalation choice, or None.

    Same factory and run constraints as every in-run construction
    (the pin path's seam). None when construction is impossible —
    the caller then lets the original failure stand.
    """
    configurable = config.get("configurable", {}) or {}
    secret_store = configurable.get("secret_store")
    if secret_store is None:
        return None
    try:
        from aether_engine.orchestration.core import _create_provider_instance

        return _create_provider_instance(
            choice.provider, choice.model, secret_store,
            local_only=bool(configurable.get("local_only")),
            require_vision=bool(configurable.get("require_vision")),
            user_config=configurable.get("user_config"),
        )
    except Exception:
        logger.warning(
            "Escalation target %s:%s unusable; the original "
            "failure stands", choice.provider, choice.model,
            exc_info=True)
        return None


# ── Settings report (AGENT_ROUTING_REQUEST) ───────────────────────


def effective_models_report(user_config: Any | None) -> dict[str, Any]:
    """Per-agent effective models for Settings → Agent Orchestration.

    For every agent: what would serve it right now, from where,
    and why in one plain sentence. Pins are reported as pins;
    unpinned specialists under "auto" get the router's own
    decision on NEUTRAL signals (no task text, no images) —
    labelled as the default decision, because each real
    delegation is decided per task and can differ. The run's
    default model (the user's configured default provider/model)
    is the incumbent for those decisions.
    """
    from aether_engine.state import engine_state

    config = user_config if user_config is not None else engine_state.user_config
    mode = routing_mode(config)
    agent_models = getattr(config, "agent_models", None) or {}
    default_provider = getattr(config, "default_provider", None) or "google_gemini"
    default_model = getattr(config, "default_model", None) or ""

    candidates: list[Candidate] = []
    history = History()
    if mode == "auto":
        try:
            candidates = collect_candidates(
                registry=engine_state.model_registry,
                configured_providers=engine_state.configured_providers,
                health_manager=engine_state.health_manager,
                incumbent_provider=str(default_provider),
                incumbent_model=str(default_model),
            )
            history = collect_history(candidates)
        except Exception:
            logger.debug("Routing report inputs unavailable", exc_info=True)
            candidates = []

    agents: list[dict[str, Any]] = []
    for agent in ("supervisor", "researcher", "planner", "coder"):
        pin = agent_models.get(agent)
        if pin and pin != "inherit":
            p_name, _, m_name = str(pin).partition(":")
            agents.append({
                "agent": agent,
                "provider": p_name or None,
                "model": m_name or str(pin),
                "source": "pinned",
                "tier": None,
                "reason": (
                    "Pinned by you in Agents settings — routing "
                    "never overrides a pin."),
            })
            continue
        if agent == "supervisor":
            agents.append({
                "agent": agent,
                "provider": str(default_provider),
                "model": str(default_model),
                "source": "run_default",
                "tier": None,
                "reason": (
                    "The supervisor runs on the run's model — it "
                    "does the delegating, so auto routing chooses "
                    "models for the specialists instead."),
            })
            continue
        if mode != "auto":
            agents.append({
                "agent": agent,
                "provider": str(default_provider),
                "model": str(default_model),
                "source": "run_default",
                "tier": None,
                "reason": (
                    "Model routing is off — unpinned specialists "
                    "use the run's model."),
            })
            continue
        choice = None
        if candidates:
            choice = route(
                TaskSignals(agent=agent), candidates, history)
        if choice is None:
            agents.append({
                "agent": agent,
                "provider": str(default_provider),
                "model": str(default_model),
                "source": "run_default",
                "tier": None,
                "reason": (
                    "No eligible alternative is configured — the "
                    "run's model serves."),
            })
        else:
            agents.append({
                "agent": agent,
                "provider": choice.provider,
                "model": choice.model,
                "source": "auto",
                "tier": TIER_NAMES.get(choice.tier),
                "reason": choice.reason,
            })
    return {"mode": mode, "agents": agents}
