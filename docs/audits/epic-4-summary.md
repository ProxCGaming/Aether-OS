# Epic 4 — Memory That Actually Works: PR-style summary

Branch `build/full-build`. Baseline at epic start (Epic 3 close):
pytest 799 passed / 13 known-failed / 2 skipped; Jest 216/216.
At Epic 4 close: **pytest 812 passed / 13 known-failed / 2 skipped;
Jest 222/222**. The 13 failures are the long-standing pre-existing
families — zero regressions. Records: ADR 0032; PRD at
`epic4-prd-and-issues.md` in the goal files.

## What shipped, per issue
- **E4-01 — Facade + native default** (`774c8d4`, wiring in
  `2de8429`): `memory/facade.py` defines the MemoryBackend surface;
  NativeMemoryBackend delegates 1:1 (default path uses the native
  functions' original signatures — behaviour byte-identical, pinned
  by the parity suite). Executor completion + resume learn paths,
  supervisor memory tools, and all MEMORY_* websocket handlers
  resolve through `backend_or_none(UserConfig.memory_backend)`:
  loud log + no memory for that call when an external backend is
  unavailable; never a silent engine swap, never a crashed task.
  `memory_backend` is behind the config-layer trust boundary.
- **E4-02 — Supersede, never overwrite** (`6f8e1b0`): facts gain
  status/superseded_by by idempotent migration; extraction
  reconciles per entity (one extra provider call) and marks
  contradicted actives as superseded — kept as history, excluded
  from all default retrieval, auditable. Reconciliation failure
  leaves everything active (no data loss). Dedupe now matches
  active facts only.
- **E4-03 — Fact edit + export** (`6f8e1b0`, `2de8429`, UI
  `504476a`): `update_fact` (refuses empties and duplicate-minting
  edits), MEMORY_FACT_UPDATE + MEMORY_EXPORT contracts/handlers,
  export = full memory JSON (facts incl. history + episodes).
- **E4-04 — Backfill + embedding health** (`2de8429`): startup
  kicks a bounded (200) backfill for the native backend;
  `episodic.embedding_stats()` (totals / pending / last failure)
  rides MEMORY_EPISODES_RESPONSE into the Memory UI.
- **E4-05 — Fast-chat learn parity** (`2de8429`, core refactored
  in `a319631`): `fast_chat.learn_fast_chat_exchange` runs detached
  after fast-chat TASK_COMPLETED with the chat's provider — a fact
  stated in fast chat is retrievable by a later graph task
  (test-pinned).
- **E4-06 — External adapters** (`a7a39ac`): Mem0 (own extraction;
  episodes verbatim via infer=False + metadata), LangMem (requires
  the package; persistent LangGraph-Store-shaped JSON store,
  lexical search, provider-driven extraction — scope documented in
  the module), Graphiti (Neo4j env-gated; loop-thread bridging;
  honest NotImplementedError where graphiti-core has no API). All
  raise MemoryBackendUnavailable naming the missing piece;
  mappings unit-tested against stubbed libraries. None of the three
  packages is installed in this environment, so live end-to-end
  runs against real Mem0/Neo4j remain the owner's to exercise after
  installing — the unavailable path IS exercised and pinned.
- **E4-07 — Budget, scale, acceptance** (`a319631`): supervisor
  memory results capped at 2000 chars with a truncation marker.
  Acceptance as tests: learn → supervisor tool visibly uses the
  fact → delete → gone from retrieval → export contains the rest.
  Scale (this VM, seeded): 500-fact search 0.8 ms; 2,000-episode
  hybrid query 122 ms.
- **E4-08 — Memory UI** (`504476a`): fact search (engine query
  branch on MEMORY_GRAPH_REQUEST), Show-superseded toggle with
  badged history rows, inline fact edit, export download, backend +
  embedding stats line. Jest +6 net (222 total).

## Also in this epic
knowledge_graph.py and episodic.py received their first post-gate
ruff cleanups (annotation modernisation, removeprefix/removesuffix,
TimeoutError builtin alias) — behaviour-equal, verified by their
suites. A subagent filesystem-sync delay was observed during the
UI batch (its writes flushed minutes after its report); the lead
verified the landed files directly before committing.

## Deliberately not done
Cross-backend migration and embedding transfer (owner-accepted
limits); Graphiti/LangMem management operations their libraries
don't expose; semantic search for the LangMem adapter.
