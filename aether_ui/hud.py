"""HUD Panel: Status header, model selector, prompt bar, and live streaming event log.

Styled according to Figma Direction A ('Ionized Void').
"""
import datetime
from typing import Optional
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
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
from aether_ui.theme import (
    BRAND_ENGINE,
    BRAND_FRONTEND,
    CHECKBOX_CSS,
    INPUT_CSS,
    SCROLLBAR_CSS,
    STATUS_DEGRADED,
    STATUS_HEALTHY,
    STATUS_OFFLINE,
    STATUS_STANDBY,
    SURFACE_BG,
    SURFACE_BORDER,
    SURFACE_PANEL,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)
from aether_ui.widgets.provider_health_badge import ProviderHealthBadge
from aether_ui.widgets.routing_banner import RoutingBanner
from aether_ui.components.model_selector import ModelSelectorButton

_BADGE_COLORS = {
    UIState.DISCONNECTED: ("#1A1D26", STATUS_STANDBY),
    UIState.CONNECTING:   ("#292110", STATUS_DEGRADED),
    UIState.CONNECTED:    ("#0E261E", STATUS_HEALTHY),
    UIState.TASK_RUNNING: ("#1D1B36", BRAND_FRONTEND),
    UIState.ERROR:        ("#291217", STATUS_OFFLINE),
}

_BTN_SEND_CSS = f"""
QPushButton {{
    background-color: {STATUS_HEALTHY};
    color: #07150E;
    font-weight: 700;
    font-size: 12px;
    font-family: 'Segoe UI', sans-serif;
    border: none;
    border-radius: 6px;
    padding: 6px 16px;
}}
QPushButton:hover {{
    background-color: #38E5AC;
}}
QPushButton:disabled {{
    background-color: #1A2320;
    color: #3B5249;
}}
"""

_BTN_CANCEL_CSS = f"""
QPushButton {{
    background-color: {STATUS_OFFLINE};
    color: #FFFFFF;
    font-weight: 700;
    font-size: 12px;
    font-family: 'Segoe UI', sans-serif;
    border: none;
    border-radius: 6px;
    padding: 6px 14px;
}}
QPushButton:hover {{
    background-color: #FA5870;
}}
QPushButton:disabled {{
    background-color: #241418;
    color: #523138;
}}
"""

_MODEL_INDICATOR_CSS = f"""
QLabel {{
    font-size: 11px;
    font-weight: 600;
    color: {BRAND_ENGINE};
    background: #0E1A20;
    border: 1px solid {BRAND_ENGINE}40;
    border-radius: 4px;
    padding: 3px 8px;
}}
"""


def _format_provider_name(provider: Optional[str], sm=None) -> str:
    if not provider:
        return "Active Provider"
    
    if sm and hasattr(sm, "providers"):
        for p in sm.providers:
            if p.get("name") == provider:
                disp = p.get("display_name")
                if disp:
                    return disp

    mapping = {
        "google_gemini": "Google Gemini",
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        "groq": "Groq",
        "mistral": "Mistral",
        "deepseek": "DeepSeek",
        "openrouter": "OpenRouter",
        "ollama": "Ollama (Local)",
    }
    return mapping.get(provider.lower(), provider.replace("_", " ").title())


class HudWidget(QWidget):
    start_task_requested = Signal(str, str)  # (prompt, model)
    cancel_task_requested = Signal()
    model_changed_by_user = Signal(str, str)  # provider, model name
    manage_models_requested = Signal()

    def __init__(self, state_machine: UIStateMachine, parent=None):
        super().__init__(parent)
        self.sm = state_machine
        self.heartbeat = 0
        self._suppress_model_signal = False
        self._active_provider = "google_gemini"
        self._active_model_name = ""
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
        lo.setContentsMargins(16, 10, 16, 16)
        lo.setSpacing(10)

        # Header row: Status pill + Health badge + Model pill + Heartbeat
        hdr = QHBoxLayout()
        hdr.setSpacing(8)

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
        self.lbl_hb.setStyleSheet(f"font-size:11px; color:{TEXT_MUTED}; font-family:Consolas, monospace;")
        hdr.addWidget(self.lbl_hb)
        lo.addLayout(hdr)

        # Input Row: Model dropdown + Prompt line + Send + Cancel
        ilo = QHBoxLayout()
        ilo.setSpacing(8)

        self.cmb_model = ModelSelectorButton()
        self.cmb_model.setToolTip("Active model")
        self.cmb_model.model_selected.connect(self._on_model_changed)
        self.cmb_model.manage_requested.connect(self.manage_models_requested.emit)
        ilo.addWidget(self.cmb_model)

        self.inp_prompt = QLineEdit()
        self.inp_prompt.setPlaceholderText("Ask AETHER (e.g. 'Build a FastAPI endpoint')…")
        self.inp_prompt.setStyleSheet(INPUT_CSS)
        self.inp_prompt.returnPressed.connect(self._on_send)
        ilo.addWidget(self.inp_prompt, 1)

        self.btn_send = QPushButton("Send")
        self.btn_send.setFixedHeight(34)
        self.btn_send.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_send.setStyleSheet(_BTN_SEND_CSS)
        self.btn_send.clicked.connect(self._on_send)
        ilo.addWidget(self.btn_send)

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setFixedHeight(34)
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.setStyleSheet(_BTN_CANCEL_CSS)
        self.btn_cancel.clicked.connect(self.cancel_task_requested.emit)
        ilo.addWidget(self.btn_cancel)
        lo.addLayout(ilo)

        # Routing transparency banner
        self.routing_banner = RoutingBanner()
        lo.addWidget(self.routing_banner)

        # Event stream section
        lbl_log = QLabel("EVENT STREAM")
        lbl_log.setStyleSheet(
            f"font-size:10px; color:{TEXT_SECONDARY}; font-weight:700; "
            "letter-spacing:0.5px; font-family:'Segoe UI', sans-serif;"
        )
        lo.addWidget(lbl_log)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet(f"""
            QTextEdit {{
                background: {SURFACE_PANEL};
                color: {TEXT_PRIMARY};
                border: 1px solid {SURFACE_BORDER};
                border-radius: 8px;
                padding: 10px;
                font-family: Consolas, 'Cascadia Code', monospace;
                font-size: 11px;
                line-height: 1.5;
            }}
            {SCROLLBAR_CSS}
        """)
        lo.addWidget(self.log, 1)

    def _on_send(self):
        text = self.inp_prompt.text().strip()
        if not text:
            return
        self.inp_prompt.clear()
        model = self.cmb_model._active_model
        self.sm.clear_error()
        self.start_task_requested.emit(text, model)

    def _on_model_changed(self, provider: str, model: str):
        if not self._suppress_model_signal and model:
            if provider:
                self._active_provider = provider
            self._active_model_name = model
            self.model_changed_by_user.emit(provider, model)

    @staticmethod
    def _badge_css(state: UIState) -> str:
        bg, fg = _BADGE_COLORS.get(state, ("#14171F", STATUS_STANDBY))
        return (
            f"QLabel {{ font-weight:700; font-size:11px; padding:4px 10px; "
            f"border-radius:6px; background:{bg}; color:{fg}; font-family:'Segoe UI', sans-serif; }}"
        )

    def set_active_model(self, provider_display: str, model: str):
        """Update the HUD model indicator and dropdown from external source."""
        if provider_display:
            self._active_provider = provider_display
        if model:
            self._active_model_name = model
            self.lbl_model.setText(f"{model}")
            self.lbl_model.setVisible(True)
        # Sync dropdown without re-emitting signal
        self._suppress_model_signal = True
        self.cmb_model.set_active_model(self._active_provider, self._active_model_name)
        self._suppress_model_signal = False

    def set_available_models(self, providers: list):
        """Populate the model dropdown from provider config."""
        self._suppress_model_signal = True
        self.cmb_model.set_providers(providers)
        self._suppress_model_signal = False

    def log_event(self, event: Event):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        p = event.payload or {}
        t = event.type

        if t == EventType.START_TASK:
            m = p.get("model", "")
            m_str = f" [{m}]" if m else ""
            self.log.append(
                f'<div style="margin:2px 0;"><span style="color:{STATUS_HEALTHY};">●</span> '
                f'<span style="color:{TEXT_MUTED};">{ts}</span> '
                f'<span style="color:{STATUS_HEALTHY}; font-weight:bold;">task.start</span> '
                f'<span style="color:{TEXT_PRIMARY};">{p.get("prompt")}{m_str}</span></div>'
            )
        elif t == EventType.ROUTING_DECISION:
            reason = p.get("reason", "")
            model = p.get("model", "")
            prov = p.get("provider", "")
            if prov:
                self._active_provider = prov
            self.routing_banner.set_decision(model_label=model, reason=reason)
            self.log.append(
                f'<div style="margin:2px 0;"><span style="color:{BRAND_FRONTEND};">●</span> '
                f'<span style="color:{TEXT_MUTED};">{ts}</span> '
                f'<span style="color:{BRAND_FRONTEND}; font-weight:bold;">provider.select</span> '
                f'<span style="color:{TEXT_SECONDARY};">{model or prov} — {reason}</span></div>'
            )
        elif t == EventType.FALLBACK_STARTED:
            reason = p.get("reason", "")
            to_model = p.get("to_model", "")
            from_prov = p.get("from_provider", "")
            to_prov = p.get("to_provider", "")
            if to_prov:
                self._active_provider = to_prov
            self.health_badge.update_health(
                status="degraded",
                latency_ms=0.0,
                provider_name=_format_provider_name(from_prov or self._active_provider, self.sm),
                last_error=reason,
            )
            self.routing_banner.set_decision(model_label=to_model, reason=reason, is_fallback=True)
            self.log.append(
                f'<div style="margin:2px 0;"><span style="color:{STATUS_DEGRADED};">●</span> '
                f'<span style="color:{TEXT_MUTED};">{ts}</span> '
                f'<span style="color:{STATUS_DEGRADED}; font-weight:bold;">fallback.started</span> '
                f'<span style="color:{TEXT_SECONDARY};">{reason}</span></div>'
            )
        elif t == EventType.FALLBACK_COMPLETED:
            prov = p.get("provider", "")
            mod = p.get("model", "")
            latency = p.get("latency_ms", 0.0)
            if prov:
                self._active_provider = prov
            self.health_badge.update_health(
                status="healthy",
                latency_ms=latency,
                provider_name=_format_provider_name(self._active_provider, self.sm),
            )
            self.log.append(
                f'<div style="margin:2px 0;"><span style="color:{STATUS_HEALTHY};">●</span> '
                f'<span style="color:{TEXT_MUTED};">{ts}</span> '
                f'<span style="color:{STATUS_HEALTHY}; font-weight:bold;">fallback.ok</span> '
                f'<span style="color:{TEXT_PRIMARY};">Switched to {prov} ({mod})</span></div>'
            )
        elif t == EventType.FALLBACK_FAILED:
            self.health_badge.update_health(
                status="offline",
                latency_ms=0.0,
                provider_name=_format_provider_name(self._active_provider, self.sm),
                last_error=p.get("error"),
            )
            self.log.append(
                f'<div style="margin:2px 0;"><span style="color:{STATUS_OFFLINE};">●</span> '
                f'<span style="color:{TEXT_MUTED};">{ts}</span> '
                f'<span style="color:{STATUS_OFFLINE}; font-weight:bold;">fallback.failed</span> '
                f'<span style="color:{TEXT_PRIMARY};">{p.get("error")}</span></div>'
            )
        elif t == EventType.PROVIDER_HEALTH_UPDATE:
            health_map = p.get("health", {})
            active_p = p.get("provider") or self._active_provider or "google_gemini"
            hinfo = health_map.get(active_p)
            if not hinfo and health_map:
                active_p, hinfo = next(iter(health_map.items()))
            if hinfo:
                self.health_badge.update_health(
                    status=hinfo.get("status", "healthy"),
                    latency_ms=hinfo.get("latency_ms", 0.0),
                    provider_name=_format_provider_name(active_p, self.sm),
                    last_error=hinfo.get("last_error"),
                )
        elif t in (EventType.PROVIDER_VALIDATE_RESPONSE, EventType.SETTINGS_PROVIDER_VALIDATE_RESULT):
            pname = p.get("provider", "")
            v_status = p.get("status", "")
            latency = p.get("latency_ms", 0.0)
            msg = p.get("message", "")
            if pname and (pname == self._active_provider or not self._active_provider):
                if v_status == "connected":
                    self.health_badge.update_health(
                        status="healthy",
                        latency_ms=latency,
                        provider_name=_format_provider_name(pname, self.sm),
                    )
                elif v_status == "invalid_key":
                    self.health_badge.update_health(
                        status="offline",
                        latency_ms=0.0,
                        provider_name=_format_provider_name(pname, self.sm),
                        last_error=msg or "Invalid API key",
                    )
                elif v_status in ("error", "offline", "degraded"):
                    self.health_badge.update_health(
                        status="degraded",
                        latency_ms=0.0,
                        provider_name=_format_provider_name(pname, self.sm),
                        last_error=msg,
                    )
        elif t == EventType.TASK_CREATED:
            prov = p.get("routing", {}).get("provider") or p.get("provider")
            if prov:
                self._active_provider = prov
            self.log.append(
                f'<div style="margin:2px 0;"><span style="color:{BRAND_FRONTEND};">●</span> '
                f'<span style="color:{TEXT_MUTED};">{ts}</span> '
                f'<span style="color:{BRAND_FRONTEND}; font-weight:bold;">task.created</span> '
                f'<span style="color:{TEXT_SECONDARY};">{p.get("task_id", "")[:8]}</span></div>'
            )
        elif t == EventType.TASK_PROGRESS:
            if "tool_call" in p:
                self.log.append(
                    f'<div style="margin:2px 0;"><span style="color:{BRAND_ENGINE};">●</span> '
                    f'<span style="color:{TEXT_MUTED};">{ts}</span> '
                    f'<span style="color:{BRAND_ENGINE}; font-weight:bold;">tool.call</span> '
                    f'<span style="color:{TEXT_PRIMARY};">{p["tool_call"]}({p.get("args", {})})</span></div>'
                )
            elif "tool_result" in p:
                self.log.append(
                    f'<div style="margin:2px 0;"><span style="color:{BRAND_ENGINE};">●</span> '
                    f'<span style="color:{TEXT_MUTED};">{ts}</span> '
                    f'<span style="color:{BRAND_ENGINE}; font-weight:bold;">tool.result</span> '
                    f'<span style="color:{TEXT_SECONDARY};">{p.get("result")}</span></div>'
                )
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
            latency = p.get("latency_ms", 0.0)
            prov = p.get("provider") or self._active_provider
            if prov:
                self._active_provider = prov
            self.health_badge.update_health(
                status="healthy",
                latency_ms=latency,
                provider_name=_format_provider_name(self._active_provider, self.sm),
            )
            lat_str = f" ({int(latency)}ms)" if latency > 0 else ""
            self.log.append(
                f'<div style="margin:4px 0;"><span style="color:{STATUS_HEALTHY};">●</span> '
                f'<span style="color:{TEXT_MUTED};">{ts}</span> '
                f'<span style="color:{STATUS_HEALTHY}; font-weight:bold;">task.complete{lat_str}</span>: '
                f'<span style="color:{TEXT_PRIMARY};">{resp}</span></div>'
            )
        elif t == EventType.TASK_CANCELLED:
            self.log.append(
                f'<div style="margin:4px 0;"><span style="color:{STATUS_DEGRADED};">●</span> '
                f'<span style="color:{TEXT_MUTED};">{ts}</span> '
                f'<span style="color:{STATUS_DEGRADED}; font-weight:bold;">task.cancelled</span></div>'
            )
        elif t == EventType.TASK_FAILED:
            err = p.get("error", "Task failed")
            retriable = p.get("retriable", False)
            prov = p.get("provider") or self._active_provider
            self.health_badge.update_health(
                status="degraded" if retriable else "offline",
                latency_ms=0.0,
                provider_name=_format_provider_name(prov, self.sm),
                last_error=err,
            )
            self.log.append(
                f'<div style="margin:4px 0;"><span style="color:{STATUS_OFFLINE};">●</span> '
                f'<span style="color:{TEXT_MUTED};">{ts}</span> '
                f'<span style="color:{STATUS_OFFLINE}; font-weight:bold;">task.failed</span>: '
                f'<span style="color:{STATUS_OFFLINE};">{err}</span></div>'
            )
        elif t == EventType.HELLO:
            self.log.append(
                f'<div style="margin:2px 0;"><span style="color:{BRAND_ENGINE};">●</span> '
                f'<span style="color:{TEXT_MUTED};">{ts}</span> '
                f'<span style="color:{BRAND_ENGINE}; font-weight:bold;">engine.ready</span> '
                f'<span style="color:{TEXT_PRIMARY};">{p.get("message")}</span></div>'
            )
            prov = p.get("active_provider", "")
            mod = p.get("active_model", "")
            if prov:
                self._active_provider = prov
            if mod:
                self.set_active_model(prov, mod)
            health_map = p.get("health", {})
            hinfo = health_map.get(self._active_provider, {})
            if hinfo and hinfo.get("last_successful_request"):
                self.health_badge.update_health(
                    status=hinfo.get("status", "healthy"),
                    latency_ms=hinfo.get("latency_ms", 0.0),
                    provider_name=_format_provider_name(self._active_provider, self.sm),
                    last_error=hinfo.get("last_error"),
                )
            else:
                self.health_badge.update_health(
                    status=hinfo.get("status", "unknown") if hinfo else "unknown",
                    latency_ms=hinfo.get("latency_ms", 0.0) if hinfo else 0.0,
                    provider_name=_format_provider_name(self._active_provider, self.sm),
                    last_error=hinfo.get("last_error") if hinfo else None,
                )
        elif t in (EventType.TOOL_APPROVAL_REQUEST, EventType.PLUGIN_APPROVAL_REQUEST):
            self.log.append(
                f'<div style="margin:2px 0;"><span style="color:{STATUS_DEGRADED};">●</span> '
                f'<span style="color:{TEXT_MUTED};">{ts}</span> '
                f'<span style="color:{STATUS_DEGRADED}; font-weight:bold;">approval.pending</span> '
                f'<span style="color:{TEXT_PRIMARY};">{p.get("tool_name", "tool")} {p.get("args", {})}</span></div>'
            )
        elif t in (EventType.TOOL_APPROVAL_GRANTED, EventType.TOOL_APPROVAL_REJECTED):
            self.log.append(
                f'<div style="margin:2px 0;"><span style="color:{BRAND_FRONTEND};">●</span> '
                f'<span style="color:{TEXT_MUTED};">{ts}</span> '
                f'<span style="color:{BRAND_FRONTEND}; font-weight:bold;">approval.resolved</span> '
                f'<span style="color:{TEXT_SECONDARY};">{t.value}</span></div>'
            )
        elif t == EventType.WORKER_EXECUTION_STARTED:
            self.log.append(
                f'<div style="margin:2px 0;"><span style="color:{BRAND_ENGINE};">●</span> '
                f'<span style="color:{TEXT_MUTED};">{ts}</span> '
                f'<span style="color:{BRAND_ENGINE}; font-weight:bold;">worker.start</span></div>'
            )
        elif t == EventType.WORKER_EXECUTION_COMPLETED:
            self.log.append(
                f'<div style="margin:2px 0;"><span style="color:{STATUS_HEALTHY};">●</span> '
                f'<span style="color:{TEXT_MUTED};">{ts}</span> '
                f'<span style="color:{STATUS_HEALTHY}; font-weight:bold;">worker.done</span></div>'
            )
        elif t == EventType.ERROR:
            self.log.append(
                f'<div style="margin:2px 0;"><span style="color:{STATUS_OFFLINE};">●</span> '
                f'<span style="color:{TEXT_MUTED};">{ts}</span> '
                f'<span style="color:{STATUS_OFFLINE}; font-weight:bold;">error</span>: '
                f'<span style="color:{STATUS_OFFLINE};">{p.get("error")}</span></div>'
            )

    def _on_state(self, sm: UIStateMachine):
        self.lbl_status.setText(sm.state.value)
        self.lbl_status.setStyleSheet(self._badge_css(sm.state))

        can_start = sm.state in (UIState.CONNECTED, UIState.ERROR)
        can_cancel = sm.state == UIState.TASK_RUNNING
        self.btn_send.setEnabled(can_start)
        self.inp_prompt.setEnabled(can_start)
        self.cmb_model.setEnabled(can_start)
        self.btn_cancel.setEnabled(can_cancel)
