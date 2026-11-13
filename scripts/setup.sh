#!/usr/bin/env bash
# Aether-OS — machine setup for the terminal-run flow (no installer).
#
# One idempotent command that reconciles this machine to the two
# lockfiles and then proves it:
#   uv.lock            -> the engine's Python environment (.venv)
#   package-lock.json  -> the desktop app's node_modules
#
# Run it on a NEW machine after cloning, and run it AGAIN on an older
# machine after `git pull` — that re-run IS the update/sync flow.
# `uv sync --locked` and `npm ci` both reconcile exactly to their lock
# (adding, removing and re-pinning as needed), and neither ever edits
# a lockfile. If a step fails, the script stops and names the step.
#
# Prerequisites are only checked, never installed silently: Git, uv,
# Node 24 + npm. Python 3.12 is provisioned by uv itself when missing.
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "setup: FAILED — $1" >&2; exit 1; }
step() { echo; echo "==> $1"; }

# --- 1. prerequisites ---------------------------------------------------
step "Checking prerequisites"
command -v git  >/dev/null 2>&1 || fail "git not found. Install Git: https://git-scm.com/downloads"
command -v uv   >/dev/null 2>&1 || fail "uv not found. Install it: https://docs.astral.sh/uv/getting-started/installation/"
command -v node >/dev/null 2>&1 || fail "node not found. Install Node 24: https://nodejs.org"
command -v npm  >/dev/null 2>&1 || fail "npm not found. It ships with Node — reinstall Node 24."
NODE_MAJOR="$(node --version | sed -E 's/^v([0-9]+).*/\1/')"
[ "$NODE_MAJOR" = "24" ] || fail "Node 24 required (see .nvmrc), found $(node --version). Install Node 24, or with nvm: nvm install 24 && nvm use 24"
echo "git $(git --version | awk '{print $3}'), uv $(uv --version | awk '{print $2}'), node $(node --version), npm $(npm --version)"

# --- 2. Python venv ------------------------------------------------------
step "Creating the Python venv (3.12) if needed"
if [ ! -x .venv/bin/python ]; then
  uv venv .venv --python 3.12
else
  echo ".venv already exists — keeping it; the sync below reconciles it."
fi

# --- 3. Engine dependencies, exactly from uv.lock ------------------------
step "Installing engine dependencies from uv.lock (all extras)"
uv sync --locked --all-extras

# --- 4. Desktop dependencies, exactly from package-lock.json -------------
step "Installing desktop dependencies from package-lock.json (inside desktop_app)"
( cd desktop_app && npm ci )

# --- 5. Smoke checks ------------------------------------------------------
step "Smoke check 1/3: locked venv imports (same list CI verifies)"
.venv/bin/python -c "import sys;
assert sys.version_info[:2] == (3, 12), sys.version;
import uvicorn, google.genai, apscheduler, ddgs, keyring, aiosqlite, faster_whisper, kokoro, onnxruntime, silero_vad;
print('locked venv import smoke: ok')"

step "Smoke check 2/3: frontend builds (vite resolves every import)"
( cd desktop_app && npm run build )

step "Smoke check 3/3: the whole pytest suite collects (engine imports clean)"
.venv/bin/python -m pytest --collect-only -q 2>&1 | tail -n 3

echo
echo "setup: OK — this machine matches the locks."
echo "  Python: $(.venv/bin/python --version 2>&1) (.venv)   Node: $(node --version)"
echo "  Run Aether with:  scripts/run.sh"
