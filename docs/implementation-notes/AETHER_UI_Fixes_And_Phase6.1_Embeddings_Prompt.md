# Task: UI Fixes + Phase 6.1 (Local Embeddings, Hybrid Retrieval)

Two parts in one prompt. Do Part A first — it's fixing things confirmed
broken, not building anything new. Part B is genuinely new work and
depends on nothing in Part A, but do it second regardless, since fixing
known-broken things takes priority over adding capability.

---

# PART A: Fix the confirmed fake/broken UI

Everything below was verified by direct code inspection, not reported
secondhand — these are real, not suspected.

## A1. Dashboard.jsx — currently 100% hardcoded
Every metric ("Online", "0" active tasks, "3 Active" plugins, "128 MB"
memory) is a literal JSX string. Zero state, zero props, zero WS calls.
Wire each to something real:
- Engine Status → derive from actual WS connection state
- Active Tasks → a real count from engine state
- Plugins → real count of installed/enabled plugins (the Plugins tab
  already fetches this correctly — reuse that pattern/endpoint)
- Memory Usage → a real reported metric, or remove the card entirely if
  nothing real backs it yet. Do not leave a fabricated number in a card
  that looks live.

## A2. Projects.jsx — currently 100% fake
`handleAddWorkspace`'s own comment admits it: *"In a real app, this
would use `window.electronAPI.showOpenDialog()`."* Currently it appends
a fake entry with the literal string `/path/to/project`. Fix:
- Real folder picker via `window.electronAPI.showOpenDialog()` (the
  same pattern `LocalModelsTab.jsx`'s `handleBrowseDir` already uses
  correctly — copy that, don't reinvent it)
- Real backend persistence — this likely maps to `workspace_roots` in
  existing config; check `aether_engine/app.py` and
  `settings_window.py`'s workspace handling before building a new
  mechanism
- Remove the "Indexed" badge unless there's a real indexing status to
  reflect — currently it's shown unconditionally regardless of any
  actual indexing having happened

## A3. LocalModelsTab.jsx — three separate issues
1. **Download-directory picker is disconnected from the actual
   download.** `handleBrowseDir` saves a real path to `localStorage`,
   but `handleDownload`'s WS payload (`{ repo_id: modelName }`) never
   includes it. Add `destination_dir` to the payload, read from the
   same `localStorage` key, and confirm the backend's
   `LOCAL_MODEL_DOWNLOAD_START` handler actually uses it (check
   `app.py` — it may need updating too, this isn't necessarily
   frontend-only).
2. **"Search" doesn't search online.** `catalog` is a hardcoded 6-model
   array; the search box only filters it client-side. Implement a real
   online catalog lookup — the Ollama library has a browsable model
   list; check if it exposes anything fetchable, or use a static but
   *regularly refreshed* curated list pulled from a real source at
   build/startup time rather than hand-typed into the component. If a
   genuinely live "currently available online" search isn't feasible
   this round, don't fake it — relabel the input honestly (e.g. "Filter
   curated list") rather than implying real-time online discovery.
3. **Pause/cancel buttons during download are explicitly fake** (the
   code's own comment says so). Either wire them to a real
   pause/cancel WS message, or remove them — don't leave a button that
   looks actionable and isn't.

## A4. Remove the Insights tab
Still present in `App.jsx`'s tab-state list
(`'Chat', 'Dashboard', 'Projects', 'Insights', 'Settings'`). Remove it
from the nav and the state enum — not required, per direct instruction.

## A5. Finish the remaining dead dropdowns
Two of the three previously-found `onChange={() => {}}` dropdowns in
`SettingsPanel.jsx` are still non-functional (Reasoning Effort selector,
per-node Agents model override). Wire both to real state with real WS
persistence, matching how the per-task-class dropdown's options list
already correctly pulls from live `providers` state — reuse that
pattern for the per-node override rather than the hardcoded 3-model
list currently there.

## Part A acceptance — real evidence per item
For each of A1–A5: show the actual before/after behavior, not a
description. For A1/A2 specifically, restart the app and confirm the
data/workspace persisted — not just that it displayed correctly once.

---

# PART B: Local Embeddings + Hybrid Retrieval for Episodic Memory

## Context
Episodic memory (Tier 2, `~/.aether/memory.db`) currently uses SQLite
FTS5/BM25 — lexical/keyword matching only. This was a deliberate,
documented choice (ADR 0017) to avoid heavy ML dependencies. This phase
adds semantic recall *alongside* that, not instead of it — hybrid
retrieval, not a replacement.

## Required approach

**Embedding model:** served locally via Ollama, the same way local LLM
inference already works — no new infrastructure category, just another
Ollama-served model. Default to `nomic-embed-text` unless you have a
concrete reason to pick differently (document the reason if you do).
Do not use a cloud embedding API — that breaks local-first for zero
benefit here.

**Routing — hard rule, no exceptions:** the embedding call goes through
`litellm.aembedding()`, exactly like every other LLM call in this
project goes through the LiteLLM-routed provider abstraction. Do not
call Ollama's embedding endpoint directly.

**Storage:** add an embedding column/table to the existing
`~/.aether/memory.db` episodic schema — store as a blob (serialized
float array). Given the realistic scale of a single user's episodic
memory (hundreds to low thousands of records, not millions), brute-force
cosine similarity in Python at query time is almost certainly sufficient
— don't add a vector-index dependency (sqlite-vec, faiss, etc.) unless
you first confirm brute-force is actually too slow at realistic scale.
State which you chose and why in the ADR.

**Write path:** when `store_episode()` runs, also embed the task summary
and store the vector alongside the existing row.

**Read path — hybrid, not a replacement:** `query_episodes()` currently
does BM25-only lookup. Keep it. Add a second signal — cosine similarity
against the query's embedding — and merge the two ranked lists (simple
weighted merge or reciprocal rank fusion, your call, document which and
why). The Supervisor's existing call site doesn't need to change, just
what happens inside `query_episodes()`.

**Scope — episodic memory only, not the knowledge graph this round.**
Tier 3's fact extraction is a different problem (structured accuracy,
not semantic recall) — don't expand into it without being asked.

## Required process
Design summary before code — specifically: confirm the embedding model
choice, confirm brute-force vs indexed similarity search with reasoning,
before implementing either.

**Write one ADR: `ADR/0020-hybrid-retrieval-local-embeddings.md`.**
Cover: model choice and why, storage approach and why brute-force was
or wasn't sufficient, the merge strategy for BM25+vector results, and
explicit confirmation the embedding call routes through
`litellm.aembedding()`.

## Acceptance — real evidence, the kind that actually tests this
The only test that matters here: **a query that succeeds semantically
where BM25 alone would fail.** Store an episode about something
described one way, then query using clearly different wording with
near-zero keyword overlap, and paste the actual retrieved result showing
it was found. Also paste evidence the embedding model runs locally (no
outbound network call during embedding).

---

## When you're done
Report Part A and Part B separately, each with their own real evidence.
Don't start anything beyond what's listed here without being asked.
