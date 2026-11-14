"""Phase 10.3 A1: prune all memory tiers in one call.

Each tier owns its prune function (episodic / knowledge graph / sessions);
this module is the single entry point the engine's scheduler calls, so a
scheduled run prunes every tier with one call and one audit event.
S5 adds the KG confidence-decay pass as a final step of the same
weekly run (the scheduler's memory_prune_weekly job is the seam —
see prune_all).
"""
import logging
from typing import Any

from aether_engine.audit import AuditLogger
from aether_engine.config import UserConfig, load_config
from aether_engine.memory import episodic, knowledge_graph, session_store

logger = logging.getLogger("aether_engine.memory.pruning")
_audit_logger = AuditLogger()


def prune_all(config: UserConfig | None = None) -> dict[str, dict[str, Any]]:
    """Run retention pruning on all three memory tiers.

    Bounds come from ``config`` (or ``load_config()`` when omitted) — see
    UserConfig in aether_engine/config.py for the documented defaults.
    A tier that fails raises: the caller (scheduler) logs it, and a later
    run retries — pruning must never silently skip a tier.
    Returns per-tier result dicts and logs MEMORY_PRUNED_ALL.
    """
    cfg = config if config is not None else load_config()
    results = {
        "episodic": episodic.prune_episodes(config=cfg),
        "knowledge_graph": knowledge_graph.prune_facts(config=cfg),
        "sessions": session_store.prune_sessions(config=cfg),
    }
    # S5: confidence decay runs AFTER retention, on whatever
    # retention kept — the trust layer on top of the caps, not a
    # second retention policy (owner's #6 direction). Inferred KG
    # facts only; stated facts are exempt inside the pass. See
    # knowledge_graph.decay_inferred_facts for the formula, the
    # demote-before-delete floor, and the most-current guard.
    results["decay"] = knowledge_graph.decay_inferred_facts(config=cfg)
    _audit_logger.log_event("MEMORY_PRUNED_ALL", results)
    logger.info("MEMORY_PRUNED_ALL %s", results)
    return results
