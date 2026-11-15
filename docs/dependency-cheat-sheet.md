# Dependency cheat sheet — every sync / install / reinstall command

Companion to [dependencies.md](dependencies.md) (what is pinned
where, and why). This page is just the commands. Windows
(PowerShell) first — that's the main dev machine — with the
bash equivalent wherever it differs. Run repo commands from the
repo root unless a step says otherwise.

**The three rules everything below follows**

1. The lockfiles are the truth: `uv.lock` for Python,
   `package-lock.json` (root + `desktop_app/`) for Node.
2. Syncing is always the *locked, clean* command — `uv sync
   --locked` and `npm ci`. Never `pip install`, never plain
   `npm install`, to "fix" a drifted environment.
3. If versions mismatch, clean-install — the setup scripts do it
   automatically (section 3); by hand it's "delete the folder,
   re-run the sync command".

---

## 1. Python packages → the project `.venv`

```powershell
# Sync the venv EXACTLY from uv.lock (engine + voice extra + dev tools)
uv sync --locked --all-extras

# Clean rebuild of the venv from scratch
Remove-Item -Recurse -Force .venv
uv sync --locked --all-extras
# bash:  rm -rf .venv && uv sync --locked --all-extras

# What is actually installed, and one package's version
uv pip list
uv run python -c "import litellm; print(litellm.__version__)"

# Check the lock still agrees with pyproject.toml
uv lock --check

# Run the pinning contract tests (pins, pairs, install paths)
uv run pytest tests/test_dependency_pinning.py -q
```

**Change a Python package's version** (never edit `uv.lock` by hand):

1. Edit the `==` pin in `pyproject.toml`.
2. `uv lock`
3. `uv sync --locked --all-extras`
4. Run the gates, then commit `pyproject.toml` + `uv.lock` together.

**Add a new Python package:** add `"name==X.Y.Z"` to `pyproject.toml`
yourself, then steps 2–4 above.

## 2. Node packages → `node_modules` (TWO places: repo root + `desktop_app/`)

```powershell
# Sync both, exactly from their locks (npm ci wipes node_modules
# itself — it IS a clean install every time)
npm ci
cd desktop_app; npm ci; cd ..

# Check the runtime versions
node --version    # must print v24.22.1 (the .nvmrc pin)
npm --version
```

**Change a Node package's version** (never edit a lockfile by hand):

1. Edit the version in the right `package.json` to the exact new one.
2. In that folder: `npm install --package-lock-only`
3. `npm ci`, run Jest, commit `package.json` + `package-lock.json` together.

## 3. One-command flows (first install + every update)

```powershell
# First time on a machine
.\scripts\setup.ps1
# bash:  ./scripts/setup.sh

# Every update after that: pull + sync (+ clean install if locks moved)
.\scripts\update.ps1
# bash:  ./scripts/update.sh

# Force a clean install by hand: delete the fingerprint, run setup
Remove-Item .aether-setup-stamp
.\scripts\setup.ps1
```

Setup fingerprints `uv.lock` + both `package-lock.json` files +
`.python-version` + `.nvmrc` into `.aether-setup-stamp`. When the
fingerprint differs it wipes `.venv`, both `node_modules` and any
stale `desktop_app/engine-payload`, then rebuilds from the locks.

## 4. The engine payload and `npm run dist`

**`npm run dist` does NOT use the project `.venv` at all.** The
payload has its own portable Python; the assembler installs the
locked packages into *it*. What `dist` needs on the machine:
Node (section 5), plus Python 3.12 + `uv` on PATH to run the
assembler.

```powershell
# Assemble / re-assemble the payload (from the REPO ROOT)
python scripts/assemble_engine_payload.py --platform win32
# --platform linux / darwin on those machines

# Package the installer (from desktop_app/)
cd desktop_app
npm run dist
```

The `predist` guard refuses to package when the payload is
**missing** or **stale**: it compares the `uv_lock_sha256` inside
`engine-payload/payload-manifest.json` with the current `uv.lock`.
If it refuses, re-run the assemble command above — never hand-copy
libraries into the payload.

> Gotcha: before running the FULL pytest suite, delete
> `desktop_app/engine-payload/` — one repo-scan test fails on the
> bundled libraries inside it. Re-assemble afterwards for `dist`.

## 5. The tools on PATH — uv, Node, npm, Python

**Creating a venv does NOT fix these.** A venv only holds Python
*packages*; the tools below are programs the machine runs before
any venv exists (`uv` is what creates the venv). If a tool is
missing, broken or the wrong version, reinstall the tool first,
then re-run the sync from sections 1–2.

| Tool | Check | Pinned | Reinstall / repair |
|---|---|---|---|
| uv | `uv --version` | 0.13.6 | Re-run the versioned installer (below) |
| Node | `node --version` | 24.22.1 | nvm: `nvm install 24.22.1` then `nvm use 24.22.1` |
| npm | `npm --version` | ships with Node | `npm install -g npm@latest` (or `@X.Y.Z` for an exact one) |
| Python | `uv python list` | 3.12.16 | `uv python install 3.12.16` |

```powershell
# Reinstall uv at the exact pinned version (PowerShell)
irm https://astral.sh/uv/0.13.6/install.ps1 | iex
# bash:  curl -LsSf https://astral.sh/uv/0.13.6/install.sh | sh
# NOTE: `uv self update` goes to LATEST, not the pin — don't use it
# when you need 0.13.6 exactly; re-run the versioned installer.

# Reinstall / switch Node with nvm-windows (or nvm on bash — same words)
nvm install 24.22.1
nvm use 24.22.1

# Python: you rarely need a system Python at all — uv provisions
# the pinned interpreter and builds .venv from it:
uv python install 3.12.16
uv python list
# A PATH python is only needed to run stdlib-only scripts (like the
# payload assembler). If yours is broken, either reinstall it
# (python.org installer, or: winget install Python.Python.3.12)
# or just use the venv's interpreter for the assembler:
#   .venv\Scripts\python.exe scripts\assemble_engine_payload.py --platform win32
```

Electron is **not** a PATH tool — it's a locked package inside
`desktop_app/node_modules`; `npm ci` (section 2) is its reinstall.

## 6. Verify everything at once

```powershell
uv lock --check
uv run pytest tests/test_dependency_pinning.py tests/test_machine_setup_flow.py -q
node --version; npm --version; uv --version
cd desktop_app; npx jest electron/__tests__/ensureEnginePayload.test.js --watchAll=false; cd ..
```

Expected: lock check silent-OK, tests green, versions
`v24.22.1` / (npm as shipped) / `uv 0.13.6`.
