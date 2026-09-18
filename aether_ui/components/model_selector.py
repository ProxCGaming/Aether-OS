"""Model Selector Dropdown Panel for HUD.

Provides a custom popup panel matching the 'Ionized Void' styling, 
with provider groupings, search filtering, and a manage models footer link.
"""
from typing import Dict, List
from PySide6.QtCore import Qt, Signal, QPoint, QSize
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QLineEdit,
    QFrame,
    QListWidget,
    QListWidgetItem
)

from aether_ui.theme import (
    BRAND_ENGINE,
    BRAND_FRONTEND,
    SURFACE_BORDER,
    SURFACE_PANEL,
    SURFACE_PANEL_HOVER,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY
)

# Unused — kept as reference; popup now uses inline stylesheet
_POPUP_CSS = ""

_TRIGGER_BTN_CSS = f"""
QPushButton {{
    background: #0D1017;
    color: {BRAND_ENGINE};
    font-size: 12px;
    font-weight: 600;
    font-family: 'Segoe UI', -apple-system, sans-serif;
    border: 1px solid {SURFACE_BORDER};
    border-radius: 6px;
    padding: 5px 12px;
    text-align: left;
}}
QPushButton:hover {{
    border-color: {BRAND_FRONTEND};
    background: {SURFACE_PANEL_HOVER};
}}
QPushButton:focus {{
    border-color: {BRAND_ENGINE};
}}
"""

_MODEL_LIST_CSS = f"""
QListWidget {{
    background: transparent;
    border: none;
    outline: none;
}}
QListWidget::item {{
    padding: 6px 10px;
    border-radius: 4px;
    margin: 1px 4px;
    color: {TEXT_PRIMARY};
    font-size: 12px;
    border: none;
}}
QListWidget::item:hover {{
    background: {SURFACE_BORDER};
}}
QListWidget::item:selected {{
    background: rgba(51, 224, 196, 0.15);
    color: {BRAND_ENGINE};
    font-weight: 600;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 6px;
    margin: 2px;
    border: none;
    border-radius: 3px;
}}
QScrollBar::handle:vertical {{
    background: #363C4E;
    min-height: 30px;
    border-radius: 3px;
    border: none;
}}
QScrollBar::handle:vertical:hover {{
    background: #555A6E;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
    background: none;
    border: none;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: none;
    border: none;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 6px;
    margin: 2px;
    border: none;
    border-radius: 3px;
}}
QScrollBar::handle:horizontal {{
    background: #363C4E;
    min-width: 30px;
    border-radius: 3px;
    border: none;
}}
QScrollBar::handle:horizontal:hover {{
    background: #555A6E;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
    background: none;
    border: none;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: none;
    border: none;
}}
QAbstractScrollArea::corner {{
    background: transparent;
}}
"""



class ModelSelectorPopup(QWidget):
    """Frameless popup containing search and provider-grouped models."""
    model_selected = Signal(str, str)  # provider, model
    manage_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        # Style ONLY the popup widget itself using its class name — avoids cascading
        # border/background rules into every child QWidget (labels, scrollbars, etc.)
        self.setStyleSheet(f"ModelSelectorPopup {{ background: {SURFACE_PANEL}; border: 1px solid {SURFACE_BORDER}; border-radius: 8px; }}")
        
        # Data structure: List of dicts (name, display_name, models, has_key)
        self._providers_data: List[Dict] = []
        self._build_ui()
        self.setFocusProxy(self.search_input)

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)
        layout = main_layout

        # 1. Search Bar
        search_layout = QHBoxLayout()
        search_layout.setContentsMargins(4, 0, 4, 4)
        
        search_icon = QLabel("🔍")
        search_icon.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        search_layout.addWidget(search_icon)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search models...")
        self.search_input.setStyleSheet(f"""
            QLineEdit {{
                background: transparent;
                border: none;
                color: {TEXT_PRIMARY};
                font-size: 12px;
                padding: 4px;
            }}
        """)
        self.search_input.textChanged.connect(self._filter_models)
        search_layout.addWidget(self.search_input, 1)
        layout.addLayout(search_layout)

        # Separator
        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setStyleSheet(f"background: {SURFACE_BORDER}; margin: 0 4px;")
        sep1.setFixedHeight(1)
        layout.addWidget(sep1)

        # 2. Model List (List Widget with custom items for headers)
        self.model_list = QListWidget()
        self.model_list.setStyleSheet(_MODEL_LIST_CSS)  # scrollbar rules are now included inside
        self.model_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.model_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.model_list.setMinimumHeight(200)
        self.model_list.setMaximumHeight(350)
        self.model_list.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.model_list)

        # Separator
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"background: {SURFACE_BORDER}; margin: 0 4px;")
        sep2.setFixedHeight(1)
        layout.addWidget(sep2)

        # 3. Manage Models Link
        self.btn_manage = QPushButton("⚙  Manage models")
        self.btn_manage.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_manage.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {TEXT_SECONDARY};
                font-size: 12px;
                text-align: left;
                padding: 8px 12px;
                border: none;
                border-radius: 4px;
            }}
            QPushButton:hover {{
                background: {SURFACE_BORDER};
                color: {TEXT_PRIMARY};
            }}
        """)
        self.btn_manage.clicked.connect(self._on_manage_clicked)
        layout.addWidget(self.btn_manage)

    def set_providers(self, providers: List[Dict]):
        """Store the provider data. Actual list population happens on showEvent."""
        self._providers_data = providers

    def _populate_list(self, filter_text: str = ""):
        self.model_list.clear()
        filter_lower = filter_text.lower()

        section_idx = 0
        for prov in self._providers_data:
            if not prov.get("has_key") or not prov.get("models"):
                continue
                
            prov_name = prov.get("name", "")
            display_name = prov.get("display_name", prov_name)
            models = prov.get("models", [])
            
            # Filter models
            matched_models = [m for m in models if filter_lower in m.lower()]
            if not matched_models and filter_lower not in display_name.lower():
                continue # Skip provider if no matches

            # If subsequent provider, add a clean spacing item
            if section_idx > 0:
                sep_item = QListWidgetItem()
                sep_item.setFlags(Qt.ItemFlag.NoItemFlags)
                sep_item.setSizeHint(QSize(0, 8))
                self.model_list.addItem(sep_item)

            # Header item: ALWAYS uniform, clean, no clipping
            header_item = QListWidgetItem()
            header_item.setFlags(Qt.ItemFlag.NoItemFlags)
            header_item.setSizeHint(QSize(0, 26))
            
            # Create label with clean padding — no vertical overflow clipping
            lbl = QLabel(display_name.upper())
            lbl.setStyleSheet(f"""
                QLabel {{
                    color: {TEXT_MUTED};
                    font-size: 10px;
                    font-weight: 700;
                    letter-spacing: 0.8px;
                    background: transparent;
                    border: none;
                    padding-left: 6px;
                    padding-top: 0px;
                    padding-bottom: 0px;
                }}
            """)
            lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.model_list.addItem(header_item)
            self.model_list.setItemWidget(header_item, lbl)
            section_idx += 1

            # Add models
            for model in matched_models:
                item = QListWidgetItem(model)
                item.setToolTip(model)
                item.setData(Qt.ItemDataRole.UserRole, {"provider": prov_name, "model": model})
                self.model_list.addItem(item)

    def _filter_models(self, text: str):
        self._populate_list(text)

    def _on_item_clicked(self, item: QListWidgetItem):
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            self.model_selected.emit(data["provider"], data["model"])
            self.close()

    def _on_manage_clicked(self):
        self.manage_requested.emit()
        self.close()

    def showEvent(self, event):
        self.search_input.clear()
        self._populate_list()
        self.search_input.setFocus()
        super().showEvent(event)


class ModelSelectorButton(QWidget):
    """Trigger button that manages the ModelSelectorPopup."""
    model_selected = Signal(str, str) # provider, model
    manage_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active_provider = ""
        self._active_model = "Select model"
        self._providers_data: List[Dict] = []
        
        self.popup = ModelSelectorPopup(self)
        self.popup.model_selected.connect(self._on_model_selected)
        self.popup.manage_requested.connect(self.manage_requested)
        
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.btn = QPushButton(f"{self._active_model}  ▾")
        self.btn.setStyleSheet(_TRIGGER_BTN_CSS)
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.clicked.connect(self.toggle_popup)
        layout.addWidget(self.btn)

    def set_active_model(self, provider: str, model: str):
        self._active_provider = provider
        self._active_model = model or "Select model"
        self.btn.setText(f"{self._active_model}  ▾")

    def set_providers(self, providers: List[Dict]):
        self._providers_data = providers
        self.popup.set_providers(providers)

    def toggle_popup(self):
        if self.popup.isVisible():
            self.popup.close()
        else:
            # Ensure popup has a layout pass so sizeHint() is meaningful
            width = max(self.btn.width(), 280)
            self.popup.setFixedWidth(width)
            self.popup.adjustSize()

            popup_h = self.popup.sizeHint().height()

            # Start with: directly below the button
            btn_global = self.btn.mapToGlobal(QPoint(0, 0))
            desired_x = btn_global.x()
            desired_y = btn_global.y() + self.btn.height() + 4

            # Clamp to screen so the popup never spawns off-screen
            screen = QApplication.screenAt(btn_global)
            if screen is None:
                screen = QApplication.primaryScreen()
            if screen:
                sg = screen.availableGeometry()
                # Flip above button if not enough space below
                if desired_y + popup_h > sg.bottom():
                    desired_y = btn_global.y() - popup_h - 4
                # Clamp horizontally
                if desired_x + width > sg.right():
                    desired_x = sg.right() - width
                desired_x = max(desired_x, sg.left())
                desired_y = max(desired_y, sg.top())

            self.popup.move(desired_x, desired_y)
            self.popup.show()


    def _on_model_selected(self, provider: str, model: str):
        self.set_active_model(provider, model)
        self.model_selected.emit(provider, model)

    def setEnabled(self, enabled: bool):
        self.btn.setEnabled(enabled)
        super().setEnabled(enabled)
