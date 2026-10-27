#!/usr/bin/env bash
# Run oxlint on the staged JS/JSX files passed by lint-staged.
# Runs from desktop_app so its .oxlintrc.json is picked up.
# Exits non-zero when oxlint reports errors (warnings alone do not block).
set -u

cd "$(dirname "$0")/../desktop_app" || exit 1

if [ "$#" -eq 0 ]; then
  exit 0
fi

exec npx --no -- oxlint "$@"
