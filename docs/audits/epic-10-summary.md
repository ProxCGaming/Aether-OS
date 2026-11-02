# Epic 10 — Hardening, packaging & release readiness: PR-style summary

Branch: `build/full-build` · Decisions: ADR 0037 · PRD:
`epic10-prd-and-issues.md` (goal files)

## What shipped, per issue

- **E10-01 Security sweep.** The HTTP surface is two routes:
  `GET /health` (public by design — status + schema version only)
  and `WS /ws/tasks` (token-verified, rejections audit-logged).
  Dependency sweep via pip-audit over the project environment:
  17 CVEs in 2 packages — pyasn1 upgraded 0.6.4 → 0.6.5 (4 fixed);
  filelock's 13 are **accepted with reasons** (ADR 0037: litellm
  1.106.4 pins `filelock<4`; forcing the fix downgraded litellm).
  npm audit is blocked by this environment's registry — recorded
  as unverified, not clean.
- **E10-02 Audit rotation.** `AuditLogger` rotates at 10 MB
  keeping 5 gzipped archives, logs `AUDIT_LOG_ROTATED` into the
  fresh file, and never lets a rotation failure lose a write.
  Hash-chaining deferred as a documented accepted risk.
- **E10-03 Backup / restore.** `aether_engine/backup.py`:
  manifest + sha256 per file, SQLite snapshot API for consistent
  live backups (non-SQLite `.db` falls back to raw copy), secrets
  excluded by default, restore validates everything before
  writing anything and refuses wholesale on mismatch. WS contracts
  + a Settings → Advanced card; restore answers
  `restart_required` honestly.
- **E10-04 Diagnostics + crash logs.** Redacted bundle (config
  through the audit redactor, memory as counts only, audit tail),
  downloaded from Settings. Engine crash hook (main + thread)
  and Electron main-process handlers write
  `~/.aether/logs/crash*.log`; "Open logs folder" via a new
  preload/IPC bridge.
- **E10-05 Update check.** `VERSION_CHECK` compares the app
  version with the latest GitHub release and only reports.
  No auto-update channel (none was approved).
- **E10-06 Packaging.** Version single-sourced
  (`aether_engine/version.py`, APP_VERSION 0.3.5) — FastAPI app +
  HELLO payload read it, package.json aligned (was 0.0.0), lockstep
  test added. electron-builder: `files` now includes
  `electron/**/*` (the tray/window-state/notification helpers were
  previously **missing from the packaged app** — a real defect),
  NSIS configured (per-user, directory choice), real icons
  generated (512 app icon + tray icon; the tray's empty-image
  placeholder is gone). `npm run dist` run here produced
  `Aether-OS-0.3.5.AppImage` (Linux target), and the packaged
  `app.asar` was inspected: `electron/notify.cjs`,
  `electron/windowState.cjs`, `dist/tray-icon.png`, `main.cjs`,
  `preload.cjs` all present. The `build/` icon needed a gitignore
  exception so a fresh clone can build the installer.
- **E10-07 Docs.** Root README created (clone → run → test →
  build, using the repo's actual commands); CHANGELOG gains the
  [0.3.5] full-build section; ADR 0037 records the decisions.

## Verification

- pytest: **855 passed / 13 failed / 2 skipped** — the 13 are the
  characterised environment families (engine_ws ×4,
  startup_validation ×4, providers_panel ×2, phase5_1 ×1,
  phase5_2 ×1, episodic ×1 Ollama); every Epic 10 test passes
  (rotation ×3, backup ×4 incl. WS plumbing, diagnostics
  redaction, version lockstep).
- Jest: **247/247**.
- Packaging: `npm run dist` exit 0; AppImage produced; asar
  contents verified.

## Owner-gated remainder (unchanged)

- The Windows walkthrough — install the NSIS build on a fresh
  Windows profile, onboard, uninstall — can only be honestly
  verified on Windows; it stays on the owner's QA list.
- Publishing/releasing, pushing, and merging to `main`: the
  owner's acts, per the master prompt's stop rule.
