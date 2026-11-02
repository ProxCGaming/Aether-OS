# ADR 0037 — Epic 10 hardening decisions

Status: accepted, 2026-10-31.

## Audit log: rotation now, hash-chain deferred (accepted risk)

`AuditLogger` rotates at 10 MB keeping 5 gzipped archives, and the
fresh log records `AUDIT_LOG_ROTATED`. Hash-chaining entries was
considered and deferred: the audit log is a single-user local
debugging/evidence trail, not a compliance log; tamper-evidence
against an attacker who already owns the user's account buys
little, and chaining would complicate every writer. Revisit if
Aether ever gains multi-user or shared-machine deployments.

## Dependency sweep: pyasn1 fixed, filelock accepted

pip-audit over the project environment found 17 CVEs in 2 packages.

- **pyasn1 0.6.4 → 0.6.5** — upgraded; its 4 CVEs are gone.
- **filelock 3.32.7 (13 CVEs, fixed in 4.2.1) — accepted, cannot
  move.** litellm 1.106.4 (the provider layer the whole app rides
  on) declares `filelock>=3.16.1,<4.0`. Forcing filelock ≥ 4.2.1
  via a uv constraint made the resolver *downgrade litellm to
  1.103.10* — trading a current, tested provider layer for an
  older one to patch a file-locking utility. The filelock findings
  are local symlink/TOCTOU races in cache-lock paths
  (huggingface-hub/litellm model caches in the user's own
  directories): exploitation needs a local attacker already able
  to write to those paths. Accepted for this single-user desktop
  app; re-run the sweep whenever litellm relaxes the pin.
- **npm audit could not run** — this environment's registry
  refuses audit queries (also via the public registry flag).
  Recorded as *unverified*, not clean; the JS dependency review
  rides on the Jest suite + the lockfile until an audit can run.

## Backup scope: secrets excluded by default

Backups include config, memory/session DBs, MCP config, skills,
plugins and logs, with sha256-verified manifests and consistent
SQLite snapshots. `secrets.db` is excluded unless the user opts in
— keys are re-enterable, and the store is ciphertext at rest, so
including it never exposes plaintext but does spread ciphertext
around. Restore validates everything before writing anything and
answers `restart_required` honestly (no live state reload exists).

## Updates: report-only check, no auto-update channel

No update channel was approved, so per the master prompt there is
no auto-updater. Settings → Advanced → "Check for updates"
compares the app version with the latest GitHub release and only
reports; downloads/installs are the user's act.

## Version: single source

`aether_engine/version.py` `APP_VERSION` is the source of truth;
the FastAPI app and the websocket HELLO read it (both previously
hardcoded "0.3.5"), `desktop_app/package.json` carries the same
value for electron-builder, and a test pins the two equal.
