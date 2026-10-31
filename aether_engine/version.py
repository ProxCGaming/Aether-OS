"""The single source of truth for the Aether-OS app version.

The FastAPI app, the websocket HELLO payload, backup manifests and
diagnostics bundles all read this constant. The desktop shell's
version lives in desktop_app/package.json (electron-builder needs
it there); a test pins the two equal so they cannot drift.
"""

APP_VERSION = "0.3.5"
