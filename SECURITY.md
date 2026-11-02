# Security

## Reporting a vulnerability

Open a private report through GitHub's *Security → Advisories →
Report a vulnerability* on this repository, or contact the owner via
the email on the GitHub profile. Please do not open a public issue for
an exploitable problem.

## Security model, briefly

- The engine listens on `127.0.0.1` only. The desktop app connects
  with a per-install token sent as an `Authorization` header; the
  token is held by the Electron main process and never reaches the
  renderer or any model context.
- API keys and vault credentials live in an encrypted store
  (`~/.aether/vault.db`). Agents never receive credential values:
  logins are filled into the matching site's form by the app shell,
  bound to that site, and every use is written to the audit log.
- Every tool call passes one policy and approval pipeline. Shell
  execution runs under resource caps in a worker process; payments
  always require an individual confirmation.
- Session exports and the diagnostics bundle pass through a redactor
  that strips keys and tokens.

The decisions behind this model are recorded in `docs/adr/` (notably
the vault and approval-binding records).
