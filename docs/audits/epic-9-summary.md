# Epic 9 — Optimisation pass: summary

Closed 31 Oct 2026. Branch `build/full-build`; nothing pushed,
`main` untouched. ADR: 0036 (decisions + before/after table).
Harness: `benchmarks/epic9_bench.py` — baselines first, optimised
only what the numbers indicted.

## What the numbers indicted, and what changed

- **Startup (E9-02).** Importing the engine cost 5,583 ms; 4,274 ms
  of it was litellm, imported at module level by the provider and
  the episodic store. Now lazy (PEP 562 module `__getattr__`, patch
  seams preserved) + a background warm-up thread in the lifespan.
  Import 1,138 ms; RSS after init 206 → 82 MB.
- **Runtime (E9-03).** With the embedding endpoint down, the
  episodic hybrid query paid ~105 ms per query and ~5,020 ms on the
  first query over unembedded episodes (one backoff-laden LiteLLM
  attempt per backfill episode). A 60 s circuit breaker on the
  single embedding choke point + `num_retries=0` (the documented
  single-attempt contract): steady-state query 5.6 ms, first
  degraded query pays one attempt per cooldown window. Session
  store, KG search, provider list, and the event codec measured
  healthy (sub-ms to µs) and were left alone.
- **Bundle (E9-04).** Entry chunk 1,946 kB → 537 kB (gzip 524 →
  149 kB) by lazy-loading the 3D graph background; mermaid/elk
  chunks were already on-demand.
- **Reliability drills (E9-05).** Reconnect storm (20 cycles →
  engine still answers) and locked session DB (bounded readable
  failure inside sqlite's busy timeout, writes recover) are pinned
  in `tests/test_epic9_drills.py`. Provider-outage behaviour cites
  the Epic 5 drills.
- **Code health (E9-06).** Production ruff 398 → 0 (oxlint already
  0 errors). Hand fixes inside the sweep: `__all__` on both
  re-export surfaces, a B023 loop-closure bound properly, dead
  `prev_status` captures removed, a dangling `DiscoveryError`
  forward reference given its real import, and per-site noqa
  reasons on all 29 blind-catch sites. Dead-code candidates
  re-verified — all three checkpointer/fallback modules are live;
  nothing deleted.

## Verification

- Epic tests: `tests/test_epic9_perf.py` (breaker trips/skips,
  non-connection errors ignored, success resets, litellm stays
  unloaded on import) + `tests/test_epic9_drills.py` 2/2.
- Full suites: pytest **846 passed / 13 failed / 2 skipped** — the
  13 are the characterised pre-existing environment families
  (identical set to the Epic 7 run); the one Epic-9-attributable
  failure the first run exposed (a Phase 10.1 timeout test missing
  breaker isolation) was fixed and re-verified. Jest **247/247**.
- Bench after-state (medians): import 1,138 ms · init 26 ms · RSS
  82 MB · episodic hybrid 5.6 ms · KG 0.6 ms · session list 0.7 ms
  · provider list 0.2 ms · codec ~5–6 µs.
