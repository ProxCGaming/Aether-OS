"""Epic 4, E4-06: LangMem-style memory backend (external adapter).

Selected via ``memory_backend = "langmem"``; requires the
``langmem`` package (``pip install langmem``) — without it the
backend is unavailable with a clear message.

Storage is a LangGraph-Store-shaped JSON file store
(``~/.aether/langmem_store.json``, atomic temp+rename writes — the
same crash-safety pattern as the native stores): namespaces hold
items with the BaseStore field shape (namespace / key / value /
timestamps). Two namespaces: ("aether", "facts") and
("aether", "episodes").

Honest scope notes:
- Extraction in learn() uses the engine's provider with the same
  JSON fact contract as the native extractor — LangMem's managed
  extraction pipelines are configuration-heavy and are not wired
  here; what IS wired is durable storage + retrieval behind the
  facade.
- Search is lexical (substring, recency-ranked), not embedding
  semantic. Ranking differing per backend is an owner-accepted
  property of the switchable-memory decision.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from aether_engine.memory.facade import MemoryBackend, MemoryBackendUnavailable

logger = logging.getLogger("aether_engine.memory.backends.langmem")

_FACTS_NS = ("aether", "facts")
_EPISODES_NS = ("aether", "episodes")
_DEFAULT_PATH = Path.home() / ".aether" / "langmem_store.json"


class JsonFileStore:
    """A minimal persistent store with the LangGraph BaseStore item
    shape. Every mutation rewrites the file atomically."""

    def __init__(self, path: Path | None = None):
        self._path = path or _DEFAULT_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._items: dict[str, dict] = {}
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text("utf-8"))
                if isinstance(raw, dict):
                    self._items = raw
            except (OSError, ValueError):
                logger.warning("LangMem store at %s is unreadable; starting empty", self._path)
                self._items = {}

    @staticmethod
    def _ns_key(namespace: tuple, key: str) -> str:
        return "/".join(namespace) + "::" + key

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._items), "utf-8")
        os.replace(tmp, self._path)

    @staticmethod
    def _item(ns_key: str, record: dict) -> SimpleNamespace:
        namespace, _, key = ns_key.rpartition("::")
        return SimpleNamespace(
            namespace=tuple(namespace.split("/")) if namespace else (),
            key=key,
            value=record["value"],
            created_at=record["created_at"],
            updated_at=record["updated_at"],
        )

    # Sync core — the facade's management/read methods are sync and
    # are called from inside the engine's event loop, so they must
    # never need asyncio.run. Async wrappers delegate via to_thread.
    def put(self, namespace: tuple, key: str, value: dict) -> None:
        ns_key = self._ns_key(namespace, key)
        now = time.time()
        created = self._items.get(ns_key, {}).get("created_at", now)
        self._items[ns_key] = {"value": value, "created_at": created, "updated_at": now}
        self._save()

    def get(self, namespace: tuple, key: str) -> SimpleNamespace | None:
        ns_key = self._ns_key(namespace, key)
        record = self._items.get(ns_key)
        return self._item(ns_key, record) if record else None

    def delete(self, namespace: tuple, key: str) -> bool:
        if self._items.pop(self._ns_key(namespace, key), None) is not None:
            self._save()
            return True
        return False

    def search(self, namespace: tuple, query: str | None = None, limit: int = 10) -> list:
        prefix = "/".join(namespace) + "::"
        records = [
            (ns_key, rec) for ns_key, rec in self._items.items() if ns_key.startswith(prefix)
        ]
        if query:
            needle = query.lower()
            records = [
                (k, r) for k, r in records
                if needle in json.dumps(r["value"]).lower()
            ]
        records.sort(key=lambda kv: kv[1]["updated_at"], reverse=True)
        return [self._item(k, r) for k, r in records[:limit]]

    async def aput(self, namespace: tuple, key: str, value: dict) -> None:
        await asyncio.to_thread(self.put, namespace, key, value)

    async def aget(self, namespace: tuple, key: str) -> SimpleNamespace | None:
        return await asyncio.to_thread(self.get, namespace, key)

    async def adelete(self, namespace: tuple, key: str) -> None:
        await asyncio.to_thread(self.delete, namespace, key)

    async def asearch(self, namespace: tuple, query: str | None = None, limit: int = 10) -> list:
        return await asyncio.to_thread(self.search, namespace, query, limit)


class LangMemMemoryBackend(MemoryBackend):
    name = "langmem"

    def __init__(self, store: Any = None, *, store_path: Path | None = None):
        try:
            import langmem  # noqa: F401 — presence is the gate; storage/extraction are this adapter's
        except ImportError as e:
            raise MemoryBackendUnavailable(
                "The langmem package is not installed (pip install langmem)."
            ) from e
        self._store = store if store is not None else JsonFileStore(store_path)

    # -- writes ---------------------------------------------------------
    async def record_episode(self, task_id, user_prompt, task_summary, tags, outcome, thinking_log=None):
        await self._store.aput(_EPISODES_NS, task_id, {
            "task_id": task_id,
            "user_prompt": user_prompt,
            "task_summary": task_summary,
            "tags": list(tags or []),
            "outcome": outcome,
            "timestamp": int(time.time()),
        })

    async def learn(self, provider, user_prompt, assistant_response, task_id):
        if len(user_prompt.strip()) < 3:
            return
        prompt = (
            "Extract concrete long-term facts from the conversation as a JSON array of "
            '{"entity_name": str, "entity_type": str, "fact": str, "confidence": float}. '
            "Return ONLY the JSON array.\n\n"
            f"User: {user_prompt}\nAssistant: {assistant_response}"
        )
        text = ""
        async for chunk in provider.call_stream(messages=[
            {"role": "system", "content": "You are a Knowledge Extraction Engine."},
            {"role": "user", "content": prompt},
        ]):
            if chunk.text:
                text += chunk.text
        cleaned = text.strip().strip("`")
        cleaned = cleaned.removeprefix("json")
        try:
            facts = json.loads(cleaned.strip())
        except ValueError:
            logger.warning("LangMem extraction returned invalid JSON; nothing stored")
            return
        if not isinstance(facts, list):
            return
        for item in facts:
            if not isinstance(item, dict) or not item.get("fact"):
                continue
            await self._store.aput(_FACTS_NS, str(uuid.uuid4()), {
                "entity_name": item.get("entity_name", "User"),
                "entity_type": item.get("entity_type", "Fact"),
                "fact_text": item["fact"],
                "confidence": item.get("confidence", 1.0),
                "source_task_id": task_id,
                "timestamp": int(time.time()),
                "status": "active",
            })

    # -- reads ------------------------------------------------------------
    @staticmethod
    def _fact_shape(item) -> dict:
        value = item.value
        return {
            "fact_id": item.key,
            "entity_name": value.get("entity_name", "User"),
            "entity_type": value.get("entity_type", "Fact"),
            "fact_text": value.get("fact_text", ""),
            "timestamp": value.get("timestamp"),
            "confidence": value.get("confidence"),
            "status": value.get("status", "active"),
        }

    def _all_facts(self, limit: int) -> list[dict]:
        return [self._fact_shape(i) for i in self._store.search(_FACTS_NS, limit=limit)]

    def search_facts(self, query, limit=10, include_history=False):
        items = self._store.search(_FACTS_NS, query=query, limit=limit)
        return [self._fact_shape(i) for i in items]

    def list_facts(self, limit=100, include_history=False):
        return self._all_facts(limit)

    def get_entity_facts(self, entity_name, include_history=False):
        return self.search_facts(entity_name, limit=50)

    async def search_episodes(self, query, limit=5):
        items = await self._store.asearch(_EPISODES_NS, query=query, limit=limit)
        return [dict(i.value) for i in items]

    def list_episodes(self, limit=50):
        items = self._store.search(_EPISODES_NS, limit=limit)
        return [dict(i.value) for i in items]

    # -- management ---------------------------------------------------------
    def delete_fact(self, fact_id):
        return self._store.delete(_FACTS_NS, fact_id)

    def update_fact(self, fact_id, new_text):
        item = self._store.get(_FACTS_NS, fact_id)
        if item is None:
            return False
        value = dict(item.value)
        value["fact_text"] = new_text
        value["timestamp"] = int(time.time())
        self._store.put(_FACTS_NS, fact_id, value)
        return True

    def delete_entity(self, entity_name):
        raise NotImplementedError("The LangMem backend has no entity grouping; delete individual facts.")

    def delete_episode(self, task_id):
        return self._store.delete(_EPISODES_NS, task_id)

    def clear_facts(self):
        for item in self._store.search(_FACTS_NS, limit=100_000):
            self._store.delete(_FACTS_NS, item.key)
        return True

    def clear_episodes(self):
        for item in self._store.search(_EPISODES_NS, limit=100_000):
            self._store.delete(_EPISODES_NS, item.key)
        return True

    def export(self):
        return {
            "version": 1,
            "backend": self.name,
            "exported_at": int(time.time()),
            "facts": self.list_facts(limit=100_000, include_history=True),
            "episodes": self.list_episodes(limit=100_000),
        }
