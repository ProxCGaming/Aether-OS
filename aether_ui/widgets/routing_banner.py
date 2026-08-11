"""Routing transparency and fallback alert banner widget matching Figma Page 9."""
from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget
from aether_ui.theme import (
    BRAND_FRONTEND,
    STATUS_DEGRADED,
    SURFACE_BORDER,
    SURFACE_PANEL,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)


class RoutingBanner(QWidget):
    """Card banner displaying real-time routing decisions and fallback notices."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)

        self.lbl_tag = QLabel("ROUTING")
        self.lbl_tag.setStyleSheet(
            f"font-size: 10px; font-weight: 700; color: {BRAND_FRONTEND}; "
            "letter-spacing: 0.5px; font-family: 'Segoe UI', sans-serif;"
        )
        layout.addWidget(self.lbl_tag)

        self.lbl_text = QLabel("")
        self.lbl_text.setWordWrap(True)
        self.lbl_text.setStyleSheet(
            f"font-size: 12px; color: {TEXT_PRIMARY}; font-weight: 400; line-height: 1.4; "
            "font-family: 'Segoe UI', sans-serif;"
        )
        layout.addWidget(self.lbl_text)

        self._apply_normal_style()

    def _apply_normal_style(self):
        self.setStyleSheet(f"""
            RoutingBanner {{
                background-color: {SURFACE_PANEL};
                border: 1px solid {BRAND_FRONTEND}40;
                border-radius: 8px;
            }}
        """)

    def _apply_fallback_style(self):
        self.setStyleSheet(f"""
            RoutingBanner {{
                background-color: #1F1710;
                border: 1px solid {STATUS_DEGRADED}60;
                border-radius: 8px;
            }}
        """)

    def set_decision(
        self,
        model_label: str,
        reason: str,
        is_fallback: bool = False,
    ):
        if is_fallback:
            self.lbl_tag.setText("FALLBACK ROUTING")
            self.lbl_tag.setStyleSheet(
                f"font-size: 10px; font-weight: 700; color: {STATUS_DEGRADED}; "
                "letter-spacing: 0.5px; font-family: 'Segoe UI', sans-serif;"
            )
            self.lbl_text.setText(f"Fallback to {model_label} — {reason}")
            self._apply_fallback_style()
        else:
            self.lbl_tag.setText("ROUTING")
            self.lbl_tag.setStyleSheet(
                f"font-size: 10px; font-weight: 700; color: {BRAND_FRONTEND}; "
                "letter-spacing: 0.5px; font-family: 'Segoe UI', sans-serif;"
            )
            if model_label and not reason.startswith(f"Routed to {model_label}"):
                self.lbl_text.setText(f"Routed to {model_label} — {reason}")
            else:
                self.lbl_text.setText(reason)
            self._apply_normal_style()

        self.setVisible(True)

    def clear_banner(self):
        self.lbl_text.setText("")
        self.setVisible(False)
