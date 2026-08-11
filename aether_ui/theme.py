"""Centralized Design Tokens & Stylesheets for AETHER-OS (Direction A — 'Ionized Void').

Tokens extracted directly from Figma MVP specification:
- surface/bg: #0B0D12
- surface/panel: #14171F
- surface/border: #262A36
- text/primary: #E8E6F0
- text/secondary: #8A8FA3
- brand/frontend: #7C6FFF
- brand/engine: #33E0C4
- status/healthy: #2BD9A0
- status/degraded: #F2A93B
- status/offline: #F2415B
- status/standby: #6B6F7A
"""

# ---------------------------------------------------------------------------
# Color Tokens
# ---------------------------------------------------------------------------
SURFACE_BG = "#0B0D12"
SURFACE_PANEL = "#14171F"
SURFACE_PANEL_HOVER = "#1A1E29"
SURFACE_CARD = "#12151D"
SURFACE_BORDER = "#262A36"
SURFACE_BORDER_LIGHT = "#363C4E"

TEXT_PRIMARY = "#E8E6F0"
TEXT_SECONDARY = "#8A8FA3"
TEXT_MUTED = "#555A6E"

BRAND_FRONTEND = "#7C6FFF"
BRAND_ENGINE = "#33E0C4"

STATUS_HEALTHY = "#2BD9A0"
STATUS_DEGRADED = "#F2A93B"
STATUS_OFFLINE = "#F2415B"
STATUS_STANDBY = "#6B6F7A"

# ---------------------------------------------------------------------------
# Common Stylesheets (Using strict Qt QSS compatible syntax and rgba)
# ---------------------------------------------------------------------------
SCROLLBAR_CSS = f"""
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
QScrollBar:vertical {{
    background: {SURFACE_BG};
    width: 6px;
    margin: 0px;
    border-radius: 3px;
}}
QScrollBar::handle:vertical {{
    background: {SURFACE_BORDER};
    min-height: 24px;
    border-radius: 3px;
}}
QScrollBar::handle:vertical:hover {{
    background: {BRAND_FRONTEND};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: none;
}}
"""

INPUT_CSS = f"""
QLineEdit {{
    background: #0D1017;
    color: {TEXT_PRIMARY};
    font-size: 12px;
    font-family: 'Segoe UI', -apple-system, sans-serif;
    border: 1px solid {SURFACE_BORDER};
    border-radius: 6px;
    padding: 6px 12px;
}}
QLineEdit:focus {{
    border: 1px solid {BRAND_FRONTEND};
    background: #10141D;
}}
QLineEdit:disabled {{
    background: #080A0E;
    color: {TEXT_MUTED};
    border-color: #1A1D27;
}}
"""

COMBO_CSS = f"""
QComboBox {{
    background: #0D1017;
    color: {BRAND_ENGINE};
    font-size: 12px;
    font-weight: 600;
    font-family: 'Segoe UI', -apple-system, sans-serif;
    border: 1px solid {SURFACE_BORDER};
    border-radius: 6px;
    padding: 5px 10px;
    min-width: 140px;
}}
QComboBox:hover {{
    border-color: {BRAND_FRONTEND};
}}
QComboBox:focus {{
    border-color: {BRAND_ENGINE};
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {TEXT_SECONDARY};
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background: {SURFACE_PANEL};
    color: {TEXT_PRIMARY};
    selection-background-color: #242247;
    selection-color: {BRAND_FRONTEND};
    border: 1px solid {SURFACE_BORDER};
    border-radius: 6px;
    padding: 4px;
    outline: none;
}}
"""

CHECKBOX_CSS = f"""
QCheckBox {{
    color: {TEXT_SECONDARY};
    font-size: 12px;
    spacing: 6px;
}}
QCheckBox:hover {{
    color: {TEXT_PRIMARY};
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {SURFACE_BORDER};
    border-radius: 4px;
    background: #0D1017;
}}
QCheckBox::indicator:hover {{
    border-color: {BRAND_ENGINE};
}}
QCheckBox::indicator:checked {{
    background: {BRAND_ENGINE};
    border-color: {BRAND_ENGINE};
    image: none;
}}
"""
