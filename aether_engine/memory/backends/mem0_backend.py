"""Epic 4, E4-06: Mem0 memory backend (external adapter).

Wraps the `mem0` package (``pip install mem0ai``) behind the
facade. Mem0 does its own LLM extraction in ``add()`` and its own
ranking in ``search()`` — both differ from the native engine, which
is an owner-accepted property of switching backends.

Mapping onto the facade:
- learn()        → mem0.add(messages) — Mem0 extracts the facts.
- record_episode → mem0.add(..., infer=False, metadata={episode})
  so episodes are stored verbatim and findable again.
- facts          → the user's mem0 memories (user_id "aether-user").
- episodes       → memories whose metadata marks them as episodes.

Construction fails with MemoryBackendUnavailable when the package
is missing or Mem0 itself cannot initialise (its default config
needs an LLM key, e.g. OPENAI_API_KEY) — the message carries the
underlying error so the fix is obvious.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aether_engine.memory.facade import MemoryBackend, MemoryBackendUnavailable

logger = logging.getLogger("aether_engine.memory.backends.mem0")

_USER_ID = "aether-user"


class Mem0MemoryBackend(MemoryBackend):
    name = "mem0"

    def __init__(self, memory: Any = None):
        if memory is None:
            try:
                from mem0 import Memory
            except ImportError as e:
                raise MemoryBackendUnavailable(
                    "The mem0 package is not installed (pip install mem0ai)."
                ) from e
            try:
                memory = Memory()
            except Exception as e:
                raise MemoryBackendUnavailable(
                    f"Mem0 could not initialise: {e}. Mem0's default config needs an "
                    "LLM provider key (e.g. OPENAI_API_KEY) — configure Mem0 or use the native backend."
                ) from e
        self._m = memory

    # -- helpers ------------------------------------------------------
    @staticmethod
    def _fact_shape(item: dict) -> dict:
        return {
            "fact_id": item.get("id"),
            "entity_name": "User",
            "entity_type": "Memory",
            "fact_text": item.get("memory", ""),
            "timestamp": item.get("created_at"),
            "confidence": item.get("score"),
            "status": "active",
            "metadata": item.get("metadata") or {},
        }

    @staticmethod
    def _results(payload: Any) -> list[dict]:
        if isinstance(payload, dict):
            return list(payload.get("results") or [])
        return list(payload or [])

    @staticmethod
    def _is_episode(item: dict) -> bool:
        # Facts and episodes share one Mem0 user; the metadata marker
        # record_episode() writes is the ONLY thing that separates the
        # two scopes (E4-F05). Every fact-side operation must honour it.
        return (item.get("metadata") or {}).get("aether_type") == "episode"

    # -- writes -------------------------------------------------------
    async def record_episode(self, task_id, user_prompt, task_summary, tags, outcome, thinking_log=None):
        body = f"Task: {user_prompt}\nOutcome: {outcome}"
        metadata = {"aether_type": "episode", "task_id": task_id, "tags": list(tags or [])}

        def _add():
            try:
                return self._m.add(
                    [{"role": "user", "content": body}],
                    user_id=_USER_ID, metadata=metadata, infer=False,
                )
            except TypeError:
                # Older mem0 without the infer flag: verbatim storage
                # is not guaranteed, but the episode is still kept.
                return self._m.add([{"role": "user", "content": body}],
                                   user_id=_USER_ID, metadata=metadata)

        await asyncio.to_thread(_add)

    async def learn(self, provider, user_prompt, assistant_response, task_id):
        messages = [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": assistant_response},
        ]
        await asyncio.to_thread(
            self._m.add, messages, user_id=_USER_ID, metadata={"task_id": task_id})

    # -- reads ----------------------------------------------------------
    def search_facts(self, query, limit=10, include_history=False):
        # Over-fetch like search_episodes does, then drop episodes:
        # without the marker filter an episode stored under the same
        # user came back shaped as a fact (E4-F05).
        payload = self._m.search(query, user_id=_USER_ID, limit=limit * 3)
        items = [i for i in self._results(payload) if not self._is_episode(i)]
        return [self._fact_shape(i) for i in items[:limit]]

    def list_facts(self, limit=100, include_history=False):
        payload = self._m.get_all(user_id=_USER_ID)
        items = [i for i in self._results(payload) if not self._is_episode(i)]
        return [self._fact_shape(i) for i in items[:limit]]

    def get_entity_facts(self, entity_name, include_history=False):
        return self.search_facts(entity_name, limit=50)

    async def search_episodes(self, query, limit=5):
        payload = await asyncio.to_thread(self._m.search, query, user_id=_USER_ID, limit=limit * 3)
        episodes = []
        for item in self._results(payload):
            if (item.get("metadata") or {}).get("aether_type") == "episode":
                episodes.append({
                    "task_id": (item.get("metadata") or {}).get("task_id"),
                    "user_prompt": item.get("memory", ""),
                    "outcome": item.get("memory", ""),
                    "timestamp": item.get("created_at"),
                })
            if len(episodes) >= limit:
                break
        return episodes

    def list_episodes(self, limit=50):
        payload = self._m.get_all(user_id=_USER_ID)
        episodes = []
        for item in self._results(payload):
            if (item.get("metadata") or {}).get("aether_type") == "episode":
                episodes.append({
                    "task_id": (item.get("metadata") or {}).get("task_id"),
                    "user_prompt": item.get("memory", ""),
                    "task_summary": "",
                    "outcome": item.get("memory", ""),
                    "timestamp": item.get("created_at"),
                })
            if len(episodes) >= limit:
                break
        return episodes

    # -- management -----------------------------------------------------
    def delete_fact(self, fact_id):
        self._m.delete(fact_id)
        return True

    def update_fact(self, fact_id, new_text):
        self._m.update(fact_id, new_text)
        return True

    def delete_entity(self, entity_name):
        raise NotImplementedError("Mem0 has no entity concept; delete individual facts instead.")

    def delete_episode(self, task_id):
        # Match the task ID AND the episode marker: facts learned from
        # the same task carry the task ID in their metadata too, and
        # matching the ID alone deleted the fact while the episode
        # itself survived (E4-F05).
        deleted = False
        for item in self._results(self._m.get_all(user_id=_USER_ID)):
            if (item.get("metadata") or {}).get("task_id") == task_id \
                    and self._is_episode(item):
                self._m.delete(item.get("id"))
                deleted = True
        return deleted

    def clear_facts(self):
        # delete_all(user_id) is user-wide: it would take every episode
        # with it. Clear fact records one by one instead (E4-F05).
        for item in self._results(self._m.get_all(user_id=_USER_ID)):
            if not self._is_episode(item):
                self._m.delete(item.get("id"))
        return True

    def clear_episodes(self):
        for item in self._results(self._m.get_all(user_id=_USER_ID)):
            if self._is_episode(item):
                self._m.delete(item.get("id"))
        return True

    def export(self):
        return {
            "version": 1,
            "backend": self.name,
            "exported_at": int(time.time()),
            "facts": self.list_facts(limit=100_000, include_history=True),
            "episodes": self.list_episodes(limit=100_000),
        }
