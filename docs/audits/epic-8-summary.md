# Epic 8 — Beyond-core capabilities: PR-style summary

Branch: `build/full-build` · Decisions: ADR 0038 · PRD:
`epic8-prd-refined.md` (goal files) · Board:
`epic8-kanban-board-refined.md` (goal files)

Build landed 31 Oct 2026: **25 of 26 board issues** (22 PRD issues,
D1–D4 splits), E8-5.4 parked. The branch was pushed once, at
`0f41728`, with the owner's approval, for Windows QA; every commit
since is local-only, and `main` is untouched. What the independent
review then found — and what the fix programme did about it — is
the second half of this summary.

## What shipped, per issue

- **E8-1.1a Rewind manifest** (`1efe378`). Each task records its
  rewind points (step → checkpoint, label, file-writes so far) as
  it runs; the step picker and per-step menus surface exactly
  those points.
- **E8-1.1b Rewind execution** (`947b5bf`). State-only rollback
  with the not-reverted file list in the confirm modal, the
  5-second Undo popup, and outright deletion of discarded steps
  on expiry or on a new message (owner-confirmed as shipped,
  31 Oct 2026).
- **E8-1.2 Fork** (`c0eaa1d`). Fork from any completed step into
  a new session ("Fork of <title>"), non-destructive.
- **E8-1.3 Edit-on-approval** (`c82e405`). Arguments-only edits
  at the approval pause, re-run through the full policy
  pipeline; audit records changed field names, never values.
- **E8-2.1 Artifact store + canvas** (`ca252b0`). Files agents
  write/edit are auto-recorded as references in a real store and
  rendered (code, Markdown, diffs, data, images) in a docked
  per-session panel that never auto-opens.
- **E8-2.2 Project preview** (`8dfa444`). Preview tab for web
  artifacts: built file or detected dev server (ports
  5173/3000/8000/8080 + `package.json` scripts) in a sandboxed
  frame. (Its isolation was later rebuilt — M01 below.)
- **E8-2.3 Global all-artifacts tab** (`0c48c05`). Every
  session's artifacts in one searchable view, with navigation
  back to the owning session.
- **E8-2.4 Summary documents** (`ba7a257`). Each task's final
  summary saved as a rendered document in its artifact group —
  the slim reports scope the owner settled on.
- **E8-3.1 Repo map** (`0d3f5b9`). Tree-sitter symbol map,
  PageRank-ranked to a ~1k-token budget, injected into graph
  tasks only.
- **E8-3.2 Prose index + `workspace_search`** (`a239780`).
  Prose-only embedding index on the unified local embedder;
  the search tool re-verifies every citation against the live
  file before returning it.
- **E8-3.3 Indexing controls** (`902a4c7`). Settings surface for
  consent, exclusions, and index state per workspace.
- **E8-4.1 Snip flow** (`f49c328`). Region capture → attachment
  on a normal message, processed by the configured aux-vision
  model.
- **E8-4.2 Media index + Library** (`863927c`). All chat media
  indexed in place; Library section inside the global Artifacts
  view, user-given and generated collections separate.
- **E8-4.3 Analysis pipeline + `media_recall`** (`3a17ca1`).
  Async on-arrival analysis (vision description/tags; probe +
  sampled frames for video) with embedded metadata, recallable
  from the Library and by agents.
- **E8-4.4 Video transcripts** (`b858b30`). Transcript stage
  riding the local STT runner, with honest states
  (`done`/`no_audio`/`failed`/`unavailable`) and spoken-phrase
  recall.
- **E8-5.1a Streaming voice core** (`76a4c05`). The talker
  cascade: Silero VAD + endpointing, local STT/TTS, fast chat as
  the brain interface, live two-way mirroring into chat.
- **E8-5.1b Barge-in + escalation** (`e040942`). Playback stops
  at interruption and only heard words are committed; real work
  escalates to the supervisor and runs free in the background.
- **E8-5.2 Endpointing tuning + latency bench** (`2a3fc8a`).
  Voice Tuning sliders in Settings and the per-stage bench
  against the <800 ms median-turn bar (shortfalls logged with
  stage attribution).
- **E8-5.3 Push-to-talk + read-aloud** (`927cfa2`). Composer
  dictation as editable, never-auto-sent text; any message
  read aloud via local Kokoro.
- **E8-5.4 Premium speech-to-speech — PARKED.** No commit.
  Needs an owner directive plus its own mini-grill.
- **E8-6.1 Browser core** (`872336d`). Dedicated-profile,
  visible, policy-checked browser with registered tools and
  pause / take-control / stop on the task card.
- **E8-6.2 Confirmation policy + envelopes** (`6a7dde7`).
  Per-category Settings policy surface (Always confirm /
  Allow-with-envelope / Allow freely), shipped preset all
  Always confirm, per-task envelopes for non-payment
  categories.
- **E8-6.3 Credential vault** (`e728ad8`). Encrypted store on
  the existing `BaseProtector`; mediated, site-bound fill that
  never exposes a password to the model; full audit trail.
- **E8-A7.1 Session export/import** (`e6e2dd1`). Per-session
  Markdown + JSON export (attachments by name or ZIP), every
  export through the diagnostics redactor; import recreates a
  living session.
- **E8-A8.1a Cost ledger + header cost** (`eaa23a1`).
  Persistent per-call ledger (local calls at $0.00) and the
  live per-session cost surface in the header.
- **E8-A8.1b Context breakdown + Settings Usage** (`ed44211`).
  Segmented context bar with itemised breakdown, per-model
  context in Settings → Usage, and the optional daily soft
  budget alert.
- Associated: `69d3b00` restored a conditional close dropped in
  the A8.1b rebase resolution; `f773415` refreshed the pytest
  baseline for tests fixed along the way; `0f41728` declared
  the voice runtime dependencies (review follow-up (g)).

## The review, and the fix programme

An independent review (GPT-6 Astra via AgentRouter in OpenCode,
reading the repo directly) audited the landed build at
`2a3fc8a` and returned **NOT ready: 20 MAJOR + 15 MINOR**, every
finding reproduced with harnesses. A separate verification pass
independently confirmed **35 of 35**. Two fix programmes then
worked the findings under one standing rule — **no duplicated
issues anywhere**: the Astra queue (this summary's M/N items)
and the EGM blocker queue (F-items) cross-reference instead of
duplicating; M13 *is* EGM F-07, and M18 *is* EGM F-12.

**MAJORs — all with decided fixes have landed:**

- M01 preview bridge escape → `f7fe200` (sandbox without
  same-origin; preload-less webview partition for servers).
- M02 click classification trusted selectors → `9048e8f`
  (page evidence only, fail-closed; amount from the page).
- M03 approval not bound to final args → `ee128d4`
  (reauthorize post-edit/post-hook against a fresh snapshot).
- M04 browser/task identity disconnect → `74a9277` (sessions
  bound to task identity; fail-closed without it).
- M05 challenge gaps on typing/resume → `8252149` (live
  re-detection before all agent actions; verified resume).
- M06 vault domain conflation → `71594ce` (public-suffix
  registrable domains).
- M07 redirect during vault approval → `01890b3` (live page
  revalidated at the fill moment).
- M08 vault plaintext via exception text → `c814b18`
  (driver errors normalised secret-free at the boundary).
- M09+M10+M11 rewind attachment loss / rowid collisions /
  split staging → `29e1ed1` (attachments carried, fresh rowids,
  two-phase recoverable staging with persisted expiry).
- M12 no exact step picker → `794e0a0` (manifest-anchored
  picker; authoritative retained messages rendered).
- M13 ZIP exports bypass the redactor → `a59b59f` (= EGM F-07).
- M14+M15 import integrity → `f36c83c` (staged, CRC-verified,
  single-transaction import with compensation).
- M16 graph tasks drop image attachments → `6d10bc0` (shared
  multimodal content builder on graph-init and seeded history).
- M17 local vision silently falls back to cloud → `bfada07`
  (`local_only` routing constraint threaded through fallbacks;
  honest failure instead of an upload).
- M19 ledger drops calls → `8bd1c26` (every call recorded,
  context-less as `system`, no double counting).
- M20 invented context numbers → `8e8b95b` (breakdown measured
  from the real assembly; unmeasurable = `unknown`).
- **M18 streaming-voice RTC transport — still open** (= EGM
  F-12): negotiation endpoint and transport never landed; the
  single remaining build item, awaiting the owner's
  single-owner call between the two programmes.

**MINORs — the buildable queue is drained:**

- N01 rewind warning missed repeat writes → `0bca507` (per-step
  write events; the confirm modal's own identical subtraction
  fixed in the same slice).
- N02+N03 embedding/config integrity → `848f3d0` (embeddings
  stamped model + dimension, mismatches invalidated/rebuilt;
  `repo_map_token_budget` round-trips, 0 stays 0).
- N04+N06 snip/canvas honesty → `5ee6fcc` (display/scale-aware
  capture, honest failure instead of a synthetic image; real
  save/open actions; docked panel).
- N05 shell-created files missed by collection → `109d8f0`
  (filesystem-effect diffing scoped to the containment roots).
- N07+N08+N11 Library wiring → `2f16b37` (analysis events and
  backend recall reach the Library; exact-message jump via
  persisted provenance; envelope grants on compact cards).
- N10 silent voice escalations → `4c597b7` (approval/completion/
  failure announcements with stable identities; never an
  approval channel).
- N12 test order-dependence → `3380344` (shared-state leaks
  scoped/restored; both execution orders green).
- N13 browser runtime undeclared → `e740817` (dependency half:
  `playwright>=1.64.1` declared via uv). The Chromium bootstrap
  route — bundle vs first-run download — is an owner decision,
  still open.
- N14 context surfaces → data half in M20 (`8e8b95b`), UI half
  `a1bb141` (capacity-relative shares; the fabricated
  1,048,576-token denominator removed; Settings per-model view).
- N15 decision/closure records → ADR 0038 + this summary, with
  the PRD/board wording aligned (Open Point 10 closed as
  owner-confirmed; the sequential-build sentence replaced with
  the parallel-builders / single-merger discipline as actually
  run; the E8-5.2 board card now states the <800 ms median bar).
- N09 (voice transport proof) rides M18 and closes with it.

From the EGM queue, F-13, F-14, and F-15 also landed as
test-only pins on this branch (`fd0dc90`, `d35930d`, `fa25e80`);
its Wave-4 remainder had not landed at this writing. Builder
notes recorded for the consolidated board: steering messages
drop attachments (a distinct seam from M16, cross-referenced
to it); shell-created media reaches the canvas but is not
auto-registered as generated media; media from a steered
continuation attributes to the turn's originating message.

## Verification

- Landing discipline for every slice, build and fix alike:
  isolated worktree → diff review → commit under the repo's
  hooks (never `--no-verify`) → rebase onto the current tip →
  fast-forward merge → **full gate (pytest + Jest) on the
  merged tree**, with failures diffed against the 13-entry
  known-failures baseline (`scripts/pytest-known-failures.txt`;
  the F-18 subset rule: only failures outside the baseline
  block).
- Last full gate (at `e740817`): pytest **1502 passed / 13
  failed — exactly the baseline / 2 skipped**; Jest **486/486
  (69 suites)**. The tip has since gained only the test-only
  F-15 pin (`fa25e80`).
- Known load flakes (`test_mcp_auto_connect`, voice bench /
  session tests) flipped in some gates under parallel load and
  passed standalone every time; N12's genuine order-dependence
  was a defect and is fixed, not flaked.

## Owner-gated remainder (unchanged)

- **Windows acceptance QA** — including the N04 snip proof on a
  real multi-monitor, mixed-DPI desk, DPAPI behaviour, browser
  handoff, and audio. Nothing here substitutes for it.
- **Push of the fix tip** needs a fresh GitHub device-flow
  approval; **merge to `main`** is the owner's last-step call.
- **M18/F-12 single-owner decision** (which programme builds
  the voice transport), the **Chromium bootstrap route**
  (N13), and **PRD Open Points 11–14** (vault unlock model,
  media deletion semantics, snip hotkey, TOTP storage).
- **E8-5.4** stays parked until the owner un-parks it.
