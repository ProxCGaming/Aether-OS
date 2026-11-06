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
# D4-16: jest's combined output can contain "No tests found" (from
# one pattern's lookup) while a genuinely related test FAILED in the
# same run — grepping for the phrase alone laundered that failure
# into a pass. Honour the phrase only when nothing ran at all: no
# suite results (no PASS/FAIL suite lines, no "Tests:" summary with
# results) and no failed tests.
if echo "$output" | grep -q "No tests found" \
  && ! echo "$output" | grep -qE "^(PASS|FAIL) " \
  && ! echo "$output" | grep -qE "Tests:.*[1-9][0-9]* (failed|passed)"; then
  exit 0
fi
exit 1
