# ADR 0042: Per-Workspace Sessions

## Status
Accepted

## Date
2026-11-02

## Context
ADR 0041 item 4 records the divergence: the master prompt required
sessions scoped per workspace, Epic 6 excluded them, and sessions
shipped global with no owner-approved waiver. The owner took the
fresh decision the same day (grill, 12 questions, 2 Nov 2026;
`per-workspace-sessions-grill-20261102.md`) and directed the build.
This ADR is the decision record for that build, which landed as six
slices on `build/full-build`: store binding (`7eba58a`), canonical
workspace identity (`0f1e5ed`), socket binding (`13db655`),
per-workspace retention (`b7ae06a`), desktop list/Projects
(`7dedbb1`), and export/import provenance plus this record (final
slice).

## Decision
1. **No migration (Q1).** The owner is the only user and has no
   real sessions to migrate. The binding column starts NULL on every
   existing row; there is no backfill and no inference/suggestion
   machinery — binding happens at creation or by explicit move.
2. **Binding is the canonicalised root path (Q2, Q8).** Sessions
   carry a nullable `workspace` column (session-store schema v3)
   holding the canonical workspace root path; NULL means unbound.
   Workspace roots are canonicalised on add, and a second spelling
   of the same folder is refused with a pointer to the existing
   entry, so one folder is one workspace everywhere. A generated
   workspace id (the only rename-proof option) was considered and
   not taken: renaming a folder on disk orphans its bindings until
   the old root is removed and the sessions are moved (items 6–7).
3. **Bound at creation (Q3).** SESSION_CREATE binds to the
   then-active root; an explicit null creates an unbound session.
   The binding is fixed until moved — opening a session never
   re-files it.
4. **"No workspace" is first-class (Q4).** Unbound sessions are a
   permanent, deliberate bucket (shown as "No workspace"), not
   migration residue: users can create and keep unbound sessions.
5. **Switching filters; the open session is safe (Q5).** Switching
   the active workspace (under Projects) lists that workspace's
   sessions; clicking a title makes it the current session; the
   currently open session is never lost by browsing another
   workspace — it stays accessible from its own workspace.
6. **Moves are the correction mechanism (Q6).** A per-session move
   and a multi-select bulk move re-file sessions between workspaces
   and the unbound bucket. A move changes the binding only — it
   does not touch `updated_at`, so it is not activity for pruning.
7. **Removal unbinds, never deletes (Q7).** Removing a workspace
   root unbinds its sessions; the removal reply/confirmation states
   how many. No session or message is deleted by a root removal.
8. **Session-only export; the binding does not travel (Q9, as
   superseded by the owner's clarification).** There is no
   workspace export. The Epic 8 per-session export (E8-A7) stays
   session-only and carries the session's context (chat history,
   the work record, produced artifacts, shared attachments). The
   home workspace appears as a **provenance label only**: the
   `workspace` value rides the JSON as data, and the Markdown
   header gains one line (`Workspace: <path>` / `Workspace: No
   workspace`). An imported session **always lands unbound**,
   whatever the file says, and the import result states the outcome
   (`workspace: null` plus a `workspace_note`). The export format
   stays v1; legacy files without the key import exactly as before.
9. **Search is global (Q10).** Session search spans all workspaces;
   results are labelled with their workspace. Search never follows
   the list filter.
10. **Binding is organisation-only (Q11).** Filing, list scoping,
    creation default, and moves — nothing else. Memory,
    instructions, and tool permissions are not scoped by the
    binding; the per-workspace context that exists separately
    (project config, indexing consent) is untouched.
11. **Full per-workspace retention caps (Q12).** The config
    retention numbers (default: newest 200 sessions, 180 days by
    last activity) apply **per bucket**: each canonical root is a
    bucket of its own, and unbound sessions form the "No
    workspace" bucket with the same caps. The owner took this with
    its costs stated: the total store is bounded per bucket rather
    than globally, and a move changes which bucket counts a
    session.

## Consequences
- ADR 0041 item 4's divergence is closed: sessions are scoped per
  workspace as the master prompt required, under the semantics
  above. ADR 0041 carries a superseding pointer to this ADR.
- Counts caveat: a binding naming no configured root — reachable
  only via a restored backup, since imports land unbound — counts
  under no workspace row in WORKSPACE_LIST and not as unbound
  either. The session itself still lists normally in the global
  session list; it is simply filed nowhere the Projects view knows.
- Import is unbound by construction: the restore insert never
  writes the workspace column, so no import path (JSON or ZIP) can
  smuggle a binding in from a file.
- Export interoperability is preserved in both directions: an old
  importer ignores the JSON `workspace` key (and the extra Markdown
  header line) entirely, and the new importer treats a missing key
  exactly as it treated every pre-binding export.
