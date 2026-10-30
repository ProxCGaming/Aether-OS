"""Phase 10.3 A1: prune all memory tiers in one call.

Each tier owns its prune function (episodic / knowledge graph / sessions);
this module is the single entry point the engine's scheduler calls, so a
scheduled run prunes every tier with one call and one audit event.
"""
import logging
from typing import Any, Dict, Optional

from aether_engine.audit import AuditLogger
from aether_engine.config import UserConfig, load_config
from aether_engine.memory import episodic, knowledge_graph, session_store

logger = logging.getLogger("aether_engine.memory.pruning")
_audit_logger = AuditLogger()


def prune_all(config: Optional[UserConfig] = None) -> Dict[str, Dict[str, Any]]:
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
    _audit_logger.log_event("MEMORY_PRUNED_ALL", results)
    logger.info("MEMORY_PRUNED_ALL %s", results)
    return results
