# ADR 0034 — Desktop shell behaviour and first-run onboarding

Status: accepted, 31 Oct 2026. Epic 6.

## Decisions

1. **Shell lives in the main process, logic in pure modules.**
   Single-instance lock (second launch focuses the running window),
   geometry persistence to `window-state.json` in userData with
   validated bounds, a tray with Show/Hide/Quit, and an Alt+Space
   summon hotkey. The decision-bearing logic (bounds validation,
   when to fire an OS notification) lives in `desktop_app/electron/`
   as pure, Jest-tested modules; `main.cjs` only wires them.
   Notifications fire for task completion and approval-needed ONLY
   when the window is hidden or unfocused — the user looking at the
   app is never double-signalled.
2. **Onboarding is a state, not a wizard framework.** The engine
   owns one boolean (`UserConfig.onboarding_completed`,
   project-layer forbidden). The renderer shows the overlay when no
   provider has a key AND the flag is false; each step uses the
   ordinary settings contracts, so onboarding can never drift from
   Settings behaviour. Finish and Skip are the same write.
3. **The active workspace is roots[0].** Rather than a new field,
   activation reorders `workspace_roots` (every consumer — tool
   containment, skills, task defaults — already keys off the list).
   The list response reports `is_active` and an on-disk `exists`
   flag so a deleted folder reads as missing, not live.
4. **Reset means the user config, exactly.** `CONFIG_RESET_REQUEST`
   replaces `UserConfig` with defaults and saves. Keys live in the
   secret store and memory in its own stores — neither is config,
   neither is reset; the UI copy says so before the user confirms.
5. **Fresh defaults track the verified model.** `UserConfig`'s
   default model is `gemini-3.5-flash` (was 2.0 Flash), matching the
   routing default and the curated registry (ADR 0033).

## Consequences

- The owner's fresh-profile walkthrough (install → onboard → chat →
  task → approval → restart → persistence) is now executable end to
  end; geometry and config survive restart, the overlay does not
  reappear.
- Engine process lifecycle (spawning/supervising the Python engine
  from Electron) remains with the launcher and Epic 10 packaging;
  the shell connects and reconnects as before.
