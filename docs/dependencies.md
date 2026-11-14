# Dependencies — what is pinned where, and how to update everything

**The rule (owner directive, 15 Nov 2026): every library, package,
runtime and tool this app uses is pinned to an exact version, each
ecosystem has ONE source of truth in its own file, and every
install path consumes that file — never a version range.**

Why: an installer once shipped litellm 1.108.0 while dev/CI tested
the locked 1.106.4, because the engine-payload assembler installed
the `pyproject.toml` *ranges* fresh at build time. That litellm,
paired with the project's mcp 1.30.0, crashed every tool-carrying
model call (`mcp.types.CacheableResult` exists only in mcp 2.x)
behind a misleading "network/service error". Ranges are how
untested combinations reach users; exact pins + lockfiles are how
they don't. `tests/test_dependency_pinning.py` fails if any pin
below floats again.

## The map

| What | Source of truth | Pin | Who installs from it, and how |
|---|---|---|---|
| Python packages (engine, incl. voice extra + dev tools) | `uv.lock`, generated from the exact `==` pins in `pyproject.toml` | every package, exact version + hashes | Dev/CI: `uv sync --locked`. Shipped payload: the assembler exports the lock (`uv export --locked`, hashes included) and installs only that |
| Python interpreter | `.python-version` | CPython **3.12.16** | uv provisions it for the venv; the payload's portable runtime is the same version (pin + per-platform SHA-256 in `scripts/assemble_engine_payload.py`, transcribed from python-build-standalone's `SHA256SUMS`) |
| Desktop (Electron/React) packages | `desktop_app/package-lock.json`, with exact versions in `desktop_app/package.json` | every package, exact | `npm ci` (CI, release, setup scripts) — a clean install from the lock by definition |
| Repo-root packages (commit tooling) | `package-lock.json`, with exact versions in `package.json` | every package, exact | `npm ci` (setup scripts) |
| Node runtime | `.nvmrc` | **24.22.1** | setup scripts require it exactly; CI/release pass it to setup-node; `desktop_app/package.json` `engines` mirrors it |
| uv (the Python toolchain) | the `version:` input on setup-uv in both workflows; mirrored in `scripts/setup.*` | **0.13.6** | workflows install exactly it; setup scripts warn when a machine's uv differs |
| GitHub Actions | `.github/workflows/ci.yml`, `.github/workflows/release.yml` | every action pinned to a **commit SHA** with a `# vX.Y.Z` comment | GitHub runs exactly those commits |
| Electron | part of the desktop lock above | the exact version in `desktop_app/package.json` | ships inside the app; end users never install it separately |
| App version | `aether_engine/version.py` (`APP_VERSION`) | mirrored exactly in `pyproject.toml`, both `package.json` files | the release workflow refuses a tag that disagrees |

The assembled payload proves its own provenance:
`payload-manifest.json` inside every payload records the SHA-256
of the `uv.lock` it was built from and of the hashed requirements
export it installed, so an installed app's engine can be checked
against the repo's lock byte-for-byte. Payload verification also
runs a tool-carrying litellm call (mock response, no network) at
assembly time — the exact call shape that once crashed — so a
broken library pair can never ship again.

## How to bump a dependency

Never edit a lockfile by hand, and never widen a pin to a range.

- **Python:** change the `==` pin in `pyproject.toml`, run
  `uv lock`, run the gates (`uv run pytest`, and for provider
  libraries a real tool-carrying call), commit `pyproject.toml` +
  `uv.lock` together.
- **Node:** `npm install --save-exact <pkg>@<version>` in the right
  folder (or edit `package.json` to the exact version and run
  `npm install --package-lock-only`), run Jest, commit the manifest
  + lock together.
- **Node/Python/uv/Action pins:** change the pin file (`.nvmrc`,
  `.python-version`, the workflow), update every mirror named in
  the map in the same commit — `tests/test_dependency_pinning.py`
  names any mirror you missed.

## How anyone updates EVERYTHING on their machine

There are two kinds of installs. Both update the whole stack —
app code, engine, and every library — to one exact pinned set.
Nothing is ever patched library-by-library in place.

### Installed app (Windows installer / Linux AppImage)

1. Open **Settings → About → Check for updates**. The app compares
   its version with the latest GitHub Release and tells you plainly.
2. If an update is available, **Download the new installer**, verify
   it against the `SHA256SUMS` file attached to the Release if you
   want byte-level certainty, and run it.
3. The installer replaces the entire app — shell, engine payload,
   all bundled libraries — with the new release's pinned set. Your
   data is not part of the install tree and is kept: `~/.aether`
   (config, memory, logs) and the app's userData survive updates.

### Source checkout (developers, testers)

- First time: `scripts/setup.sh` / `scripts/setup.ps1` — installs
  everything from the locks and proves it with smoke checks.
- Every update after that: **`scripts/update.sh` /
  `scripts/update.ps1`** — one command: `git pull --ff-only`, then
  setup. Setup fingerprints the lockfiles + pin files
  (`.aether-setup-stamp`); when they moved, it **clean-installs**:
  `.venv`, both `node_modules`, and any stale engine payload are
  wiped and rebuilt from the locks. A drifted environment is never
  patched in place.

## Downloads the app manages at runtime (honest scope)

These are *content the user chooses*, not app libraries, and they
follow their own rules:

- **Playwright's Chromium** is downloaded on first browser use.
  Its revision is fixed by the pinned `playwright` package version,
  so the browser build is as pinned as the package is.
- **Speech and local models** (faster-whisper, Kokoro, Ollama
  models) are downloaded by name from the Model Hub, at the user's
  request, into the user's own model stores. Model weights are user
  content — they update when the user downloads a newer one, and
  the built-in speech repo *names* (`Systran/faster-whisper-small`,
  `hexgrad/kokoro-82m`) are fixed in code while their upstream
  revisions are not hash-pinned today.
