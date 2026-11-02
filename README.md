# Aether-OS

A desktop app that runs AI agents on your own machine. A Python engine
(FastAPI + LangGraph + LiteLLM) does the work — a supervisor routes each
task to a specialist agent — and an Electron + React app is the window
onto it. Every tool call passes one policy and approval pipeline before
it runs, and API keys live only in an encrypted secret store.

**Version 0.3.5** · [Changelog](CHANGELOG.md) ·
[Architecture decisions](docs/adr/) · [Per-epic summaries](docs/audits/)

<!-- SCREENSHOT SLOT: replace this comment with a real screenshot of the
app (a chat with a finished task, canvas open) once one is captured on a
real machine. No placeholder art. -->

## Install

### From a release

Installers are built by the release workflow (Windows NSIS, Linux
AppImage) and published on the
[Releases page](https://github.com/ProxCGaming/Aether-OS/releases).
The first release has not been published yet — until it is, install
from source below. Installers are unsigned: Windows SmartScreen will
warn on first run (choose *More info → Run anyway*).

### From source (works today)

Prerequisites: Python 3.12 with [uv](https://docs.astral.sh/uv/),
Node 24, Git.

```bash
git clone https://github.com/ProxCGaming/Aether-OS.git
cd Aether-OS
uv sync                    # engine dependencies
cd desktop_app && npm ci   # desktop dependencies
cd ..
```

Run it — engine first, then the app:

```bash
# terminal 1: engine on 127.0.0.1:8000
uv run python run_engine.py

# terminal 2: desktop app
cd desktop_app && npm run electron:dev
```

First run walks a 3-step onboarding: add a provider API key, pick a
workspace, choose an approval default.

### Build your own installer

The app bundles a portable Python runtime plus the engine as files;
assemble that payload first, then package:

```bash
python scripts/assemble_engine_payload.py --platform win32   # or: linux
cd desktop_app && npm run dist
```

`npm run dist` stops with a clear error if the payload has not been
assembled.

### Platform state

| Platform | From source | Packaged installer |
|---|---|---|
| Windows | Yes | NSIS configured and built by the release workflow; the install → onboard → uninstall walkthrough is not yet verified on a real Windows machine |
| Linux | Yes | AppImage built and inspected during Epic 10 |
| macOS | Expected to work (dependencies are cross-platform) — untested | No packaged build today |

## What it does

- **Chat and delegation.** Everyday chat answers directly. Ask for work
  and the supervisor assigns it to a specialist agent (planner, coder,
  researcher); each specialist can run its own model, set in
  Settings → Agents.
- **Approvals before actions.** File writes, shell commands, browser
  actions and payments pass one policy pipeline. Consequential actions
  wait for your click; payments are always confirmed individually.
- **Memory that persists.** A knowledge graph plus an episodic memory
  of past tasks. The memory backend is switchable; the built-in engine
  is the default.
- **Voice mode.** Speech-to-text (faster-whisper) and text-to-speech
  (Kokoro) run locally; the conversation's brain is any provider you
  configure. Approvals are never given by voice.
- **Canvas.** Files agents produce collect in a panel beside the chat,
  grouped by task — view, copy, or save a copy.
- **Workspace search.** Agents can search the folders you grant them.
  Every citation is re-checked against the live file before it is
  shown to you.
- **Screen snip.** Capture a region of your screen and ask about it
  (hotkey changeable in Settings).
- **Browser agent + password vault.** The agent browses in its own
  profile. Saved credentials are filled into the matching site's login
  form by the app shell — the agent never sees the password — and every
  use is audit-logged. The vault also stores TOTP codes and works as
  your authenticator app.
- **Model hub.** Settings → Local Models searches Hugging Face and the
  Ollama library, shows each model's format, size and licence, labels
  honestly how (or whether) it can run locally, and downloads in one
  click. Which model serves dictation, voice mode and media
  transcription is chosen in Settings → Models & Tasks.
- **Sessions per workspace.** Chats are filed under the project
  (workspace) they belong to; export or import any single session as
  Markdown or JSON.
- **Cost visibility.** Per-session cost and context indicators, with a
  persistent ledger that includes zero-cost local calls.

## Data & safety

- All state lives in `~/.aether/` — config, memory databases, skills,
  plugins, vault, audit log. Settings → Advanced backs it up to a
  hash-verified zip and restores it.
- The engine listens on `127.0.0.1` only and requires a per-install
  token, sent as an `Authorization` header and held by the Electron
  main process — the renderer never holds it.
- The diagnostics export (Settings → Advanced) is redacted: no API
  keys, no memory content.

## Development

Layout:

- `aether_engine/` — the engine: orchestration (LangGraph supervisor),
  tools, MCP client, memory, providers, secrets.
- `aether_common/` — the wire contracts shared with the UI.
- `desktop_app/` — the Electron + React app (`main.cjs`, `preload.cjs`,
  `electron/` helpers, `src/`).
- `tests/` — pytest suite; `desktop_app/src/**/__tests__/` — Jest.
- `docs/adr/` — architecture decisions; `docs/audits/` — per-epic
  summaries; `benchmarks/` — the Epic 9 perf harness.

Test:

```bash
uv run pytest                 # engine suite
cd desktop_app && npm test    # UI suite
```

As of 0.3.5: 2,719 pytest tests passing (11 known failures, all
environment-specific and tracked in `scripts/pytest-known-failures.txt`)
and 874 Jest tests passing. Coverage is measured, not gated — see the
coverage commands in `docs/audits/` and `.github/workflows/ci.yml`.

Commits are gated (commitlint + lint + related tests; pushes run the
full suites under a known-failures subset rule). Never bypass with
`--no-verify`. The full rules are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Known limitations

- The voice pipeline's sub-800 ms turn target is not met yet on the
  development VM (~7.9 s measured end to end, dominated by local
  speech-to-text); figures on real hardware are pending.
- The Windows packaged install walkthrough is owner-verification
  pending (see the platform table).
- Speech-to-speech voice mode and premium feature packs are designed,
  not built.

## Licence

No licence file yet. The source is available to read and build for
yourself; no redistribution rights are granted until a licence is
added.
