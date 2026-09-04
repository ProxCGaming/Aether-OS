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
    background: transparent;
    width: 6px;
    margin: 2px;
    border-radius: 3px;
}}
QScrollBar::handle:vertical {{
    background: {SURFACE_BORDER_LIGHT};
    min-height: 30px;
    border-radius: 3px;
}}
QScrollBar::handle:vertical:hover {{
    background: {TEXT_MUTED};
}}
QScrollBar::handle:vertical:pressed {{
    background: {TEXT_SECONDARY};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
    background: none;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: none;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 6px;
    margin: 2px;
    border-radius: 3px;
}}
QScrollBar::handle:horizontal {{
    background: {SURFACE_BORDER_LIGHT};
    min-width: 30px;
    border-radius: 3px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {TEXT_MUTED};
}}
QScrollBar::handle:horizontal:pressed {{
    background: {TEXT_SECONDARY};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
    background: none;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
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
    background-color: {SURFACE_PANEL};
    border: 1px solid {SURFACE_BORDER};
    border-radius: 4px;
    selection-background-color: {SURFACE_CARD};
    selection-color: {BRAND_FRONTEND};
    color: {TEXT_PRIMARY};
    padding: 4px;
    outline: none;
}}
"""

LIST_CSS = f"""
QListWidget {{
    background: #0D1017;
    border: 1px solid {SURFACE_BORDER};
    border-radius: 6px;
    color: {TEXT_PRIMARY};
    font-size: 12px;
    font-family: 'Segoe UI', -apple-system, sans-serif;
    outline: none;
}}
QListWidget::item {{
    padding: 8px 12px;
    border-radius: 4px;
    margin: 2px 4px;
    color: {TEXT_PRIMARY};
}}
QListWidget::item:hover {{
    background: {SURFACE_BORDER};
}}
QListWidget::item:selected {{
    background: rgba(124, 111, 255, 0.15);
    color: {BRAND_FRONTEND};
    font-weight: bold;
    border: 1px solid rgba(124, 111, 255, 0.3);
}}
"""

CHECKBOX_CSS = f"""
QCheckBox {{
    color: {TEXT_SECONDARY};
    font-size: 13px;
    font-weight: 500;
    spacing: 10px;
}}
QCheckBox:hover {{
    color: {TEXT_PRIMARY};
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border: 1.5px solid {SURFACE_BORDER_LIGHT};
    border-radius: 4px;
    background: #0D1017;
}}
QCheckBox::indicator:hover {{
    border-color: {BRAND_FRONTEND};
    background: #141824;
}}
QCheckBox::indicator:checked {{
    background: {BRAND_ENGINE};
    border-color: {BRAND_ENGINE};
}}
QCheckBox::indicator:checked:hover {{
    background: #45F0D5;
    border-color: #45F0D5;
}}
"""

PROVIDER_TILE_CSS = f"""
QPushButton {{
    background: {SURFACE_PANEL};
    border: 1px solid {SURFACE_BORDER};
    border-radius: 8px;
    text-align: left;
    padding: 12px;
    color: {TEXT_PRIMARY};
}}
QPushButton:hover {{
    background: #141824;
    border: 1px solid {BRAND_FRONTEND};
}}
"""

MODEL_ROW_CSS = f"""
QListWidget {{
    background: #090B10;
    border: 1px solid {SURFACE_BORDER};
    border-radius: 6px;
    padding: 4px;
}}
QListWidget::item {{
    color: {TEXT_PRIMARY};
    padding: 8px;
    border-radius: 4px;
}}
QListWidget::item:hover {{
    background: {SURFACE_PANEL};
}}
QListWidget::item:selected {{
    background: {BRAND_ENGINE}33; /* transparent teal */
    color: {BRAND_ENGINE};
    border-left: 3px solid {BRAND_ENGINE};
}}
"""

QUICK_ADD_BTN_CSS = f"""
QPushButton {{
    background: transparent;
    border: 1px solid {BRAND_FRONTEND};
    border-radius: 14px;
    color: {BRAND_FRONTEND};
    font-weight: 600;
    padding: 4px 12px;
}}
QPushButton:hover {{
    background: {BRAND_FRONTEND}22;
}}
"""
