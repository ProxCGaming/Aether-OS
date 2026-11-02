#!/usr/bin/env bash
# Run the Jest tests related to the staged files passed by lint-staged.
# Passes when no related tests exist: the "component must have a test"
# rule lives in scripts/check-jest-test.sh, not here. Fails only when a
# related test actually runs and fails.
set -u

cd "$(dirname "$0")/../desktop_app" || exit 1

if [ "$#" -eq 0 ]; then
  exit 0
fi

output=$(npx --no -- jest --watchAll=false --findRelatedTests "$@" 2>&1)
status=$?
echo "$output"

if [ "$status" -eq 0 ]; then
  exit 0
fi
if echo "$output" | grep -q "No tests found"; then
  exit 0
fi
exit 1
