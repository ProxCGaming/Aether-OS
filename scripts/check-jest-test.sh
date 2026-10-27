#!/usr/bin/env bash
# Block a commit when a staged React component has no Jest test.
# This is the SINGLE place for the Jest-test-existence logic.
#
# Rules (PRD Q5):
# - A staged desktop_app/src/**/*.jsx component that is new, or modified,
#   must have a *.test.jsx somewhere: <dir>/__tests__/<Name>.test.jsx,
#   <dir>/<Name>.test.jsx, or any tracked/staged <Name>.test.jsx under
#   desktop_app/src.
# - Do NOT block if a test file already exists, even if it was not touched
#   in this commit.
# - Do NOT block non-component files: *.test.jsx files, files under
#   __tests__/, and desktop_app/src/main.jsx (the app entry point).
set -u

missing=()

# Staged .jsx files under desktop_app/src (added, copied, modified, renamed).
while IFS= read -r -d '' file; do
  case "$file" in
    *.test.jsx) continue ;;
    */__tests__/*) continue ;;
    desktop_app/src/main.jsx) continue ;;
  esac

  dir=$(dirname "$file")
  base=$(basename "$file" .jsx)
  expected="$dir/__tests__/$base.test.jsx"

  # 1. Conventional spot: <dir>/__tests__/<Name>.test.jsx on disk.
  if [ -f "$expected" ]; then
    continue
  fi
  # 2. Colocated: <dir>/<Name>.test.jsx on disk.
  if [ -f "$dir/$base.test.jsx" ]; then
    continue
  fi
  # 3. Any tracked or staged <Name>.test.jsx under desktop_app/src.
  # (glob magic: ** crosses directories, including zero levels.)
  if git ls-files --cached -- ":(glob)desktop_app/src/**/$base.test.jsx" | grep -q .; then
    continue
  fi

  missing+=("$file (missing test: $expected)")
done < <(git diff --cached --name-only -z --diff-filter=ACMR -- 'desktop_app/src/*.jsx' 'desktop_app/src/**/*.jsx')

if [ "${#missing[@]}" -gt 0 ]; then
  echo "pre-commit: the following staged component(s) have no Jest test:" >&2
  for m in "${missing[@]}"; do
    echo "  - $m" >&2
  done
  echo "Add the test file (e.g. desktop_app/src/__tests__/Foo.test.jsx) and commit again." >&2
  exit 1
fi

exit 0
