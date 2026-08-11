"""HUD Panel: Status header, model selector, prompt bar, and live streaming event log."""
import datetime
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from aether_common.contracts import Event, EventType
from aether_ui.state import UIState, UIStateMachine
from aether_ui.widgets.provider_health_badge import ProviderHealthBadge
from aether_ui.widgets.routing_banner import RoutingBanner

_BADGE = {
    UIState.DISCONNECTED: ("#382020", "#F85149"),
    UIState.CONNECTING:   ("#382E10", "#E3B341"),
    UIState.CONNECTED:    ("#153022", "#3FB950"),
    UIState.TASK_RUNNING: ("#16243B", "#58A6FF"),
    UIState.ERROR:        ("#382020", "#F85149"),
}

_BTN_CSS = """QPushButton {{
    background-color: {bg}; color: #FFFFFF; font-weight: bold;
    border: none; border-radius: 6px; padding: 6px 14px; font-size: 12px;
}}
QPushButton:hover {{ background-color: {hover}; }}
QPushButton:disabled {{ background-color: #21262D; color: #484F58; }}"""

_INPUT_CSS = """QLineEdit {
    background: #0D1117; color: #E6EDF3; border: 1px solid #30363D;
    border-radius: 6px; padding: 6px 10px; font-size: 12px;
}
QLineEdit:focus { border: 1px solid #58A6FF; }"""

_COMBO_CSS = """QComboBox {
    background: #0D1117; color: #C9D1D9; border: 1px solid #30363D;
    border-radius: 6px; padding: 4px 8px; font-size: 11px; min-width: 140px;
}
QComboBox:hover { border-color: #58A6FF; }
QComboBox::drop-down { border: none; }
QComboBox QAbstractItemView {
    background: #161B22; color: #C9D1D9; selection-background-color: #1F6FEB;
    border: 1px solid #30363D;
}"""

_MODEL_INDICATOR_CSS = """QLabel {
    font-size: 11px; font-weight: 600; color: #38BDF8;
    background: #0F172A; border-radius: 4px; padding: 3px 8px;
}"""


class HudWidget(QWidget):
    start_task_requested = Signal(str, str)  # (prompt, model)
    cancel_task_requested = Signal()
    model_changed_by_user = Signal(str)  # model name

    def __init__(self, state_machine: UIStateMachine, parent=None):
        super().__init__(parent)
        self.sm = state_machine
        self.heartbeat = 0
        self._suppress_model_signal = False
        self._build_ui()

        self._hb_timer = QTimer(self)
        self._hb_timer.setInterval(1000)
        self._hb_timer.timeout.connect(self._tick)
        self._hb_timer.start()

        self.sm.subscribe(self._on_state)
        self._on_state(self.sm)

    def _tick(self):
        self.heartbeat += 1
        self.lbl_hb.setText(f"Heartbeat: #{self.heartbeat}")

    def _build_ui(self):
        lo = QVBoxLayout(self)
        lo.setContentsMargins(16, 12, 16, 16)
        lo.setSpacing(10)

        # Header: status + health badge + active model indicator + heartbeat
        hdr = QHBoxLayout()
        self.lbl_status = QLabel("DISCONNECTED")
        self.lbl_status.setStyleSheet(self._badge_css(UIState.DISCONNECTED))
        hdr.addWidget(self.lbl_status)

        self.health_badge = ProviderHealthBadge()
        hdr.addWidget(self.health_badge)

        self.lbl_model = QLabel("")
        self.lbl_model.setStyleSheet(_MODEL_INDICATOR_CSS)
        self.lbl_model.setVisible(False)
        hdr.addWidget(self.lbl_model)

        hdr.addStretch()
        self.lbl_hb = QLabel("Heartbeat: #0")
        self.lbl_hb.setStyleSheet("font-size:11px; color:#718096; font-family:monospace;")
        hdr.addWidget(self.lbl_hb)
        lo.addLayout(hdr)

        # Input Row: Model dropdown + Prompt input + Send + Cancel
        ilo = QHBoxLayout()
        ilo.setSpacing(8)

        self.cmb_model = QComboBox()
        self.cmb_model.setStyleSheet(_COMBO_CSS)
        self.cmb_model.setToolTip("Active model")
        self.cmb_model.currentTextChanged.connect(self._on_model_changed)
        ilo.addWidget(self.cmb_model)

        self.inp_prompt = QLineEdit()
        self.inp_prompt.setPlaceholderText("Ask AETHER (e.g. 'What time is it?')…")
        self.inp_prompt.setStyleSheet(_INPUT_CSS)
        self.inp_prompt.returnPressed.connect(self._on_send)
        ilo.addWidget(self.inp_prompt, 1)

        self.btn_send = self._make_btn("Send", "#238636", "#2EA043")
        self.btn_send.clicked.connect(self._on_send)
        ilo.addWidget(self.btn_send)

        self.btn_cancel = self._make_btn("Cancel", "#DA3633", "#B62324")
        self.btn_cancel.clicked.connect(self.cancel_task_requested.emit)
        ilo.addWidget(self.btn_cancel)
        lo.addLayout(ilo)

        # Routing transparency banner
        self.routing_banner = RoutingBanner()
        lo.addWidget(self.routing_banner)

        # Event log
        lbl_log = QLabel("Event Stream:")
        lbl_log.setStyleSheet("font-size:11px; color:#8B949E; font-weight:600;")
        lo.addWidget(lbl_log)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.log.setMinimumHeight(120)
        self.log.setStyleSheet("""QTextEdit {
            background:#0D1117; color:#58A6FF; font-family:Consolas,monospace;
            font-size:11px; border:1px solid #30363D; border-radius:6px; padding:8px;}""")
        lo.addWidget(self.log, 1)

    def _make_btn(self, text, bg, hover):
        btn = QPushButton(text)
        btn.setFixedHeight(32)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(_BTN_CSS.format(bg=bg, hover=hover))
        return btn

    def _on_send(self):
        text = self.inp_prompt.text().strip() or "Say hello"
        model = self.cmb_model.currentText()
        self.sm.clear_error()
        self.start_task_requested.emit(text, model)

    def _on_model_changed(self, model: str):
        if not self._suppress_model_signal and model:
            self.model_changed_by_user.emit(model)

    @staticmethod
    def _badge_css(state: UIState) -> str:
        bg, fg = _BADGE.get(state, ("#2D3748", "#A0AEC0"))
        return (
            f"QLabel {{ font-weight:bold; font-size:12px; padding:4px 10px; "
            f"border-radius:6px; background:{bg}; color:{fg}; }}"
        )

    def set_active_model(self, provider_display: str, model: str):
        """Update the HUD model indicator and dropdown from external source."""
        self.lbl_model.setText(f"{model}")
        self.lbl_model.setVisible(bool(model))
        # Sync dropdown without re-emitting signal
        self._suppress_model_signal = True
        idx = self.cmb_model.findText(model)
        if idx >= 0:
            self.cmb_model.setCurrentIndex(idx)
        self._suppress_model_signal = False

    def set_available_models(self, models: list):
        """Populate the model dropdown from provider config."""
        self._suppress_model_signal = True
        current = self.cmb_model.currentText()
        self.cmb_model.clear()
        self.cmb_model.addItems(models)
        idx = self.cmb_model.findText(current)
        if idx >= 0:
            self.cmb_model.setCurrentIndex(idx)
        self._suppress_model_signal = False

    def log_event(self, event: Event):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        p = event.payload or {}
        t = event.type

        if t == EventType.START_TASK:
            m = p.get("model", "")
            m_str = f" [{m}]" if m else ""
            self.log.append(f'<span style="color:#7EE787;">[{ts}] <b>&gt; Prompt{m_str}:</b> {p.get("prompt")}</span>')
        elif t == EventType.ROUTING_DECISION:
            reason = p.get("reason", "")
            model = p.get("model", "")
            self.routing_banner.set_decision(model_label=model, reason=reason)
            self.log.append(f'<span style="color:#58A6FF;">[{ts}] <b>⚡ Routing:</b> {reason}</span>')
        elif t == EventType.FALLBACK_STARTED:
            reason = p.get("reason", "")
            to_model = p.get("to_model", "")
            self.routing_banner.set_decision(model_label=to_model, reason=reason, is_fallback=True)
            self.log.append(f'<span style="color:#FFA657;">[{ts}] <b>🔀 Fallback:</b> {reason}</span>')
        elif t == EventType.FALLBACK_COMPLETED:
            prov = p.get("provider", "")
            mod = p.get("model", "")
            self.log.append(f'<span style="color:#56D364;">[{ts}] <b>✔ Fallback Succeeded:</b> Completed with {prov} ({mod})</span>')
        elif t == EventType.FALLBACK_FAILED:
            self.log.append(f'<span style="color:#F85149;">[{ts}] <b>✖ Fallback Failed:</b> {p.get("error")}</span>')
        elif t == EventType.PROVIDER_HEALTH_UPDATE:
            health_map = p.get("health", {})
            current_model = self.cmb_model.currentText()
            # Find matching provider or default
            for pname, hinfo in health_map.items():
                if pname == "google_gemini":
                    self.health_badge.update_health(
                        status=hinfo.get("status", "healthy"),
                        latency_ms=hinfo.get("latency_ms", 0.0),
                        provider_name="Google Gemini",
                        last_error=hinfo.get("last_error"),
                    )
                    break
        elif t == EventType.TASK_CREATED:
            self.log.append(f'<span style="color:#D2A8FF;">[{ts}] <b>Task Created:</b> {p.get("task_id", "")[:8]}</span>')
        elif t == EventType.TASK_PROGRESS:
            if "tool_call" in p:
                self.log.append(f'<span style="color:#FFA657;">[{ts}] <b>⚡ Tool Call:</b> {p["tool_call"]}({p.get("args", {})})</span>')
            elif "tool_result" in p:
                self.log.append(f'<span style="color:#79C0FF;">[{ts}] <b>➔ Tool Result:</b> {p.get("result")}</span>')
            elif "text_delta" in p:
                import markdown
                import re
                text = p["text_delta"]
                # Format <think> tags to a nice block
                text = re.sub(
                    r'<think>(.*?)</think>', 
                    r'<div style="color: #8B949E; border-left: 2px solid #30363D; padding-left: 10px; margin-bottom: 10px; margin-top: 10px;"><i>🤔 Thinking...<br/>\1</i></div>', 
                    text, 
                    flags=re.DOTALL
                )
                # Fallback for unclosed <think> tag
                text = re.sub(
                    r'<think>(.*)$', 
                    r'<div style="color: #8B949E; border-left: 2px solid #30363D; padding-left: 10px; margin-bottom: 10px; margin-top: 10px;"><i>🤔 Thinking...<br/>\1</i></div>', 
                    text, 
                    flags=re.DOTALL
                )
                html = markdown.markdown(text, extensions=['fenced_code', 'tables'])
                self.log.append(f'<div style="color:#C9D1D9; margin-top: 5px;">{html}</div>')
                self.log.ensureCursorVisible()
        elif t == EventType.TASK_COMPLETED:
            resp = p.get("response", "")
            self.log.append(f'\n<span style="color:#56D364;">[{ts}] <b>✔ Completed:</b> {resp}</span>')
        elif t == EventType.TASK_CANCELLED:
            self.log.append(f'\n<span style="color:#F0883E;">[{ts}] <b>⊘ Cancelled</b></span>')
        elif t == EventType.TASK_FAILED:
            self.log.append(f'\n<span style="color:#F85149;">[{ts}] <b>✖ Failed:</b> {p.get("error")}</span>')
        elif t == EventType.HELLO:
            self.log.append(f'<span style="color:#58A6FF;">[{ts}] <b>Engine Ready:</b> {p.get("message")}</span>')
            if p.get("active_model"):
                self.set_active_model(p.get("active_provider", ""), p["active_model"])
        elif t == EventType.TOOL_APPROVAL_REQUEST:
            self.log.append(f'<span style="color:#E3B341;">[{ts}] <b>⚠ Approval Required:</b> {p.get("tool_name", "tool")} {p.get("args", {})}</span>')
        elif t in (EventType.TOOL_APPROVAL_GRANTED, EventType.TOOL_APPROVAL_REJECTED):
            self.log.append(f'<span style="color:#58A6FF;">[{ts}] <b>Consent:</b> {t.value}</span>')
        elif t == EventType.WORKER_EXECUTION_STARTED:
            self.log.append(f'<span style="color:#38BDF8;">[{ts}] <b>Worker:</b> execution started</span>')
        elif t == EventType.WORKER_EXECUTION_COMPLETED:
            self.log.append(f'<span style="color:#56D364;">[{ts}] <b>Worker:</b> execution completed</span>')
        elif t == EventType.ERROR:
            self.log.append(f'<span style="color:#F85149;">[{ts}] <b>Error:</b> {p.get("error")}</span>')

    def _on_state(self, sm: UIStateMachine):
        self.lbl_status.setText(sm.state.value)
        self.lbl_status.setStyleSheet(self._badge_css(sm.state))

        can_start = sm.state in (UIState.CONNECTED, UIState.ERROR)
        can_cancel = sm.state == UIState.TASK_RUNNING
        self.btn_send.setEnabled(can_start)
        self.inp_prompt.setEnabled(can_start)
        self.cmb_model.setEnabled(can_start)
        self.btn_cancel.setEnabled(can_cancel)
