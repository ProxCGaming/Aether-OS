#!/usr/bin/env bash
# Run Ruff on the staged Python files passed by lint-staged.
# Check only, never --fix: auto-fixing at commit time can change behaviour.
# Fails LOUDLY (never silently passes) when Ruff cannot run:
#   - uv missing -> "install uv" message
#   - Ruff not installed in the project venv -> "run uv sync" message
set -u

if command -v uv >/dev/null 2>&1; then
  UV=uv
elif [ -x "$HOME/.local/bin/uv" ]; then
  UV="$HOME/.local/bin/uv"
else
  echo "pre-commit: 'uv' was not found on PATH, so Ruff could not run." >&2
  echo "Install uv (https://docs.astral.sh/uv/getting-started/installation/) and run 'uv sync', then commit again." >&2
  exit 1
fi

if ! "$UV" run --no-sync ruff --version >/dev/null 2>&1; then
  echo "pre-commit: Ruff is not installed in this project's environment." >&2
  echo "Run 'uv sync' to install it, then commit again." >&2
  exit 1
fi

if [ "$#" -eq 0 ]; then
  exit 0
fi

exec "$UV" run --no-sync ruff check "$@"
