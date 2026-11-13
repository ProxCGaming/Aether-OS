#!/usr/bin/env bash
# Aether-OS — run from the terminal.
#
# Starts the engine, waits for its /health to answer (120s budget, the
# same one the packaged app's lifecycle allows), then launches the
# desktop app in dev mode. Closing the app stops the engine again.
#
# WARNING: do not use this script when testing the PACKAGED app's own
# engine startup — it pre-starts an engine on :8000, which a packaged
# app would adopt, masking payload/lifecycle faults.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -x .venv/bin/python ] || { echo "run: no .venv yet — run scripts/setup.sh first." >&2; exit 1; }

ENGINE_PID=""
cleanup() { if [ -n "$ENGINE_PID" ]; then kill "$ENGINE_PID" 2>/dev/null || true; fi; }
trap cleanup EXIT INT TERM

echo "run: starting the engine on 127.0.0.1:8000 ..."
.venv/bin/python run_engine.py &
ENGINE_PID=$!

HEALTHY=0
for _ in $(seq 1 120); do
  if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then HEALTHY=1; break; fi
  if ! kill -0 "$ENGINE_PID" 2>/dev/null; then
    echo "run: FAILED — the engine exited during startup (see its output above)." >&2
    exit 1
  fi
  sleep 1
done
[ "$HEALTHY" = "1" ] || { echo "run: FAILED — the engine did not answer /health within 120s." >&2; exit 1; }
echo "run: engine healthy — launching the desktop app."

cd desktop_app && npm run electron:dev
