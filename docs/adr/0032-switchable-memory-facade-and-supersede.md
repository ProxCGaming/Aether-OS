# ADR 0032 — Switchable Memory Facade & Fact Supersede

**Status:** Accepted
**Date:** 2026-10-31
**Branch:** build/full-build (Epic 4)

## Context

Memory is Aether-OS's core differentiator, and two structural gaps
remained after the Phase 10 work. First, every consumer (executor
learn paths, supervisor memory tools, websocket management
handlers) called the episodic/KG modules directly, so the owner's
switchable-memory decision (30 Oct 2026 — a common facade, native
engine as production default, Mem0 / LangMem / Graphiti pluggable,
no automatic migration, embeddings do not transfer) had nowhere to
land. Second, the knowledge graph could dedupe but not *change its
mind*: a contradicting new fact coexisted with the old one forever,
so retrieval could present stale facts as current.

## Decisions

**D1 — One facade, native default, loud degradation.**
`aether_engine/memory/facade.py` defines the `MemoryBackend`
surface the engine actually uses (record episode, learn,
search/list facts + episodes, delete/update, clear, export,
embedding stats). `NativeMemoryBackend` delegates 1:1 to the
existing stores; its default path calls them with their original
signatures so behaviour is byte-identical. All engine call sites
resolve through `get_memory_backend(UserConfig.memory_backend)`;
`backend_or_none` is the task-path contract — an unavailable
external backend logs loudly and yields no memory for that call; it
never silently swaps engines and never crashes a task.
`memory_backend` sits behind the config-layer trust boundary: a
cloned workspace cannot choose the engine's memory store.

**D2 — External adapters are real or absent, never simulated.**
Mem0, LangMem, and Graphiti adapters live in `memory/backends/`,
lazily imported. Construction raises `MemoryBackendUnavailable`
naming the missing package/service/credential. Operations a
library genuinely lacks (Graphiti has no delete-one-fact) raise
NotImplementedError with the reason. LangMem's storage is a
persistent JSON file store with the LangGraph BaseStore item
shape, deliberately scoped (lexical search; extraction via the
engine provider with the native JSON fact contract) and documented
as such in its module.

**D3 — Contradiction = supersede, never overwrite.**
Facts gain `status` ('active' | 'superseded') + `superseded_by`
via an idempotent migration in `init_kg_db` (existing rows default
to 'active'). During extraction, entities that gained facts get
one reconciliation call: the provider marks which older active
facts the new ones directly replace; those rows are marked, kept
(they ARE the history), and excluded from every default retrieval
path. `include_history=True` surfaces them for the Memory UI.
Reconciliation is best-effort: its failure leaves all facts active
— storing is never undone by a reconciliation problem.

**D4 — Learn parity for fast chat.** Fast chat had no learn path
(Epic 1 deferred it here). Its completions now learn through the
same facade in a detached background task
(`fast_chat.learn_fast_chat_exchange`), using the chat's own
provider for extraction; episodes carry tags ["chat", "fast"].

## Consequences

- Switching backends is a config change with explicit, accepted
  semantics: stores are independent, nothing migrates, ranking and
  extraction differ per backend, and several management operations
  are native-only (the UI learns the truth from the backend's own
  responses, not from promises).
- Retrieval can no longer present a replaced fact as current,
  while the full history remains inspectable and exportable.
- Embedding health (embedded vs pending, last failure) is data,
  not log archaeology: `episodic.embedding_stats()` rides the
  MEMORY_EPISODES_RESPONSE into Diagnostics, and startup backfills
  up to 200 unembedded episodes for the native backend.
