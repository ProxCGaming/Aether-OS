## What this changes

<!-- One or two sentences. If this fixes a reviewed finding, cite its
ID (for example E6-F05, S2-N2) — findings are deduplicated across all
review programmes, so reference the existing entry. -->

## Tests

- [ ] Engine: `uv run pytest`
- [ ] UI: `cd desktop_app && npm test`
- [ ] New behaviour landed test-first

## Gates

- [ ] Commits are Conventional Commits (commitlint will check)
- [ ] No `--no-verify` anywhere
- [ ] If a baseline entry in `scripts/pytest-known-failures.txt` now
      passes, its line is removed in this PR and the commit says so
