import math
from PySide6.QtCore import Qt, QTimer, QPointF
from PySide6.QtGui import QPainter, QColor, QRadialGradient, QBrush
from PySide6.QtWidgets import QWidget
from aether_ui.state import UIState

# Color palettes: (core, mid, halo) per state
_PALETTES = {
    UIState.DISCONNECTED: ((80,90,110,220),  (50,60,80,160),   (40,50,70,60)),
    UIState.CONNECTING:   ((245,200,60,240), (215,140,25,180), (230,160,30,70)),
    UIState.CONNECTED:    ((0,240,255,240),  (0,150,220,180),  (0,180,255,70)),
    UIState.TASK_RUNNING: ((230,60,180,255), (140,40,220,200), (180,50,240,80)),
    UIState.ERROR:        ((240,60,60,240),  (180,30,30,180),  (220,40,40,70)),
}


class OrbWidget(QWidget):
    """Pulsing ambient orb rendered with QPainter. Independent 30 FPS QTimer."""

    def __init__(self, parent=None, size: int = 180):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.ui_state = UIState.DISCONNECTED
        self.phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self):
        speed = 0.08 if self.ui_state == UIState.TASK_RUNNING else 0.04
        self.phase = (self.phase + speed) % (2 * math.pi)
        self.update()

    def set_state(self, state: UIState):
        if isinstance(state, str):
            try:
                state = UIState(state)
            except ValueError:
                pass
        self.ui_state = state
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        cx, cy = self.width() / 2, self.height() / 2
        center = QPointF(cx, cy)
        base_r = min(self.width(), self.height()) * 0.32
        pulse = math.sin(self.phase) * 0.12
        core_r = base_r * (1.0 + pulse * 0.5)
        halo_r = base_r * (1.6 + pulse * 0.8)

        pal = _PALETTES.get(self.ui_state, _PALETTES[UIState.DISCONNECTED])
        core_c, mid_c, halo_c = (QColor(*c) for c in pal)

        # Halo glow
        hg = QRadialGradient(center, halo_r)
        hg.setColorAt(0.0, halo_c)
        hg.setColorAt(0.6, QColor(halo_c.red(), halo_c.green(), halo_c.blue(), int(halo_c.alpha() * 0.4)))
        hg.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(hg))
        p.drawEllipse(center, halo_r, halo_r)

        # Sphere body
        offset = QPointF(core_r * 0.25, core_r * 0.25)
        bg = QRadialGradient(center - offset, core_r * 1.2)
        bg.setColorAt(0.0, core_c)
        bg.setColorAt(0.65, mid_c)
        bg.setColorAt(1.0, QColor(core_c.red() // 4, core_c.green() // 4, core_c.blue() // 4, 220))
        p.setBrush(QBrush(bg))
        p.drawEllipse(center, core_r, core_r)

        # Specular highlight
        sc = center - QPointF(core_r * 0.35, core_r * 0.35)
        sr = core_r * 0.4
        sg = QRadialGradient(sc, sr)
        sg.setColorAt(0.0, QColor(255, 255, 255, 180))
        sg.setColorAt(0.5, QColor(255, 255, 255, 60))
        sg.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(sg))
        p.drawEllipse(sc, sr, sr)
