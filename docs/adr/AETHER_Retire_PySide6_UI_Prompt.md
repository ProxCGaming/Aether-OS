# Task: Actually Retire `aether_ui/` — Last Attempt Didn't

## Context
The commit `6b030ec "chore: Deprecate old Python-based UI components"`
didn't deprecate anything — it added 374 lines and created two new
files (`chat_bubble.py`, `sessions_drawer.py`) inside `aether_ui/`. The
full switch to `desktop_app/` (Electron/React) has already been decided
(see ADR 0019) — this task is to actually implement that decision, not
re-litigate it.

## What to do
1. **Stop adding features to `aether_ui/`.** No exceptions, starting
   now.
2. **Before archiving anything, check the parity table in ADR 0019.**
   Two real gaps still exist only in `aether_ui/` and not yet in
   `desktop_app/`: Advanced/Security settings, and Skills (the honest
   empty-state version, not the current Electron toggle). Confirm
   whether either is still needed for a working app right now — if
   Security settings (DPAPI/Job Object visibility) are load-bearing for
   current use, port them to Electron first, don't leave a functional
   gap.
3. **Once nothing depends on it being live:** move `aether_ui/` to
   `legacy/aether_ui/` (or equivalent — your call on exact location,
   just make it unambiguous this isn't the active frontend), and add a
   one-line `README.md` at its root: retired as of ADR 0019, superseded
   by `desktop_app/`, kept for reference only.
4. **Do not delete it outright.** It has real, working reference
   implementations (the Tools/MCP/Plugins logic that Electron's version
   was ported from) — those are worth keeping around as reference until
   the port is fully done and verified, per ADR 0019's tracked gaps.

## What NOT to do
- Don't quietly keep extending it "just this once" for a small fix —
  that's exactly the pattern that produced the mislabeled commit.
- Don't archive it before the Advanced/Security gap is resolved one way
  or another (ported, or explicitly decided as acceptable to drop).

## When you're done
Confirm: what (if anything) got ported to Electron first, where
`aether_ui/` ended up, and that the README notice is in place. This
should be a small, quick task — if it's turning into a large one,
something in step 2 needs a real decision from the product owner first,
so stop and ask rather than guess.
