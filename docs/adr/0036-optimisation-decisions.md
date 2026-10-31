# ADR 0036 — Epic 9 optimisation decisions and the numbers behind them

Status: accepted, 31 Oct 2026. All measurements from
`benchmarks/epic9_bench.py` (in-repo, re-runnable; medians of the
stated run counts, this VM, no network, no Ollama).

## Before / after

| Metric | Before | After | How |
|---|---|---|---|
| Engine import (`aether_engine.app`) | 5,583 ms | 1,138 ms | litellm deferred (below) |
| RSS after engine init | 206 MB | 82 MB | same |
| Episodic hybrid query, 2,000 eps, embedding endpoint DOWN (median) | 105 ms | 5.6 ms | circuit breaker (below) |
| First hybrid query, endpoint down, 2,000 unembedded eps | 5,020 ms | one attempt per 60 s window, then ~ms | breaker + `num_retries=0` |
| UI entry chunk | 1,946 kB (524 kB gzip) | 537 kB (149 kB gzip) | 3D graph lazy-loaded |
| KG search, 500 facts | 0.6 ms | 0.6 ms | already fine — untouched |
| Session list, 300 sessions | 0.6 ms | 0.7 ms | already fine — untouched |
| Provider list build | 0.3 ms | 0.2 ms | already fine — untouched |
| WS event codec | 5.4 µs enc / 6.4 µs dec | 5.2 µs / 6.3 µs | already fine — untouched |
| Fast-chat first event (TestClient harness) | 23.9 ms | 5.6 ms | harness-variance range; no claim made |
| Production ruff findings (engine + common) | 398 | 0 | E9-06 clean-up |
| oxlint | 0 errors | 0 errors | unchanged (122 warnings stand) |

## Decisions

1. **litellm loads lazily; startup warms it in a background
   thread.** litellm's import eagerly builds the OpenAI type tree
   (~4.3 s, ~124 MB) and was paid by every engine start although no
   provider call can happen before a key exists and a task runs.
   `litellm_provider` and `episodic` now resolve the real module on
   first use via module `__getattr__` (PEP 562), which also keeps
   the historical patch seams (`litellm_provider.litellm.*`,
   `episodic.litellm.*`) landing on the real module object; call
   sites bind it locally. The app lifespan starts a daemon warm-up
   thread so interactive first use normally pays nothing either.
   Rejected: pinning/patching litellm itself (not ours to fork) and
   a proxy object (broke `unittest.mock.patch` locality semantics —
   tried, reverted).

2. **Embedding failures trip a 60 s circuit breaker.**
   `_embed_text` is the single embedding choke point (store, query,
   backfill). Against a dead endpoint, each attempt cost LiteLLM
   retry/backoff (~1–4 s), and one query could pay it
   1 + backfill_limit times. After a connection-class failure
   (ConnectionError/TimeoutError/APIConnectionError/ConnectError),
   attempts pause for 60 s and retrieval degrades to the lexical
   leg — which returns on its own merit by design. Any success
   resets the breaker; arbitrary errors (bad request, auth) never
   trip it; the tripping failure keeps its distinct
   EPISODIC_EMBEDDING_FAILED audit entry. `aembedding` also gets
   `num_retries=0`, matching the function's documented
   single-attempt contract (the system's retry is the backfill).

3. **The 3D knowledge graph is a lazy chunk.** It is the ambient
   background, not the app shell; three.js + force-graph (~1.4 MB)
   no longer block first paint. The Suspense fallback paints the
   same background colour; the graph fades in when loaded.
   Mermaid/elk/cytoscape were already dynamic-import chunks.

4. **Lint bar: production code at zero, per-site reasons, no broad
   suppressions.** Blind-except and try-except-pass sites carry a
   `noqa` with the site's reason (best-effort probe, per-item
   isolation, fail-closed cleanup, diagnostics-recording). The two
   `__init__` re-export surfaces got `__all__` instead of import
   deletion. `tests/` debt (138 findings) is deliberately not
   churned in this epic; the commit gate cleans files as they are
   touched.

5. **Dead-code rule applied, nothing deleted.** The master
   prompt's candidates (duplicated checkpointer/fallback modules)
   were re-verified on this branch: `langgraph/checkpointer.py`,
   `routing/fallback.py`, and `mcp/fallback.py` are distinct live
   modules with real importers in engine and tests.

## Not measured here (owner's QA, by design)

Electron first paint, real streaming smoothness, and package size
on Windows — this VM cannot honestly measure them; they stay on
Arindam's QA list alongside the MCP demos.
