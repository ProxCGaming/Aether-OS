#!/usr/bin/env bash
# Aether-OS — update this machine: pull the latest source, then
# reconcile the machine to the locks.
#
# This is THE way a source checkout stays current: `git pull
# --ff-only` brings new code AND new lockfiles/pins, and setup.sh
# then rebuilds the environments from them — clean-installing
# (.venv, node_modules, engine-payload wiped and rebuilt) whenever
# the lock fingerprint moved, per the clean-install rule in
# setup.sh's header. User data (~/.aether) is never touched.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Pulling the latest source (fast-forward only)"
git pull --ff-only

exec scripts/setup.sh
