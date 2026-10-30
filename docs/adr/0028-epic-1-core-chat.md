# ADR 0028 — Epic 1 Core Chat & Orchestration Decisions

- Status: Accepted
- Date: 2026-10-30
- Branch: `build/full-build`
- Context: Epic 1 rebuilt the chat and orchestration core on the
  reconciled staging branch (Epic 0, ADR 0026) around the
  `create_supervisor()` migration (ADR 0027). This record captures the
  epic-level decisions that individual slice commits reference, so a
  future reader does not have to reconstruct them from commit bodies.
  Baseline at close: pytest 664 passed / 14 known pre-existing
  failures (plus one documented full-suite flake) / 2 skipped;
  Jest 191/191.

## Decisions

### D1. Two entry paths, one history (E1-04)
Plain conversation takes a fast-chat path (no supervisor); anything
classified as work takes the graph. The classifier defaults to WORK
when uncertain, and slash commands, skills and forced agents always
take the graph. Both paths write to the same session store, and both
resolve history through one shared shaper
(`session_store.get_chat_history`). Fast chat deliberately does not
write episodic/KG memory yet — flagged for Epic 4 parity.

### D2. Identity is client-generated and shared (edit-and-resend)
A user message has ONE id, minted by the client and used by the
rendered bubble and the stored row alike. Truncation addresses that
id, falls back to an occurrence-aware content match for messages
whose UI id is not a row id, and invalidates the session's graph
checkpoint; the next run re-seeds a fresh thread from the retained
store history. The store, not the checkpoint, is the source of truth
for what the user sees.

### D3. Context is a view, never a rewrite (E1-06)
Compaction folds older turns into `context_summary` at run assembly;
the messages channel keeps the full history. Every model in the run
reads the same summary-plus-tail view (`model_facing_messages`). The
summariser is the `compression` auxiliary model with the run provider
as fallback.

### D4. Steering is segment continuation (E1-07)
A message sent mid-task is queued and applied at a step boundary by
continuing the same checkpoint thread with the steers appended. A
`Command` update cannot deliver a message to a paused specialist (it
resumes from its own subgraph checkpoint), so approval-pause steers
are applied as a fresh segment after the resume settles, and
re-queued untouched if the task pauses again.

### D5. Enforcement by absence, backstopped centrally (E1-09, E1-11)
Plan mode and the spawn policy both work by construction first:
plan mode offers only READ_ONLY tools; the supervisor prompt does not
offer disallowed specialists. Central backstops (execute_tool's
plan-mode refusal, the post-model hook's denial) exist for forged or
stale state, and a denied delegation ends the run with the reason as
its answer — nothing is silently dropped.

### D6. Auxiliary models are real seams or nothing (E1-06/08/12)
`vision`, `compression`, `advisor` and `title` are the only auxiliary
task classes, each with a real engine call site; the Phase 10.3
placebo contract prunes anything else. Advisor and title are
best-effort decorations: unconfigured or failing means no effect,
never a failed task.

### D7. Config resolves in layers, safety stays global (E1-14)
Global → workspace project file → per-task overlay, resolved per run
without mutating the global config. A project file may not set
`tool_policies` or `workspace_roots`: a cloned workspace must never
loosen the user's Deny policies or redefine its own containment.

### D8. Model streams stay separated by field (E1-16, E1-17)
Thinking travels on `StreamChunk.thinking` / THINKING events, never
inside answer text, and is never persisted as the reply. Scratchpad
content is sanitised in the engine before publication and is
display-only; it cannot execute or feed back into a run.
