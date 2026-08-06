"""Provider health status badge widget."""
from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

_STATUS_CONFIG = {
    "healthy": {"color": "#3FB950", "bg": "#13231B", "border": "#238636", "label": "Healthy"},
    "degraded": {"color": "#D29922", "bg": "#2A1E0E", "border": "#9E6A03", "label": "Degraded"},
    "offline": {"color": "#F85149", "bg": "#281518", "border": "#DA3633", "label": "Offline"},
    "unknown": {"color": "#8B949E", "bg": "#161B22", "border": "#30363D", "label": "Standby"},
}


class ProviderHealthBadge(QWidget):
    """Real-time provider health status pill with latency indicator."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedHeight(24)
        self._status = "unknown"
        self._latency = 0.0

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(6)

        self.dot = QLabel("●")
        self.dot.setStyleSheet("font-size:10px; color:#8B949E;")
        layout.addWidget(self.dot)

        self.lbl_text = QLabel("Standby")
        self.lbl_text.setStyleSheet("font-size:11px; font-weight:600; color:#8B949E;")
        layout.addWidget(self.lbl_text)

        self.update_health("unknown", 0.0)

    def update_health(
        self,
        status: str,
        latency_ms: float = 0.0,
        provider_name: Optional[str] = None,
        last_error: Optional[str] = None,
    ):
        self._status = status.lower() if status else "unknown"
        self._latency = latency_ms

        cfg = _STATUS_CONFIG.get(self._status, _STATUS_CONFIG["unknown"])
        color = cfg["color"]
        bg = cfg["bg"]
        border = cfg["border"]
        label_text = cfg["label"]

        if self._latency > 0 and self._status == "healthy":
            label_text = f"Healthy ({int(self._latency)}ms)"

        self.dot.setStyleSheet(f"font-size:10px; color:{color};")
        self.lbl_text.setText(label_text)
        self.lbl_text.setStyleSheet(f"font-size:11px; font-weight:600; color:{color};")

        self.setStyleSheet(f"""
            ProviderHealthBadge {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 12px;
            }}
        """)

        tip_lines = [f"Status: {cfg['label']}"]
        if provider_name:
            tip_lines.insert(0, f"Provider: {provider_name}")
        if self._latency > 0:
            tip_lines.append(f"Latency: {self._latency:.1f} ms")
        if last_error:
            tip_lines.append(f"Last Error: {last_error}")
        self.setToolTip("\n".join(tip_lines))
