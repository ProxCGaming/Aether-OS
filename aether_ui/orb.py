"""Ionized Void Cybernetic Orb Widget.

Renders a high-fidelity ambient orb with cybernetic HUD arc rings, specular highlights,
radial glowing halos, and floating orbital particles using QPainter at 60 FPS.
Matches Figma specifications across all UI states (DISCONNECTED, CONNECTING, CONNECTED, TASK_RUNNING, ERROR).
"""
import math
from PySide6.QtCore import Qt, QTimer, QPointF, QRectF
from PySide6.QtGui import QPainter, QColor, QRadialGradient, QBrush, QPen
from PySide6.QtWidgets import QWidget
from aether_ui.state import UIState
from aether_ui.theme import (
    BRAND_ENGINE,
    BRAND_FRONTEND,
    STATUS_DEGRADED,
    STATUS_HEALTHY,
    STATUS_OFFLINE,
    STATUS_STANDBY,
    SURFACE_BG,
)


class OrbWidget(QWidget):
    """60 FPS cybernetic glowing Orb visualizer with state-driven dynamics and HUD rings."""

    def __init__(self, parent=None, size: int = 190):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.ui_state = UIState.DISCONNECTED
        self.phase = 0.0
        self.ring_rotation = 0.0

        # Orbital particles: (orbit_radius_multiplier, angle_offset, speed_multiplier, size, color_type)
        self._particles = [
            (1.35, 0.8, 0.7, 3.0, "engine"),
            (1.45, 2.4, -0.5, 2.5, "frontend"),
            (1.30, 3.9, 0.9, 3.0, "engine"),
            (1.48, 5.1, -0.8, 2.0, "frontend"),
            (1.38, 1.7, 0.6, 2.5, "engine"),
        ]

        self._timer = QTimer(self)
        self._timer.setInterval(16)  # ~60 FPS
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def stop(self):
        if hasattr(self, "_timer") and self._timer.isActive():
            self._timer.stop()

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)

    def _tick(self):
        if self.ui_state == UIState.TASK_RUNNING:
            speed = 0.05
            rot_speed = 1.2
        elif self.ui_state == UIState.CONNECTING:
            speed = 0.035
            rot_speed = 0.6
        elif self.ui_state == UIState.CONNECTED:
            speed = 0.02
            rot_speed = 0.25
        else:
            speed = 0.012
            rot_speed = 0.08

        self.phase = (self.phase + speed) % (2 * math.pi)
        self.ring_rotation = (self.ring_rotation + rot_speed) % 360.0
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

        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        center = QPointF(cx, cy)
        max_bound = min(w, h) / 2.0

        pulse = math.sin(self.phase)
        base_r = min(w, h) * 0.24

        # -------------------------------------------------------------------
        # 1. State: DISCONNECTED (Figma Page 3)
        # Desaturated to standby (#6B6F7A), 40% opacity, no rings
        # -------------------------------------------------------------------
        if self.ui_state == UIState.DISCONNECTED:
            core_r = base_r
            halo_r = core_r * 1.3
            # Dim soft halo
            hg = QRadialGradient(center, halo_r)
            hg.setColorAt(0.0, QColor(107, 111, 122, 35))
            hg.setColorAt(0.7, QColor(107, 111, 122, 10))
            hg.setColorAt(1.0, QColor(11, 13, 18, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(hg))
            p.drawEllipse(center, halo_r, halo_r)

            # Flat dormant sphere
            bg = QRadialGradient(center - QPointF(core_r * 0.2, core_r * 0.2), core_r * 1.1)
            bg.setColorAt(0.0, QColor(107, 111, 122, 85))
            bg.setColorAt(0.7, QColor(50, 54, 65, 95))
            bg.setColorAt(1.0, QColor(25, 28, 36, 110))
            p.setBrush(QBrush(bg))
            p.drawEllipse(center, core_r, core_r)
            return

        # -------------------------------------------------------------------
        # 2. State: ERROR (Figma Page 7)
        # Gradient replaced entirely with offline red (#F2415B), intense halo, no rings
        # -------------------------------------------------------------------
        if self.ui_state == UIState.ERROR:
            core_r = base_r * (1.0 + pulse * 0.04)
            halo_r = min(max_bound - 2, core_r * 1.7)

            # Red Halo
            hg = QRadialGradient(center, halo_r)
            hg.setColorAt(0.0, QColor(242, 65, 91, 120))
            hg.setColorAt(0.5, QColor(242, 65, 91, 40))
            hg.setColorAt(0.85, QColor(242, 65, 91, 8))
            hg.setColorAt(1.0, QColor(11, 13, 18, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(hg))
            p.drawEllipse(center, halo_r, halo_r)

            # Core Red Sphere
            bg = QRadialGradient(center - QPointF(core_r * 0.25, core_r * 0.25), core_r * 1.2)
            bg.setColorAt(0.0, QColor(255, 95, 120, 255))
            bg.setColorAt(0.5, QColor(242, 65, 91, 240))
            bg.setColorAt(1.0, QColor(140, 20, 40, 240))
            p.setBrush(QBrush(bg))
            p.drawEllipse(center, core_r, core_r)

            # Specular highlight
            sc = center - QPointF(core_r * 0.35, core_r * 0.35)
            sr = core_r * 0.38
            sg = QRadialGradient(sc, sr)
            sg.setColorAt(0.0, QColor(255, 200, 210, 180))
            sg.setColorAt(0.6, QColor(255, 120, 140, 40))
            sg.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.setBrush(QBrush(sg))
            p.drawEllipse(sc, sr, sr)
            return

        # -------------------------------------------------------------------
        # 3. State: CONNECTING (Figma Page 4)
        # Pulsing degraded tone / cool tone at low opacity with subtle animated arcs
        # -------------------------------------------------------------------
        if self.ui_state == UIState.CONNECTING:
            core_r = base_r * (0.96 + pulse * 0.06)
            halo_r = min(max_bound - 2, core_r * 1.6)

            # Amber/Cool Halo
            hg = QRadialGradient(center, halo_r)
            hg.setColorAt(0.0, QColor(242, 169, 59, 80))
            hg.setColorAt(0.5, QColor(124, 111, 255, 25))
            hg.setColorAt(0.85, QColor(124, 111, 255, 5))
            hg.setColorAt(1.0, QColor(11, 13, 18, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(hg))
            p.drawEllipse(center, halo_r, halo_r)

            # Outer subtle rotating arcs
            self._draw_hud_arcs(p, center, core_r, opacity=0.45, is_active=False)

            # Body
            bg = QRadialGradient(center - QPointF(core_r * 0.2, core_r * 0.2), core_r * 1.2)
            bg.setColorAt(0.0, QColor(160, 180, 240, 200))
            bg.setColorAt(0.6, QColor(110, 130, 190, 160))
            bg.setColorAt(1.0, QColor(40, 50, 80, 180))
            p.setBrush(QBrush(bg))
            p.drawEllipse(center, core_r, core_r)

            # Specular
            sc = center - QPointF(core_r * 0.35, core_r * 0.35)
            sr = core_r * 0.35
            sg = QRadialGradient(sc, sr)
            sg.setColorAt(0.0, QColor(255, 255, 255, 140))
            sg.setColorAt(0.7, QColor(255, 255, 255, 0))
            p.setBrush(QBrush(sg))
            p.drawEllipse(sc, sr, sr)

            self._draw_particles(p, center, core_r, opacity=0.5)
            return

        # -------------------------------------------------------------------
        # 4 & 5. States: CONNECTED & TASK_RUNNING (Figma Pages 5 & 6)
        # Smoothly blended halo fading cleanly to 0 alpha well inside bounding box
        # -------------------------------------------------------------------
        is_running = (self.ui_state == UIState.TASK_RUNNING)
        pulse_amp = 0.08 if is_running else 0.04
        core_r = base_r * (1.0 + pulse * pulse_amp)
        halo_r = max_bound - 4  # Clamped to stay perfectly within the widget bounds

        # Soft atmospheric radial glow fading to transparent
        hg = QRadialGradient(center, halo_r)
        halo_alpha = 110 if is_running else 75
        hg.setColorAt(0.0, QColor(51, 224, 196, halo_alpha))
        hg.setColorAt(0.35, QColor(124, 111, 255, int(halo_alpha * 0.6)))
        hg.setColorAt(0.70, QColor(124, 111, 255, int(halo_alpha * 0.2)))
        hg.setColorAt(0.92, QColor(11, 13, 18, 5))
        hg.setColorAt(1.0, QColor(11, 13, 18, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(hg))
        p.drawEllipse(center, halo_r, halo_r)

        # Outer HUD Cybernetic Arcs
        arc_opacity = 0.95 if is_running else 0.8
        self._draw_hud_arcs(p, center, core_r, opacity=arc_opacity, is_active=is_running)

        # Orb Sphere Body (Vibrant multi-stop Ionized Void gradient)
        offset = QPointF(core_r * 0.22, core_r * 0.22)
        bg = QRadialGradient(center - offset, core_r * 1.25)
        # Center glow: electric cyan #33E0C4 -> lavender/violet #7C6FFF -> deep void border
        bg.setColorAt(0.0, QColor(51, 224, 196, 255))
        bg.setColorAt(0.35, QColor(100, 150, 255, 255))
        bg.setColorAt(0.70, QColor(124, 111, 255, 240))
        bg.setColorAt(0.95, QColor(25, 20, 65, 250))
        bg.setColorAt(1.0, QColor(11, 13, 18, 255))
        p.setBrush(QBrush(bg))
        p.drawEllipse(center, core_r, core_r)

        # Crisp Inner Rim
        p.setPen(QPen(QColor(51, 224, 196, 80), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(center, core_r - 0.5, core_r - 0.5)

        # Specular Highlight (soft luminous white glint)
        sc = center - QPointF(core_r * 0.32, core_r * 0.32)
        sr = core_r * 0.36
        sg = QRadialGradient(sc, sr)
        sg.setColorAt(0.0, QColor(255, 255, 255, 220))
        sg.setColorAt(0.5, QColor(230, 235, 255, 90))
        sg.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(sg))
        p.drawEllipse(sc, sr, sr)

        # Floating Orbital Glow Particles
        self._draw_particles(p, center, core_r, opacity=1.0)

    def _draw_hud_arcs(self, p: QPainter, center: QPointF, core_r: float, opacity: float, is_active: bool):
        """Draws concentric cybernetic arcs scaled cleanly inside widget bounds."""
        rot = self.ring_rotation
        p.save()
        p.translate(center)

        # Inner Arc 1 (Cyan #33E0C4)
        r1 = core_r * 1.16
        pen1 = QPen(QColor(51, 224, 196, int(210 * opacity)), 1.8)
        pen1.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen1)
        p.setBrush(Qt.BrushStyle.NoBrush)
        rect1 = QRectF(-r1, -r1, r1 * 2, r1 * 2)
        p.drawArc(rect1, int((rot * 16)), int(140 * 16))
        p.drawArc(rect1, int(((rot + 190) * 16)), int(70 * 16))

        # Outer Arc 2 (Frontend Violet #7C6FFF)
        r2 = core_r * 1.30
        pen2 = QPen(QColor(124, 111, 255, int(190 * opacity)), 2.0)
        pen2.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen2)
        rect2 = QRectF(-r2, -r2, r2 * 2, r2 * 2)
        p.drawArc(rect2, int(((-rot * 0.8 + 60) * 16)), int(110 * 16))
        p.drawArc(rect2, int(((-rot * 0.8 + 220) * 16)), int(50 * 16))

        # Dynamic Running Sweep Arc (Figma Page 6)
        if is_active:
            r3 = core_r * (1.44 + math.sin(self.phase * 2) * 0.04)
            pen3 = QPen(QColor(51, 224, 196, int(230 * opacity)), 2.4)
            pen3.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen3)
            rect3 = QRectF(-r3, -r3, r3 * 2, r3 * 2)
            p.drawArc(rect3, int(((rot * 2.0) * 16)), int(150 * 16))

        p.restore()

    def _draw_particles(self, p: QPainter, center: QPointF, core_r: float, opacity: float):
        """Draws floating orbital particles revolving smoothly."""
        p.setPen(Qt.PenStyle.NoPen)
        for mult, angle_offset, speed_mult, size, col_type in self._particles:
            ang = self.phase * speed_mult + angle_offset
            dist = core_r * mult
            px = center.x() + math.cos(ang) * dist
            py = center.y() + math.sin(ang) * dist

            if col_type == "engine":
                col = QColor(51, 224, 196, int(210 * opacity))
            else:
                col = QColor(124, 111, 255, int(210 * opacity))

            p.setBrush(QBrush(col))
            p.drawEllipse(QPointF(px, py), size / 2.0, size / 2.0)
