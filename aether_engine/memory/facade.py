"""Epic 4, E4-01: the switchable memory backend facade.

Every engine consumer of memory — the executor's completion learn
path, the supervisor's memory tools, fast chat, and the websocket
MEMORY_* handlers — talks to this surface, never to a store
directly. The native engine (episodic.py + knowledge_graph.py) is
the production default and is delegated to 1:1: selecting "native"
changes no behaviour. Mem0 / LangMem / Graphiti adapters live in
``backends/`` and are imported lazily; selecting one without its
package or service raises :class:`MemoryBackendUnavailable` with a
message naming the missing piece — the call sites degrade LOUDLY
(log + proceed without memory for that call), never silently swap
engines and never crash a task over memory.

Owner decisions honoured (30 Oct 2026): no automatic migration
between backends, embeddings do not transfer, ranking differs per
backend. Switching backends switches stores; the old store's data
stays where it was.
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("aether_engine.memory.facade")

VALID_BACKENDS = ("native", "mem0", "langmem", "graphiti")


class MemoryBackendUnavailable(RuntimeError):
    """The configured backend cannot run here (missing package,
    missing service, missing credentials). The message says what."""


class MemoryBackend:
    """The surface the engine uses. Methods a backend genuinely
    cannot support raise NotImplementedError with a reason — an
    honest gap, never a fake success."""

    name = "base"

    # -- writes ------------------------------------------------------
    async def record_episode(self, task_id: str, user_prompt: str, task_summary: str,
                             tags: list[str], outcome: str, thinking_log: str | None = None) -> None:
        raise NotImplementedError

    async def learn(self, provider, user_prompt: str, assistant_response: str, task_id: str) -> None:
        """Extract and store durable facts from one exchange."""
        raise NotImplementedError

    async def learn_lessons(self, provider, user_prompt: str, assistant_response: str, task_id: str) -> list[str]:
        """Epic 4, E4-10 (addendum omp #29): extract explicit
        lessons from one completed exchange into learned.md.

        Unlike the store operations above, this default is the real
        implementation for every backend: learned.md is a curated
        document in the data home, not a backend store, so there is
        nothing per-backend to override. Returns the lessons that
        were actually added (deduped against the document).
        Best-effort — extraction failures return []."""
        from aether_engine.memory import learned

        return await learned.extract_and_record_lessons(
            provider, user_prompt, assistant_response, task_id)

    # -- reads -------------------------------------------------------
    def search_facts(self, query: str, limit: int = 10, include_history: bool = False) -> list[dict]:
        raise NotImplementedError

    def list_facts(self, limit: int = 100, include_history: bool = False) -> list[dict]:
        raise NotImplementedError

    def get_entity_facts(self, entity_name: str, include_history: bool = False) -> list[dict]:
        raise NotImplementedError

    async def search_episodes(self, query: str, limit: int = 5) -> list[dict]:
        raise NotImplementedError

    def list_episodes(self, limit: int = 50) -> list[dict]:
        raise NotImplementedError

    # -- management --------------------------------------------------
    def delete_fact(self, fact_id: str) -> bool:
        raise NotImplementedError

    def update_fact(self, fact_id: str, new_text: str) -> bool:
        raise NotImplementedError

    def delete_entity(self, entity_name: str) -> bool:
        raise NotImplementedError

    def delete_episode(self, task_id: str) -> bool:
        raise NotImplementedError

    def clear_facts(self) -> bool:
        raise NotImplementedError

    def clear_episodes(self) -> bool:
        raise NotImplementedError

    def export(self) -> dict[str, Any]:
        raise NotImplementedError

    def embedding_stats(self) -> dict[str, Any] | None:
        """Embedding health for Diagnostics, or None when the
        backend has no embedding store of its own to report on."""
        return None


class NativeMemoryBackend(MemoryBackend):
    """The production default: delegates 1:1 to the native stores."""

    name = "native"

    async def record_episode(self, task_id, user_prompt, task_summary, tags, outcome, thinking_log=None):
        from aether_engine.memory import episodic

        await episodic.store_episode(task_id, user_prompt, task_summary, tags, outcome, thinking_log)

    async def learn(self, provider, user_prompt, assistant_response, task_id):
        from aether_engine.memory import knowledge_graph

        await knowledge_graph.extract_and_store_facts(provider, user_prompt, assistant_response, task_id)

    # The native functions gained include_history in Epic 4; the
    # default path calls them with their ORIGINAL signatures so
    # existing callers/monkeypatches see zero change.
    def search_facts(self, query, limit=10, include_history=False):
        from aether_engine.memory import knowledge_graph

        if include_history:
            return knowledge_graph.search_facts(query, limit=limit, include_history=True)
        return knowledge_graph.search_facts(query, limit=limit)

    def list_facts(self, limit=100, include_history=False):
        from aether_engine.memory import knowledge_graph

        if include_history:
            return knowledge_graph.get_all_facts(limit=limit, include_history=True)
        return knowledge_graph.get_all_facts(limit=limit)

    def get_entity_facts(self, entity_name, include_history=False):
        from aether_engine.memory import knowledge_graph

        if include_history:
            return knowledge_graph.get_entity_facts(entity_name, include_history=True)
        return knowledge_graph.get_entity_facts(entity_name)

    async def search_episodes(self, query, limit=5):
        from aether_engine.memory import episodic

        return await episodic.query_episodes(query, limit=limit)

    def list_episodes(self, limit=50):
        from aether_engine.memory import episodic

        return episodic.get_all_episodes(limit=limit)

    def delete_fact(self, fact_id):
        from aether_engine.memory import knowledge_graph

        return knowledge_graph.delete_fact(fact_id)

    def update_fact(self, fact_id, new_text):
        from aether_engine.memory import knowledge_graph

        return knowledge_graph.update_fact(fact_id, new_text)

    def delete_entity(self, entity_name):
        from aether_engine.memory import knowledge_graph

        return knowledge_graph.delete_entity(entity_name)

    def delete_episode(self, task_id):
        from aether_engine.memory import episodic

        return episodic.delete_episode(task_id)

    def clear_facts(self):
        from aether_engine.memory import knowledge_graph

        return knowledge_graph.clear_all_facts()

    def clear_episodes(self):
        from aether_engine.memory import episodic

        return episodic.clear_all_episodes()

    def export(self):
        return {
            "version": 1,
            "backend": self.name,
            "exported_at": int(time.time()),
            "facts": self.list_facts(limit=100_000, include_history=True),
            "episodes": self.list_episodes(limit=100_000),
        }

    def embedding_stats(self):
        from aether_engine.memory import episodic

        return episodic.embedding_stats()


_BACKEND_CACHE: dict[str, MemoryBackend] = {}


def get_memory_backend(user_config=None, *, backend_name: str | None = None) -> MemoryBackend:
    """Resolve the configured backend (cached per name).

    Raises MemoryBackendUnavailable when an external backend cannot
    be constructed here. An unknown name is a config error and also
    raises — silently treating it as native would hide a typo behind
    a different store.
    """
    name = backend_name or getattr(user_config, "memory_backend", None) or "native"
    name = str(name).strip().lower()
    if name in _BACKEND_CACHE:
        return _BACKEND_CACHE[name]
    if name == "native":
        backend: MemoryBackend = NativeMemoryBackend()
    elif name in ("mem0", "langmem", "graphiti"):
        try:
            if name == "mem0":
                from aether_engine.memory.backends.mem0_backend import Mem0MemoryBackend

                backend = Mem0MemoryBackend()
            elif name == "langmem":
                from aether_engine.memory.backends.langmem_backend import (
                    LangMemMemoryBackend,
                )

                backend = LangMemMemoryBackend()
            else:
                from aether_engine.memory.backends.graphiti_backend import (
                    GraphitiMemoryBackend,
                )

                backend = GraphitiMemoryBackend()
        except ImportError as e:
            raise MemoryBackendUnavailable(
                f"The {name} memory backend is not available in this install: {e}"
            ) from e
    else:
        raise MemoryBackendUnavailable(
            f"Unknown memory backend {name!r}; valid backends: {', '.join(VALID_BACKENDS)}"
        )
    _BACKEND_CACHE[name] = backend
    return backend


def backend_or_none(user_config=None, *, context: str = "memory call",
                    backend_name: str | None = None) -> MemoryBackend | None:
    """The configured backend, or None (with a loud log) when it is
    unavailable — the degradation contract for task paths: memory
    must never crash a chat, and its absence must never be silent.
    """
    try:
        return get_memory_backend(user_config, backend_name=backend_name)
    except MemoryBackendUnavailable as e:
        logger.warning("Memory backend unavailable for %s: %s", context, e)
        return None
