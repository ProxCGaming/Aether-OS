"""Credential vault (Epic 8, E8-6.3) — the password-manager slice.

Two halves, deliberately separated:

* ``store`` — :class:`CredentialVault`, encrypted-at-rest entries in
  ``~/.aether/vault.db``. Passwords (and TOTP secrets) are encrypted
  through the existing ``BaseProtector`` from
  ``aether_engine.secrets.dpapi`` — the same protector that guards
  provider API keys (Windows DPAPI / OS keychain elsewhere). No
  third-party password-manager code, no hand-rolled crypto.
* ``service`` — :class:`VaultFillService`, the mediated retrieval
  path. A credential is bound to its site (scheme + registrable
  domain), travels vault → form field directly inside the browser
  session, and is NEVER returned to the model: no tool argument,
  tool result, event, or audit payload ever carries the plaintext.
"""
