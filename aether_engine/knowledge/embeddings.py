"""LlamaIndex embedding adapter over Aether's ONE embedder (P4d).

The knowledge base embeds through ``episodic.embed_text`` — the
same resolution path (``episodic.resolve_embedder``) and the same
circuit breaker episodic memory and the workspace prose index use.
There is deliberately no second embedder and no cloud embedding
call by default: the configured embedder is local-first (Ollama,
``qwen3-embedding:0.6b``) and only an owner-configured prefixed
model name (an explicit opt-in in config) ever leaves the machine.

When the embedding endpoint is down, ``episodic.embed_text``
returns None; this adapter raises :class:`EmbeddingUnavailableError`
so callers can degrade honestly (ingestion records the file as
failed; search falls back to its lexical leg) instead of storing
or querying against a missing vector.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from llama_index.core.base.embeddings.base import Embedding
from llama_index.core.embeddings import BaseEmbedding

from aether_engine.config import UserConfig
from aether_engine.memory import episodic


class EmbeddingUnavailableError(RuntimeError):
    """The configured embedding endpoint did not answer."""


def _run_blocking(coro_factory) -> Any:
    """Run an async embedding call from synchronous LlamaIndex code.

    LlamaIndex's sync embedding hooks are only reached from sync
    index operations; Aether's own ingestion and search drive the
    async hooks. When a sync hook IS reached with no event loop
    running, ``asyncio.run`` suffices; with a loop already running
    (a sync tool call inside the engine), the coroutine runs on a
    dedicated thread with its own loop — ``episodic.embed_text``
    is a self-contained HTTP call, safe from either.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    outcome: dict[str, Any] = {}

    def _target() -> None:
        try:
            outcome["value"] = asyncio.run(coro_factory())
        except BaseException as exc:  # noqa: BLE001 — thread boundary: any failure (incl. cancellation) must reach the caller, never die silently in the thread
            outcome["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


class AetherEmbedding(BaseEmbedding):
    """A LlamaIndex ``BaseEmbedding`` backed by ``episodic.embed_text``.

    ``model_name`` reports the configured embedder so an index built
    through this adapter can be checked against the embedder that
    built it (see ``knowledge.ingest`` — a different embedder means
    a different vector space, and mixing them is never silent).
    """

    def __init__(self, *, config: UserConfig | None = None, **kwargs: Any) -> None:
        super().__init__(model_name=episodic.resolve_embedder(config), **kwargs)
        self._config = config

    async def _embed(self, text: str, *, context: str) -> Embedding:
        vector = await episodic.embed_text(
            text, config=self._config, context=context)
        if vector is None:
            raise EmbeddingUnavailableError(
                f"embedding endpoint unavailable for embedder "
                f"{self.model_name!r} (context={context})")
        return vector

    async def _aget_query_embedding(self, query: str) -> Embedding:
        return await self._embed(query, context="knowledge_search")

    async def _aget_text_embedding(self, text: str) -> Embedding:
        return await self._embed(text, context="knowledge_index")

    def _get_query_embedding(self, query: str) -> Embedding:
        return _run_blocking(lambda: self._aget_query_embedding(query))

    def _get_text_embedding(self, text: str) -> Embedding:
        return _run_blocking(lambda: self._aget_text_embedding(text))
