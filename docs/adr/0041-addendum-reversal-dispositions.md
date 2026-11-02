# ADR 0041: Dispositions for the Plan-Addendum Reversals

## Status
Accepted

## Date
2026-11-02

## Context
The owner-approved plan addendum (`aether-os-plan-addendum-gaps.md`,
30 Oct 2026) assigned scope to Epics 4–7 that the later epic plans
then omitted, excluded, or deferred without recording a disposition
(full-product review finding DOC-04). "Epic complete" therefore did
not trace to the same scope across the programme. This ADR records
what actually happened to each reversed item. It decides nothing
new: where an item was built later, the landing and its own record
are cited; where it was not built, this record states that plainly.

## Decision
No new scope is decided here. The dispositions of record are:

1. **Lessons + consolidation documents (addendum → Epic 4).** The
   addendum mandated task-end lessons (`learned.md`) and periodic
   curated consolidation; the Epic 4 plan omitted both and the epic
   closed without them. **Disposition: built after the epic** in the
   full-review fix programme — `memory/learned.py` +
   `memory/consolidation.py`, landed as E4-F09 (`07e177b`); the
   design record is ADR 0040. Closed.
2. **Fallback-chain configuration + latency-aware routing
   (addendum → Epic 5).** The Epic 5 plan excluded the addendum's
   configurable fallback chains and latency items; the epic shipped
   fallback *enforcement* (ADR 0033) but not the chain configuration
   or the latency routing. **Disposition: built after the epic** —
   one configured fallback-chain authority consulted at both
   enforcement points (E5-F02, `a312ce0`) and a per-model latency
   store whose measurements steer routing within a priority tier
   (E5-F11, `39b1373`). Closed.
3. **Skill namespacing, precedence + registry CLI/lockfile
   (addendum → Epic 7).** The Epic 7 plan deferred both; the epic
   shipped neither. **Disposition: built after the epic** —
   capability ownership, `<ns>/<name>` addressing, and
   priority-honouring discovery (E7-F08, `1decb83`), and the
   `aether-skills` CLI (install/search/list) over `skills.lock.json`
   (E7-F09, `68328f9`). One boundary stands from that landing: pins
   may name remote (GitHub) sources, but resolving them is not
   built — it fails loudly until a registry endpoint is defined.
   Closed with that boundary.
4. **Per-workspace sessions (master prompt → Epic 6).** The master
   prompt required sessions scoped per workspace; the Epic 6 plan
   excluded them and the epic summary records the exclusion
   ("Not in this epic (by design)"). Sessions shipped global and
   remain global. **Disposition: not built.** No owner-approved
   waiver and no superseding decision is on record; this entry
   documents the divergence rather than resolving it. Building
   per-workspace sessions remains a fresh decision plus a
   data-model change.
5. **Handed-off Settings suite (Epics 1–2 → Epic 6).** The Epic 1
   and 2 summaries handed a suite of Settings editors to Epic 6;
   the Epic 6 close-out did not mention them and they were not
   built in the epic. **Disposition: built after the epic** —
   strict CONFIG_UPDATE validation plus Settings surfaces for the
   handed-off fields, and the project-config layer surface
   (E6-F05, `7a04a3e`). Closed.

## Consequences
- Addendum scope is now traceable end-to-end: every reversal above
  has a disposition of record, and "complete" claims for these
  epics should be read with this ADR.
- Item 4 is the only open divergence: the product's global
  sessions contradict the master prompt's per-workspace
  requirement until a future decision changes one or the other.
