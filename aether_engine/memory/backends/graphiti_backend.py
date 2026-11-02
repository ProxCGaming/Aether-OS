"""Epic 4, E4-06: Graphiti memory backend (external adapter).

Wraps `graphiti-core` (``pip install graphiti-core``) + a Neo4j
database behind the facade. Graphiti builds a temporal knowledge
graph from episodes: learn()/record_episode() add episodes and
Graphiti extracts entities/edges itself; search() returns ranked
edges whose ``fact`` strings map onto facade facts.

Configuration comes from the environment: NEO4J_URI (default
bolt://localhost:7687), NEO4J_USER (default neo4j), NEO4J_PASSWORD
(required — without it the backend is unavailable with a message
saying so).

Async bridging: Graphiti's API is async while parts of the facade
are sync, so the adapter owns a dedicated event-loop thread and
runs coroutines on it (run_coroutine_threadsafe) — safe from both
sync and async callers.

Honest gaps (NotImplementedError, never fake success): Graphiti
has no list-all-facts or delete-one-fact API — facts are graph
edges managed through Cypher/Neo4j. list_facts approximates with a
broad search and says so; delete/update/clear raise.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import UTC, datetime
from typing import Any

from aether_engine.memory.facade import MemoryBackend, MemoryBackendUnavailable

logger = logging.getLogger("aether_engine.memory.backends.graphiti")


class _LoopThread:
    """One dedicated event loop on a daemon thread for this adapter."""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def run(self, coro, timeout: float = 60.0):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)


class GraphitiMemoryBackend(MemoryBackend):
    name = "graphiti"

    def __init__(self, graphiti: Any = None, *, loop_thread: _LoopThread | None = None):
        if graphiti is None:
            try:
                from graphiti_core import Graphiti
            except ImportError as e:
                raise MemoryBackendUnavailable(
                    "The graphiti-core package is not installed (pip install graphiti-core)."
                ) from e
            password = os.environ.get("NEO4J_PASSWORD")
            if not password:
                raise MemoryBackendUnavailable(
                    "Graphiti needs a Neo4j database: set NEO4J_URI / NEO4J_USER / "
                    "NEO4J_PASSWORD in the engine's environment, or use the native backend."
                )
            try:
                graphiti = Graphiti(
                    os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
                    os.environ.get("NEO4J_USER", "neo4j"),
                    password,
                )
            except Exception as e:
                raise MemoryBackendUnavailable(f"Graphiti could not initialise: {e}") from e
        self._g = graphiti
        self._loop = loop_thread or _LoopThread()

    # -- writes ---------------------------------------------------------
    async def record_episode(self, task_id, user_prompt, task_summary, tags, outcome, thinking_log=None):
        from graphiti_core.nodes import EpisodeType

        await self._g.add_episode(
            name=task_id,
            episode_body=f"Task: {user_prompt}\nOutcome: {outcome}",
            source=EpisodeType.text,
            source_description="aether episodic memory",
            reference_time=datetime.now(UTC),
        )

    async def learn(self, provider, user_prompt, assistant_response, task_id):
        from graphiti_core.nodes import EpisodeType

        await self._g.add_episode(
            name=f"learn-{task_id}",
            episode_body=f"User: {user_prompt}\nAssistant: {assistant_response}",
            source=EpisodeType.text,
            source_description="aether conversation",
            reference_time=datetime.now(UTC),
        )

    # -- reads ------------------------------------------------------------
    @staticmethod
    def _fact_shape(edge: Any) -> dict:
        return {
            "fact_id": str(getattr(edge, "uuid", "")),
            "entity_name": getattr(edge, "name", "Graphiti"),
            "entity_type": "Edge",
            "fact_text": getattr(edge, "fact", ""),
            "timestamp": None,
            "confidence": None,
            "status": "active",
        }

    def search_facts(self, query, limit=10, include_history=False):
        edges = self._loop.run(self._g.search(query, num_results=limit))
        return [self._fact_shape(e) for e in edges]

    def list_facts(self, limit=100, include_history=False):
        # Graphiti has no list-all API; a broad search is the closest
        # honest approximation and is documented as such.
        return self.search_facts("user", limit=limit)

    def get_entity_facts(self, entity_name, include_history=False):
        return self.search_facts(entity_name, limit=50)

    async def search_episodes(self, query, limit=5):
        edges = await self._g.search(query, num_results=limit)
        return [{
            "task_id": str(getattr(e, "uuid", "")),
            "user_prompt": getattr(e, "fact", ""),
            "outcome": getattr(e, "fact", ""),
            "timestamp": None,
        } for e in edges]

    def list_episodes(self, limit=50):
        return self._loop.run(self.search_episodes("task", limit=limit))

    # -- management ---------------------------------------------------------
    def delete_fact(self, fact_id):
        raise NotImplementedError(
            "Graphiti facts are graph edges; delete them via Neo4j/Cypher, not the facade.")

    def update_fact(self, fact_id, new_text):
        raise NotImplementedError(
            "Graphiti facts are derived from episodes and cannot be edited in place.")

    def delete_entity(self, entity_name):
        raise NotImplementedError(
            "Graphiti entities live in Neo4j; manage them via Cypher, not the facade.")

    def delete_episode(self, task_id):
        raise NotImplementedError(
            "Graphiti episode deletion is not exposed by graphiti-core's public API here.")

    def clear_facts(self):
        raise NotImplementedError("Clear the Neo4j database directly; the facade will not wipe a graph DB.")

    def clear_episodes(self):
        raise NotImplementedError("Clear the Neo4j database directly; the facade will not wipe a graph DB.")

    def export(self):
        return {
            "version": 1,
            "backend": self.name,
            "exported_at": int(time.time()),
            "note": "Graphiti export is a broad-search snapshot, not a full graph dump.",
            "facts": self.list_facts(limit=1000, include_history=True),
            "episodes": self.list_episodes(limit=1000),
        }
