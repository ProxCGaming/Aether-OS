# Contributing

Aether-OS is developed by its owner with AI assistance. The rules below
are enforced by git hooks — they are not advisory.

## Commit rules

- Conventional Commits, enforced by commitlint: one of
  `feat fix docs style refactor perf test build ci chore`, subject at
  least 20 characters, starting lowercase.
- Never commit or push with `--no-verify`. For anyone.
- Pre-commit runs, on staged files: Ruff (Python), oxlint (JS),
  related Jest tests, a Gitleaks secret scan, and a check that every
  staged React component has a test.
- Pre-push runs the full pytest and Jest suites under a subset rule:
  a push is blocked only by a failure that is *not* listed in
  `scripts/pytest-known-failures.txt`. A listed test that starts
  passing is reported, never a block.
- Python dependencies are managed with `uv` only. Upgrades are manual
  and deliberate; there is no Dependabot.
- Files over 10 MB are blocked.
- `main` is lightly protected: pull requests are optional, force-push
  and deletion are blocked.

## Tests

- Engine: `uv run pytest`. UI: `cd desktop_app && npm test`.
- New behaviour lands test-first. A fix for a reviewed finding cites
  the finding's ID in its commit message.

## Findings and reviews

Findings from any review (Astra, EGM, or manual) are deduplicated
across all review programmes before work starts: reference the
existing ID instead of opening a second entry for the same issue.
Architecture decisions are recorded in `docs/adr/`; per-epic build
summaries live in `docs/audits/`.
