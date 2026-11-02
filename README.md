# Aether-OS

A local AI agent "OS": a Python engine (FastAPI + LangGraph +
LiteLLM) with a persistent knowledge graph and episodic memory,
driven by an Electron + React desktop app. Agents route through a
supervisor, every tool call passes one policy/approval pipeline,
and API keys live only in an encrypted secret store.

Current version: **0.3.5** (single source: `aether_engine/version.py`,
mirrored in `desktop_app/package.json`, `pyproject.toml` and the root
`package.json` — tests pin all four equal).

## Layout

- `aether_engine/` — the engine: orchestration (LangGraph
  supervisor), tools, MCP client, memory, providers, secrets.
- `aether_common/` — the wire contracts shared with the UI.
- `desktop_app/` — the Electron + React app (`main.cjs`,
  `preload.cjs`, `electron/` helpers, `src/`).
- `tests/` — pytest suite; `desktop_app/src/__tests__/` — Jest.
- `docs/adr/` — architecture decisions; `docs/audits/` — per-epic
  PR-style summaries; `benchmarks/` — the Epic 9 perf harness.

## Run it (development)

Prerequisites: Python 3.12 with [uv](https://docs.astral.sh/uv/),
Node 24.

```bash
# 1. Engine dependencies + engine (127.0.0.1:8000)
uv sync
uv run python run_engine.py

# 2. Desktop app (separate terminal)
cd desktop_app
npm install        # first time only; also needed once per checkout: npm ci works too
npm run electron:dev
```

Or launch just the UI with `uv run python run_ui.py` (it warns if
the engine is not up; the app retries the connection on its own).

First run walks a 3-step onboarding: add a provider API key,
pick a workspace, choose an approval default.

## Test

```bash
uv run pytest                 # engine suite
cd desktop_app && npx jest    # UI suite
```

### Coverage

Coverage is measured, not gated: there is no failing threshold yet
(S5-M11 — measurement first, so trends are visible before anyone
picks a floor).

```bash
# Engine (aether_engine): terminal report with missing lines, plus
# written reports in coverage.xml (CI/tooling) and htmlcov/ (browse
# htmlcov/index.html). Config: [tool.coverage] in pyproject.toml.
uv run pytest --cov=aether_engine --cov-report=term-missing \
  --cov-report=xml:coverage.xml --cov-report=html:htmlcov

# Desktop app (desktop_app/src): text summary plus lcov + HTML in
# desktop_app/coverage/. Config: desktop_app/jest.config.cjs.
cd desktop_app && npm run test:coverage
```

CI runs both commands on every push / PR and uploads the written
reports as build artifacts (see `.github/workflows/ci.yml`).

Commits are gated (commitlint + lint + related tests; pushes run
the full suites) — see the commit-rules notes in
`docs/audits/` and never bypass with `--no-verify`.

## Build a distributable

```bash
cd desktop_app
npm run dist     # vite build + electron-builder (NSIS on Windows)
```

Packaging proof so far is a Linux AppImage (built and inspected
during Epic 10). The Windows NSIS build and the install → onboard →
uninstall walkthrough still await the owner's Windows pass, and the
npm dependency audit is unverified, not clean — see
`docs/audits/epic-10-summary.md` and the Known limitations note in
`CHANGELOG.md`.

## Data & safety

- Everything lives in `~/.aether/` (config, memory DBs, skills,
  plugins, audit log). Settings → Advanced can back it up to a
  hash-verified zip and restore it (restart applies a restore).
- Settings → Advanced also exports a redacted diagnostics bundle
  (no API keys, no memory content) and opens the logs folder
  (engine + shell crash logs live there).
- Settings → Advanced → Check for updates only *reports* the
  latest GitHub release; nothing installs itself.
