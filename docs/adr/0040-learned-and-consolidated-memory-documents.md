# ADR 0040: Learned and Consolidated Memory Documents (E4-10)

## Status
Accepted

## Date
2026-11-02

## Context
The owner-approved plan addendum (30 Oct 2026, Epic 4 — Memory) promised
two document-shaped memory capabilities that the Epic 4 issue list never
picked up (full-product review finding E4-F09):

- **omp #29 — the `learn` loop:** explicit lessons extracted at task end
  into a deduped, redacted, capped `learned.md`, injected into the next
  session's briefing.
- **omp #33 — memory consolidation:** a periodic consolidation pass
  turning accumulated episodic/KG rows into a readable curated memory
  document (secret-redacted, token-capped), so long-term memory is
  legible, not just rows.

## Decision
1. **Documents, not stores.** Both artefacts are Markdown files in the
   data home beside the memory databases: `~/.aether/learned.md`
   (lessons) and `~/.aether/curated_memory.md` (consolidated document).
   They are curated layers above the memory stores, not new stores:
   no schema, no migration, and consolidation writes nothing back.
2. **The learned layer is backend-independent.** `learned.md` does not
   belong to any memory backend, so the facade's `learn_lessons` is a
   working default on the base `MemoryBackend` (extraction into the
   document via `memory/learned.py`) rather than a per-adapter
   operation; mem0/langmem/graphiti inherit it unchanged. Task-end call
   sites (graph completion, resume completion, fast chat) call it
   beside `learn`, under the same best-effort contract.
3. **Briefing injection surface.** Lessons are injected by
   `learned_briefing()` into the supervisor's system prompt — the
   session briefing the supervisor routes and composes specialist
   briefings from — capped at 2,000 chars, the codebase's memory
   briefing budget, cut at a lesson boundary with a truncation marker.
4. **Consolidation reads through the facade's default views** (active
   facts only, current episodes), so a superseded fact or deleted
   episode can never be resurrected into the document, and pruning /
   dedupe semantics are untouched. It runs weekly on the capability
   scheduler next to retention pruning (never at startup, same
   reasoning as pruning), with `run_memory_consolidation_now()` as the
   manual trigger.
5. **Redaction and caps reuse house rules.** Both documents are
   scrubbed with the audit module's `_scrub_string` (the redactor
   session export uses). Caps are module constants in the style of
   `MEMORY_TOOL_RESULT_MAX_CHARS`: learned.md holds at most 100
   lessons / 20,000 chars (oldest dropped first); the curated document
   is capped at 24,000 chars with an explicit truncation note. Both
   files are written atomically (temp + fsync + `os.replace`).

## Consequences
- Lessons and the curated document survive backend switches (they are
  not backend data); switching backends changes what consolidation
  renders, not the lessons file.
- The documents are user-legible and user-editable: learned.md lessons
  are exactly its `- ` bullet lines, so hand edits and deletions are
  honoured by the next read.
- Extraction costs one additional provider call per completed task
  (the same shape as fact extraction), best-effort and detached from
  task success.
