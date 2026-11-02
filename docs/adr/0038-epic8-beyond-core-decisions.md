# ADR 0038 — Epic 8 beyond-core decisions

Status: accepted, 2026-11-01. Epic 8.

This is the decision record the Epic 8 PRD's pipeline required
(`docs/adr/` ADR + `docs/audits/` summary at epic close). It is
written at close, from the grill log, the refined PRD/board, the
independent Astra review of the landed code, and the fix programme
that followed — so it records the architecture **as it currently
stands**, including where a fix superseded the original landing.

## Decisions

1. **Rewind is state-only and manifest-driven.** Every task
   records a rewind manifest as it runs (step → checkpoint, label,
   file-writes so far); rewind consults the manifest, never raw
   checkpoints, and covers the last task only. Files written after
   the chosen point are listed in the confirmation as "will NOT be
   reverted" — conversation and agent state roll back, files do
   not. After a rewind, a 5-second popup offers Undo; on expiry —
   or when the user sends a new message inside the window — the
   discarded steps are **deleted outright**, with no persistent
   branch (owner-confirmed 31 Oct 2026, "Keep it as shipped";
   PRD Open Point 10 closed). Fork is the deliberate way to keep a
   line: it copies from any step, anywhere, into a new session.
   Rewind staging is a two-phase recoverable protocol, and the
   complete message representation — attachments included — is
   what rewind, Undo, and fork carry.
2. **Edit-on-approval edits arguments only, and authorization
   binds to the final arguments.** An edited tool call re-runs the
   full policy pipeline; the audit records which fields changed,
   never values. For browser actions the binding is stricter:
   classification, confirmation, and envelope consumption are
   computed from the **final post-edit, post-hook arguments**
   against a fresh page snapshot, and a material change
   re-confirms. Approval of an earlier form of an action
   authorizes nothing else.
3. **The canvas is an auto-derived artifact store, view-only.**
   Files a task writes or edits are recorded as references (never
   copies) in a real store, grouped by task, restored per session,
   and browsable in a global all-artifacts tab. The panel docks
   beside the chat and never auto-opens. The only actions are
   Copy, Save a copy (the artifact's real bytes and MIME type),
   and Open externally (the OS shell). Reports are slim: each
   task's final summary saved as a document, nothing wider.
   **Preview isolation supersedes the PRD text:** the refined PRD
   (§E8-2.2) prescribed `allow-scripts allow-same-origin`; as
   built — and as the review required — previews run without
   same-origin access to the parent, and dev-server previews load
   in a preload-less webview partition, so untrusted page content
   has no path to the Electron bridge.
4. **Workspace RAG is its own store, in two layers.** It does not
   switch when the memory backend switches. Layer one is ambient:
   a tree-sitter repo map (ranked, ~1k-token budget) injected into
   **graph tasks only** — fast chat gets no map. Layer two is
   deliberate: a `workspace_search` tool over an embedding index
   of **prose only** (code is never embedded; the map and live
   tools serve code), which **re-verifies every citation against
   the live file** before returning it. `.gitignore` is always
   respected; indexing is consent-first per workspace, then
   incremental. One local embedder serves the index and episodic
   memory (`qwen3-embedding:0.6b`; `4b` as the quality option;
   cloud opt-in only), and embeddings are stamped with model and
   dimension — rows from another model are invalidated and
   rebuilt, never silently compared across models.
5. **The media library indexes in place and never fabricates.**
   Files stay where they are; the library is an index inside the
   global Artifacts view, with user-given and generated media in
   separate collections. All media is analysed asynchronously on
   arrival — photos get a vision description and tags; videos get
   a technical probe, sampled-frame analysis (≤24 frames), a
   summary, and a transcript stage — and a transcript is always
   one of `done` / `no_audio` / `failed` / `unavailable`, never
   invented text. Recall works by description through library
   search and the `media_recall` tool, and every entry jumps to
   its exact conversation via persisted message provenance.
6. **Voice is fast chat with ears and a mouth — no third
   conversational system.** A talker layer (Silero VAD +
   endpointing) streams to the configured fast-chat model; STT
   (faster-whisper) and TTS (Kokoro, default voice `af_heart`)
   are local; the brain is the owner's configured cloud model.
   Real work escalates to the supervisor graph and runs free in
   the background; the conversation is mirrored live into chat in
   both directions, and only words actually heard are committed
   on barge-in. **Approvals are never voice-approved:**
   escalations announce approval-pending, completion, and failure
   under stable announcement identities, and the decision itself
   happens in chat. The acceptance bar is a <800 ms median
   end-to-end turn on reference hardware. **Not yet true:**
   production SmallWebRTC negotiation/transport is not landed
   (review finding M18, = EGM F-12, still open) — the cascade
   currently runs against the tested scripted transport, and
   premium speech-to-speech (E8-5.4) stays parked.
7. **The browser agent and vault enforce at the boundary, not
   in the prompt.** The agent works in a dedicated profile.
   Consequential-action classification derives from **page
   evidence only** and fails closed: unproven means confirmation
   is required under every policy mode, and a payment amount
   comes from the page, never from a selector. The confirmation
   policy is a Settings surface — per consequential category:
   Always confirm / Allow-with-envelope / Allow freely — with the
   shipped preset at Always confirm for everything; payments
   confirm individually by default and can never be enveloped
   (an explicit owner "Allow freely" for payments remains his
   Settings choice, per Grill Q32). Browser sessions are bound
   to their task identity; challenge state (CAPTCHA/2FA) is
   re-detected live before every agent action, resume is allowed
   only once the challenge is verifiably gone, and the agent
   never handles 2FA codes — it parks and hands over the browser.
   The vault rides the existing encrypted store
   (`BaseProtector`/DPAPI, `~/.aether/vault.db`); retrieval is
   mediated — a credential is bound to its registrable domain
   (public-suffix list), travels vault → form field directly,
   never enters model context, is revalidated against the live
   page at the fill moment, and every use is audit-logged.
   Driver failures are normalised to secret-free errors at the
   vault boundary.
8. **Export is redacted; import is transactional.** Every
   session export (Markdown, JSON, ZIP) passes the diagnostics
   redactor, including attachments carried in the ZIP. Import
   validates and stages first (archive CRCs verified, attachment
   paths rewritten to restored locations), then commits in a
   single database transaction and places files last; a refusal
   or a post-commit placement failure leaves no partial session.
9. **Cost and context accounting measure reality or say so.**
   The persistent ledger records **every** provider call —
   including context-less system calls and zero-cost local calls
   — priced via LiteLLM `completion_cost()` with a fallback
   table; an unpriced model records $0.00 with
   `pricing_status='unpriced'`, never a guessed figure. The
   context breakdown is measured from the real request assembly;
   a component that cannot be measured reports
   `{tokens: null, unknown: true}` instead of an estimate, and
   segment shares are computed against the model's actual
   context capacity. Budgeting is a soft daily alert only —
   never a hard stop mid-task.

## Consequences

- The epic's trust boundaries (preview isolation, browser
  authorization, vault mediation, local-only vision routing) are
  enforced in the execution paths themselves; the independent
  review's reproductions against the original landing are the
  regression evidence, and each fix landed behind the full test
  gate on the merged tree (see `docs/audits/epic-8-summary.md`).
- Carried forward as open, owner-gated items: the streaming-voice
  transport (M18/F-12) and its single-owner decision; the
  Chromium bootstrap route for fresh installs (dependency
  declared; bundle vs first-run download undecided); PRD Open
  Points 11–14 (vault unlock model, media deletion semantics,
  snip hotkey, TOTP storage); E8-5.4; Windows acceptance QA;
  push and merge.
