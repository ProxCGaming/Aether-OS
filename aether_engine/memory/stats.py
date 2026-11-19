"""Full Furnish Wave 6 (SPEC-6B C4): memory store statistics.

The counts + decay read-out behind Settings → Memory's controls
row. Before this module the facade had no stats surface at all:
the panel could list facts and episodes but could not say how
many there are, how many are superseded history, or when the
decay pass last ran.

Honesty rules (SPEC-6B Rule 1):

* Counts come from the facade surface only (``list_facts`` with
  history, ``list_episodes``) — this module never talks to a
  store directly, so every backend answers through the same
  calls it already serves.
* A leg a backend cannot serve reports ``None`` for its number,
  never a fabricated 0: the board renders "not reported by this
  backend" for a null, which is the truth.
* The decay numbers come from the pruning run record — the
  ``MEMORY_PRUNED_ALL`` audit event ``memory/pruning.py`` writes
  on every scheduled cleanup (its ``decay`` section is the S5
  decay pass's own result dict). No record in the audit log
  means no cleanup has run under this engine: the three decay
  fields are null rather than invented history. A lone
  ``KNOWLEDGE_GRAPH_DECAYED`` event (the decay pass also logs
  itself) is accepted as the record when no full prune record
  exists.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("aether_engine.memory.stats")

# The scan bounds for the count legs. The stores' own retention
# caps (UserConfig.kg_max_facts / episodic_max_episodes) keep
# real stores far below these; the bound exists so a stats read
# stays a read, not an export.
_FACT_SCAN_LIMIT = 100_000
_EPISODE_SCAN_LIMIT = 100_000

# The contract's fade figure (SPEC-6B C4): the inferred-fact
# half-life the board names. It is also UserConfig's shipped
# default for memory_decay_half_life_days; a caller holding the
# live config may pass it so a changed setting is what the
# board reports.
_DEFAULT_FADE_DAYS = 45


def _count_facts(backend) -> tuple[int | None, int | None]:
    """(active, superseded) from one history-inclusive listing.

    Either number is None when the backend cannot list facts —
    an honest gap, per the module docstring."""
    try:
        facts = backend.list_facts(
            limit=_FACT_SCAN_LIMIT, include_history=True) or []
    except Exception:  # one failed leg must not sink the read
        logger.warning("memory_stats: fact listing failed", exc_info=True)
        return None, None
    active = 0
    superseded = 0
    for fact in facts:
        if str(fact.get("status") or "active") == "superseded":
            superseded += 1
        else:
            active += 1
    return active, superseded


def _count_episodes(backend) -> int | None:
    try:
        episodes = backend.list_episodes(limit=_EPISODE_SCAN_LIMIT) or []
    except Exception:  # one failed leg must not sink the read
        logger.warning("memory_stats: episode listing failed", exc_info=True)
        return None
    return len(episodes)


def _last_cleanup_record(audit_log_path: Path | None) -> dict[str, Any] | None:
    """The most recent pruning run record from the audit log.

    Returns ``{"ts": float, "decayed": int, "removed": int}``
    from the newest MEMORY_PRUNED_ALL event's decay section,
    falling back to the newest KNOWLEDGE_GRAPH_DECAYED event.
    None when the log is missing/unreadable or holds no record —
    the caller then reports nulls, never invented history.
    """
    if audit_log_path is None:
        from aether_engine.audit import DEFAULT_AUDIT_LOG_PATH

        audit_log_path = DEFAULT_AUDIT_LOG_PATH
    try:
        lines = Path(audit_log_path).read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    decay_event: dict[str, Any] | None = None
    for line in reversed(lines):
        if "MEMORY_PRUNED_ALL" not in line and "KNOWLEDGE_GRAPH_DECAYED" not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") != "ENGINE_EVENT":
            continue
        action = record.get("action")
        details = record.get("details") or {}
        if action == "MEMORY_PRUNED_ALL":
            decay = details.get("decay") or {}
            return {
                "ts": record.get("ts"),
                "decayed": decay.get("decayed"),
                "removed": decay.get("removed_below_floor"),
            }
        if action == "KNOWLEDGE_GRAPH_DECAYED" and decay_event is None:
            decay_event = {
                "ts": record.get("ts"),
                "decayed": details.get("decayed"),
                "removed": details.get("removed_below_floor"),
            }
    return decay_event


def _as_count(value: Any) -> int | None:
    """A run-record number, or None when the record lacks one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def memory_stats(
    backend,
    *,
    config=None,
    audit_log_path: Path | None = None,
) -> dict[str, Any]:
    """The C4 stats shape for one facade backend.

    ``{facts_active, facts_superseded, episodes, backend_id,
    backend_label, decay: {inferred_fade_days, last_cleanup_at,
    last_faded, last_removed}}`` — see the module docstring for
    which fields may honestly be null.
    """
    from aether_engine.memory import backend_status

    facts_active, facts_superseded = _count_facts(backend)
    episodes = _count_episodes(backend)

    backend_id = str(getattr(backend, "name", "") or "")
    copy = backend_status._BACKEND_COPY.get(backend_id) or {}
    backend_label = copy.get("name") or backend_id

    fade_days: Any = _DEFAULT_FADE_DAYS
    if config is not None:
        configured = getattr(config, "memory_decay_half_life_days", None)
        if isinstance(configured, (int, float)) and not isinstance(configured, bool):
            fade_days = configured
            if float(fade_days).is_integer():
                fade_days = int(fade_days)

    record = _last_cleanup_record(audit_log_path)
    if record is None:
        decay = {
            "inferred_fade_days": fade_days,
            "last_cleanup_at": None,
            "last_faded": None,
            "last_removed": None,
        }
    else:
        ts = record.get("ts")
        decay = {
            "inferred_fade_days": fade_days,
            "last_cleanup_at": (
                float(ts) if isinstance(ts, (int, float))
                and not isinstance(ts, bool) else None),
            "last_faded": _as_count(record.get("decayed")),
            "last_removed": _as_count(record.get("removed")),
        }

    return {
        "facts_active": facts_active,
        "facts_superseded": facts_superseded,
        "episodes": episodes,
        "backend_id": backend_id,
        "backend_label": backend_label,
        "decay": decay,
    }
