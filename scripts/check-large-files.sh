#!/usr/bin/env bash
# Block a commit when a staged file is over 10 MB.
# Single clear failure message naming the file and its size.
set -u

LIMIT=$((10 * 1024 * 1024))
blocked=0

while IFS= read -r -d '' file; do
  size=$(git cat-file -s ":$file" 2>/dev/null) || continue
  if [ "$size" -gt "$LIMIT" ]; then
    mb=$(awk "BEGIN {printf \"%.1f\", $size / 1048576}")
    echo "pre-commit: staged file '$file' is ${mb} MB (over the 10 MB limit). Remove it from the commit or use Git LFS / external storage." >&2
    blocked=1
  fi
done < <(git diff --cached --name-only -z --diff-filter=ACMR)

if [ "$blocked" -ne 0 ]; then
  exit 1
fi

exit 0
