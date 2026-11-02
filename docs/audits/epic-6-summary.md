# Epic 6 — Desktop app to standard-software standard: PR-style summary

**Branch:** `build/full-build` · **Date:** 31 Oct 2026 · **Verdict:** the
shell behaves like shipped software (single instance, remembered
geometry, tray, summon hotkey, quiet notifications), first run has a
real onboarding flow, and the views that were list-only — Projects,
Dashboard, Settings — are now complete and searchable/resettable.

## What shipped

- **E6-01 Shell**: single-instance lock; validated window-geometry
  persistence; tray (Show/Hide/Quit); Alt+Space summon; OS
  notifications for task-complete / approval-needed only when the
  window is hidden or unfocused. Logic in pure modules
  (`desktop_app/electron/windowState.cjs`, `notify.cjs`), 8 Jest tests.
- **E6-02 Onboarding**: engine flag `onboarding_completed`
  (project-forbidden, CONFIG_UPDATE whitelist); three-step overlay
  (provider key → workspace → approval default) using the ordinary
  Settings contracts; skippable everywhere; shown exactly once.
- **E6-03 Palette + shortcuts**: Ctrl+K palette over actions,
  sessions, and configured models (models set the default through
  MODEL_SET_DEFAULT); Ctrl+N / Ctrl+, wired; shortcuts dialog lists
  exactly the wired set.
- **E6-04 Workspaces**: engine reports `is_active` (roots[0]) and
  `exists`; WORKSPACE_SET_ACTIVE reorders roots; Projects shows
  Active / Folder-missing states and a Set-active action.
- **E6-05 Dashboard**: Provider Health card (Epic 5 snapshot) and
  Memory card (backend + embedding stats), honest empty states.
- **E6-06 Settings search + reset**: nav filtered by label/keywords;
  CONFIG_RESET_REQUEST replaces the user config with defaults
  (audited); the reset card's copy states the exact scope — keys,
  files, and memory are not config and are not touched.
- **E6-07 Focus/a11y**: global `:focus-visible` ring; aria labels on
  icon-only shell buttons. Dark remains the single complete theme.
- Also: `UserConfig.default_model` refreshed to gemini-3.5-flash so
  fresh installs and resets start on the verified model.

## Tests

- New: shell 8, Onboarding 3, CommandPalette 4, SettingsPanelEpic6 2,
  Projects +1, Dashboard +2 (Jest); tests/test_epic6_desktop.py 3.
- Baselines after Epic 6: **pytest 837 passed / 3 failed / 2 skipped**
  (the 3 are the known timing-sensitive families: test_engine_ws,
  test_startup_validation); **Jest 243/243** on a calm full run (a
  contended run flaked one App smoke test; it passes standalone and
  in the clean full run).

## Records

- ADR 0034 (shell behaviour + onboarding/reset semantics).
- PRD + progress: `workspace/goals/aether-os-desktop-app-build/files/epic6-prd-and-issues.md`.

## Not in this epic (by design)

Engine process spawning/supervision from Electron (launcher +
Epic 10 packaging); per-workspace sessions (data-model change);
a second theme; the owner's fresh-profile walkthrough itself.
