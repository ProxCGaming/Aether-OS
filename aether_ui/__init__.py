"""Retired PySide6 UI — kept for reference, not shipped by default.

Superseded by the Electron app in ``desktop_app/`` (ADR-0019);
``run_ui.py`` launches Electron, and nothing in the engine imports this
package. Its dependencies (PySide6, qasync, markdown) are available
only via the ``legacy-ui`` optional extra: ``uv sync --extra legacy-ui``.
"""

from .state import UIState as UIState
from .state import UIStateMachine as UIStateMachine

try:
    from .hud import HudWidget as HudWidget
    from .main import AetherWindow as AetherWindow
    from .main import run_ui as run_ui
    from .orb import OrbWidget as OrbWidget
    from .ws_client import AetherWSClient as AetherWSClient
except ImportError:
    pass
