"""The single source of truth for the Aether-OS app version.

The FastAPI app, the websocket HELLO payload, backup manifests and
diagnostics bundles all read this constant. Three mirrors carry the
same value because their tooling needs it locally: the desktop
shell's version in desktop_app/package.json (electron-builder needs
it there), the Python project version in pyproject.toml (the project
is virtual for uv — no build system reads this file, so it cannot be
derived dynamically), and the root package.json (the husky /
commitlint / lint-staged manifest). Tests in
tests/test_epic10_hardening.py pin all three mirrors equal to this
constant so they cannot drift.
"""

APP_VERSION = "0.3.5"
