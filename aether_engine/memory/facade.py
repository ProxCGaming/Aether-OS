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


def _summary_line(text: Any) -> str:
    """One stored text as a one-line summary for a wire payload
    (SPEC-6B Rule 3): whitespace collapsed, capped — the panel
    shows WHICH records went, not a raw store dump."""
    line = " ".join(str(text or "").split())
    if len(line) > 200:
        line = line[:200].rstrip() + "…"
    return line


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

    async def search_facts_hybrid(self, query: str, limit: int = 10, include_history: bool = False) -> list[dict]:
        """S6: fact search with a semantic leg, where the backend
        has one. The base implementation is the backend's own
        search_facts — external backends rank their own way and
        this adds nothing for them. NativeMemoryBackend overrides
        with the KG's hybrid (substring priority + embedding
        recall, degrading to exactly search_facts when embeddings
        are unavailable). Result shape is the search_facts shape;
        rows the semantic leg attested may carry one extra key,
        "semantic_similarity"."""
        if include_history:
            return self.search_facts(query, limit=limit, include_history=True)
        return self.search_facts(query, limit=limit)

    def user_profile(self, *, limit: int | None = None,
                     offset: int = 0) -> dict[str, Any]:
        """S6: the user/relationship profile VIEW over this
        backend's facts (memory/profile.py) — a derivation from
        the stored records on every call, not a store: there is
        nothing separate to correct, and edits through
        update_fact / delete_fact show up on the next read.

        SPEC-6B C1: ``limit`` + ``offset`` paginate per section
        and the result carries per-section ``totals``; every item
        carries ``source_label`` (the human provenance string,
        derived in the view from provenance + timestamp). The
        defaults reproduce the full sections exactly as before
        pagination existed — existing callers (the fast-chat
        digest) are unaffected."""
        from aether_engine.memory import profile

        return profile.build_user_profile(self, limit=limit, offset=offset)

    def profile_briefing(self) -> str:
        """S6: the profile view as a bounded briefing string
        ("" when nothing about the user is stored)."""
        from aether_engine.memory import profile

        return profile.profile_briefing(self)

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

    # -- S8: snapshots + conversational forget ---------------------
    def snapshot_fact(self, fact_id: str) -> dict[str, Any] | None:
        """The FULL stored record for one fact (every field, any
        status) — the tombstone snapshot conversational forget
        restores from. Backends whose stores cannot hand back a
        complete record raise NotImplementedError: the forget
        service then still deletes through delete_fact, but
        reports undo as unsupported rather than faking it."""
        raise NotImplementedError(
            f"The {self.name} backend cannot snapshot facts for restore")

    def restore_fact_snapshot(self, snapshot: dict[str, Any]) -> bool:
        """Re-insert a snapshot from :meth:`snapshot_fact` with its
        original id and fields. True only when a row was inserted."""
        raise NotImplementedError(
            f"The {self.name} backend cannot restore fact snapshots")

    def snapshot_episode(self, task_id: str) -> dict[str, Any] | None:
        """The FULL stored record for one episode — the episodic
        twin of :meth:`snapshot_fact`, same honest-gap contract."""
        raise NotImplementedError(
            f"The {self.name} backend cannot snapshot episodes for restore")

    def restore_episode_snapshot(self, snapshot: dict[str, Any]) -> bool:
        """Re-insert a snapshot from :meth:`snapshot_episode` with
        its original task_id and timestamp."""
        raise NotImplementedError(
            f"The {self.name} backend cannot restore episode snapshots")

    def find_forget_candidates(
        self,
        topic: str,
        *,
        fact_ids: list[str] | None = None,
        episode_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """S8: resolve a forget topic (or explicit ids) to the
        concrete records a forget would delete, deleting nothing
        (memory/forget.py — literal matching, never embeddings:
        recall may be fuzzy, deletion must not be)."""
        from aether_engine.memory import forget

        return forget.find_forget_candidates(
            self, topic, fact_ids=fact_ids, episode_ids=episode_ids)

    def forget_memory(
        self,
        topic: str,
        *,
        phrase: str = "",
        fact_ids: list[str] | None = None,
        episode_ids: list[str] | None = None,
        config=None,
    ) -> dict[str, Any]:
        """S8: execute a conversational forget — cascading delete
        across facts, episodes, learned.md and the curated
        document, with a tombstone snapshot for undo. The result
        lists exactly what was deleted (empty = nothing matched,
        nothing deleted) and whether undo is supported on this
        backend."""
        from aether_engine.memory import forget

        return forget.execute_forget(
            self, topic, phrase=phrase, fact_ids=fact_ids,
            episode_ids=episode_ids, config=config)

    def restore_last_forget(self, *, config=None) -> dict[str, Any] | None:
        """S8: undo the most recent conversational forget (restore
        its tombstone, mark it consumed). None = nothing to undo."""
        from aether_engine.memory import forget

        return forget.restore_last_forget(self, config=config)

    # -- SPEC-6B C2: the Forget centre's panel calls --------------
    def forget_candidates(self) -> list[dict[str, Any]]:
        """Topic discovery for the Forget centre: distinct topics
        ranked by item count, each with the fact/episode counts a
        forget of it would touch (memory/forget.py —
        discover_forget_topics; counting reuses the same finder
        the deleter runs, so the panel never promises a different
        set than execute would remove)."""
        from aether_engine.memory import forget

        return forget.discover_forget_topics(self)

    def forget_execute(self, topic: str, *, config=None) -> dict[str, Any]:
        """Execute a panel forget of one topic and report it in
        the panel's shape: the EXACT removed list —
        ``[{"fact_id", "summary"}…, {"episode_id", "summary"}…]``
        (one-line summaries, SPEC-6B Rule 3) — plus the counts
        (lessons included, so nothing the cascade touched is
        hidden), the tombstone id undo would consume, and whether
        undo is supported on this backend. Runs the same
        cascading service as conversational forget
        (:meth:`forget_memory`); only the report shape differs."""
        result = self.forget_memory(topic, config=config)
        deleted = result["deleted"]
        removed = [
            {"fact_id": item["id"], "summary": _summary_line(item["text"])}
            for item in deleted["facts"]
        ] + [
            {"episode_id": item["id"], "summary": _summary_line(item["text"])}
            for item in deleted["episodes"]
        ]
        return {
            "topic": result["topic"],
            "removed": removed,
            "counts": result["counts"],
            "tombstone_id": result["tombstone_id"],
            "undo_supported": result["undo_supported"],
        }

    def forget_undo(self, *, config=None) -> dict[str, Any]:
        """Undo the most recent forget for the panel. Nothing to
        undo is an honest empty result — ``{"undone": False}``
        with zero counts, NOT an error; a tombstone the active
        backend may not restore reports ``undone: False`` with
        the reason under ``error``."""
        result = self.restore_last_forget(config=config)
        empty_counts = {"facts": 0, "episodes": 0, "lessons": 0}
        empty_restored = {"facts": [], "episodes": [], "lessons": []}
        if result is None:
            return {
                "undone": False,
                "topic": "",
                "counts": empty_counts,
                "restored": empty_restored,
            }
        if result.get("error"):
            return {
                "undone": False,
                "topic": result.get("topic", ""),
                "counts": empty_counts,
                "restored": empty_restored,
                "error": result["error"],
            }
        restored = result["restored"]
        return {
            "undone": bool(
                restored["facts"] or restored["episodes"]
                or restored["lessons"]),
            "topic": result.get("topic", ""),
            "counts": {
                kind: len(restored[kind])
                for kind in ("facts", "episodes", "lessons")
            },
            "restored": restored,
            "unrestored": result.get("unrestored", []),
        }

    def forget_history(self, limit: int = 20,
                       offset: int = 0) -> dict[str, Any]:
        """The Forgotten history: tombstone entries, newest first
        — ``{"entries": [{tombstone_id, topic, fact_count,
        episode_count, forgotten_at, undoable}…], "total": n}``;
        ``undoable`` is true only for the latest unconsumed
        tombstone (memory/forget.py — forget_history)."""
        from aether_engine.memory import forget

        return forget.forget_history(limit=limit, offset=offset)

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

    def close(self) -> None:
        """Release backend-owned resources. Most backends own none."""

    def shutdown(self) -> None:
        self.close()


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

    async def search_facts_hybrid(self, query, limit=10, include_history=False):
        from aether_engine.memory import knowledge_graph

        return await knowledge_graph.search_facts_semantic(
            query, limit=limit, include_history=include_history)

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

    def snapshot_fact(self, fact_id):
        from aether_engine.memory import knowledge_graph

        return knowledge_graph.get_fact_snapshot(fact_id)

    def restore_fact_snapshot(self, snapshot):
        from aether_engine.memory import knowledge_graph

        return knowledge_graph.restore_fact_snapshot(snapshot)

    def snapshot_episode(self, task_id):
        from aether_engine.memory import episodic

        return episodic.get_episode_snapshot(task_id)

    def restore_episode_snapshot(self, snapshot):
        from aether_engine.memory import episodic

        return episodic.restore_episode_snapshot(snapshot)

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


def close_memory_backend(backend_name: str | None = None) -> bool:
    """Close and evict one cached backend, or all of them.

    Returns True when at least one cached backend was closed. A
    closed backend is removed first so a concurrent resolver cannot
    receive an instance whose resources are being released; the next
    ``get_memory_backend`` call constructs a fresh one.
    """
    if backend_name is not None:
        names = [str(backend_name).strip().lower()]
    else:
        names = list(_BACKEND_CACHE)
    closed = False
    for name in names:
        backend = _BACKEND_CACHE.pop(name, None)
        if backend is None:
            continue
        backend.close()
        closed = True
    return closed


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
