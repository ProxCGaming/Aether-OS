"""Routing transparency and fallback alert banner widget."""
from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget


class RoutingBanner(QWidget):
    """Compact banner displaying real-time routing decisions and fallback notices."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedHeight(28)
        self.setVisible(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 2, 10, 2)
        layout.setSpacing(8)

        self.icon = QLabel("⚡")
        self.icon.setStyleSheet("font-size:12px; color:#58A6FF;")
        layout.addWidget(self.icon)

        self.lbl_text = QLabel("")
        self.lbl_text.setStyleSheet("font-size:11px; color:#C9D1D9; font-weight:500;")
        layout.addWidget(self.lbl_text, 1)

    def set_decision(
        self,
        model_label: str,
        reason: str,
        is_fallback: bool = False,
    ):
        if is_fallback:
            self.icon.setText("🔀")
            self.icon.setStyleSheet("font-size:12px; color:#FFA657;")
            self.lbl_text.setText(f"Fallback: {reason}")
            self.setStyleSheet("""
                RoutingBanner {
                    background-color: rgba(56, 35, 12, 0.7);
                    border: 1px solid rgba(210, 153, 34, 0.4);
                    border-radius: 6px;
                }
            """)
        else:
            self.icon.setText("⚡")
            self.icon.setStyleSheet("font-size:12px; color:#58A6FF;")
            self.lbl_text.setText(f"{reason}")
            self.setStyleSheet("""
                RoutingBanner {
                    background-color: rgba(22, 27, 34, 0.85);
                    border: 1px solid rgba(88, 166, 255, 0.25);
                    border-radius: 6px;
                }
            """)
        self.setVisible(True)

    def clear_banner(self):
        self.lbl_text.setText("")
        self.setVisible(False)
