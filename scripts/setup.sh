#!/usr/bin/env bash
# Aether-OS — machine setup for the terminal-run flow (no installer).
#
# One idempotent command that reconciles this machine EXACTLY to the
# project's pinned sources of truth, and then proves it:
#   uv.lock                    -> the engine's Python environment (.venv)
#   package-lock.json          -> the repo-root node_modules (commit tools)
#   desktop_app/package-lock.json -> the desktop app's node_modules
#   .python-version            -> the exact CPython (3.12.16)
#   .nvmrc                     -> the exact Node (24.22.1)
#
# Run it on a NEW machine after cloning, and run it AGAIN on an older
# machine after `git pull` (scripts/update.sh does both) — that re-run
# IS the update/sync flow. Nothing here ever installs from a version
# range or edits a lockfile.
#
# CLEAN-INSTALL RULE (owner directive, 15 Nov 2026): the script keeps
# a fingerprint of the lockfiles + version pins in
# `.aether-setup-stamp`. If the fingerprint does not match the last
# successful setup — the locks moved, a pin changed, or the stamp is
# missing — the old environments are WIPED (.venv, both node_modules,
# any stale engine-payload) and rebuilt from scratch. A drifted
# environment is never patched in place: a stale mixed install is
# exactly how a broken litellm/mcp pair once shipped in an installer.
# When the fingerprint matches, `uv sync --locked` + `npm ci` still
# reconcile exactly (npm ci is a clean install by definition).
#
# Prerequisites are only checked, never installed silently: Git, uv,
# and the exact Node from .nvmrc. Python is provisioned by uv itself,
# at the exact .python-version. If a step fails, the script stops
# and names the step.
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "setup: FAILED — $1" >&2; exit 1; }
step() { echo; echo "==> $1"; }

EXPECTED_NODE="v$(tr -d '[:space:]' < .nvmrc)"
EXPECTED_PYTHON="$(tr -d '[:space:]' < .python-version)"
PINNED_UV="0.13.6"

# --- 1. prerequisites ---------------------------------------------------
step "Checking prerequisites"
command -v git  >/dev/null 2>&1 || fail "git not found. Install Git: https://git-scm.com/downloads"
command -v uv   >/dev/null 2>&1 || fail "uv not found. Install it: https://docs.astral.sh/uv/getting-started/installation/"
command -v node >/dev/null 2>&1 || fail "node not found. Install the pinned Node $EXPECTED_NODE (see .nvmrc), e.g. with nvm: nvm install ${EXPECTED_NODE#v}"
command -v npm  >/dev/null 2>&1 || fail "npm not found. It ships with Node — reinstall Node."
NODE_FOUND="$(node --version)"
[ "$NODE_FOUND" = "$EXPECTED_NODE" ] || fail "Node $EXPECTED_NODE required (see .nvmrc), found $NODE_FOUND. Versions are pinned exactly — install it with: nvm install ${EXPECTED_NODE#v} && nvm use ${EXPECTED_NODE#v}"
UV_FOUND="$(uv --version | awk '{print $2}')"
if [ "$UV_FOUND" != "$PINNED_UV" ]; then
  echo "setup: WARNING — uv $UV_FOUND found, the pinned uv is $PINNED_UV (workflows pin it exactly). Continuing; if a sync misbehaves, install uv $PINNED_UV first."
fi
echo "git $(git --version | awk '{print $3}'), uv $UV_FOUND, node $NODE_FOUND, npm $(npm --version)"

# --- 2. fingerprint: clean-install on any mismatch -----------------------
step "Checking this machine against the lock fingerprint"
FINGERPRINT="$(cat uv.lock package-lock.json desktop_app/package-lock.json .python-version .nvmrc | sha256sum | awk '{print $1}')"
STAMP_FILE=".aether-setup-stamp"
STAMPED=""
[ -f "$STAMP_FILE" ] && STAMPED="$(tr -d '[:space:]' < "$STAMP_FILE")"
if [ "$FINGERPRINT" != "$STAMPED" ] || [ ! -x .venv/bin/python ] || [ ! -d node_modules ] || [ ! -d desktop_app/node_modules ]; then
  echo "Lock fingerprint changed (or first setup) — CLEAN INSTALL: wiping .venv, node_modules, desktop_app/node_modules and any stale desktop_app/engine-payload, then rebuilding exactly from the locks."
  rm -rf .venv node_modules desktop_app/node_modules desktop_app/engine-payload
else
  echo "Fingerprint matches the last setup — reconciling in place (uv sync --locked / npm ci are exact)."
fi

# --- 3. Python venv, at the exact pinned CPython --------------------------
step "Creating the Python venv ($EXPECTED_PYTHON) if needed"
if [ ! -x .venv/bin/python ]; then
  uv venv .venv --python "$EXPECTED_PYTHON"
else
  echo ".venv already exists — keeping it; the sync below reconciles it."
fi

# --- 4. Engine dependencies, exactly from uv.lock -------------------------
step "Installing engine dependencies from uv.lock (all extras)"
uv sync --locked --all-extras

# --- 5. Node dependencies, exactly from the package-locks -----------------
step "Installing repo-root dependencies from package-lock.json (commit tooling)"
npm ci
step "Installing desktop dependencies from package-lock.json (inside desktop_app)"
( cd desktop_app && npm ci )

# --- 6. Smoke checks ------------------------------------------------------
step "Smoke check 1/3: locked venv imports (same list CI verifies)"
.venv/bin/python -c "import sys;
assert sys.version_info[:2] == (3, 12), sys.version;
import uvicorn, google.genai, apscheduler, ddgs, keyring, aiosqlite, faster_whisper, kokoro, onnxruntime, silero_vad;
print('locked venv import smoke: ok')"

step "Smoke check 2/3: frontend builds (vite resolves every import)"
( cd desktop_app && npm run build )

step "Smoke check 3/3: the whole pytest suite collects (engine imports clean)"
.venv/bin/python -m pytest --collect-only -q 2>&1 | tail -n 3

# --- 7. Stamp the fingerprint this machine now matches ---------------------
printf '%s\n' "$FINGERPRINT" > "$STAMP_FILE"

echo
echo "setup: OK — this machine matches the locks exactly."
echo "  Python: $(.venv/bin/python --version 2>&1) (.venv)   Node: $NODE_FOUND"
echo "  Run Aether with:  scripts/run.sh    Update later with:  scripts/update.sh"
