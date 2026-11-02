"""Redacted diagnostics bundle + crash logging (Epic 10, E10-04).

The bundle is built to be safe to hand to another person: config
values pass through the audit log's central redactor (secret-shaped
keys and bare key patterns are scrubbed), memory contributes COUNTS
only — never fact/episode content — and the audit tail is included
as written (it is redacted at write time by the same redactor).
"""

from __future__ import annotations

import platform
import sys
import traceback
from pathlib import Path
from typing import Any

from aether_engine.audit import _redact
from aether_engine.backup import data_home
from aether_engine.version import APP_VERSION


def logs_dir() -> Path:
    d = data_home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def install_crash_logging() -> Path:
    """Append unhandled exceptions (main + threads) to crash.log."""
    crash_path = logs_dir() / "crash.log"

    def _write(kind: str, exc_type, exc, tb) -> None:
        try:
            with open(crash_path, "a", encoding="utf-8") as f:
                f.write(f"\n=== {kind} ===\n")
                traceback.print_exception(exc_type, exc, tb, file=f)
        except Exception:  # noqa: BLE001, S110 — crash logging must never itself crash the process
            pass

    previous_hook = sys.excepthook

    def hook(exc_type, exc, tb):
        _write("UNHANDLED", exc_type, exc, tb)
        previous_hook(exc_type, exc, tb)

    sys.excepthook = hook

    import threading

    def thread_hook(args):
        _write(f"THREAD {args.thread.name}", args.exc_type, args.exc_value, args.exc_traceback)

    threading.excepthook = thread_hook
    return crash_path


def build_diagnostics(*, user_config: Any, health: dict, kg_count: int,
                      episodic_count: int, audit_log_path: Path,
                      providers: list[str]) -> dict[str, Any]:
    """Assemble the redacted bundle. Counts only — no memory content."""
    tail: list[str] = []
    try:
        lines = Path(audit_log_path).read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-50:]
    except OSError:
        tail = []
    config_dump = {}
    try:
        config_dump = user_config.model_dump(mode="json")
    except Exception:  # noqa: BLE001 — diagnostics must degrade, not fail, on an odd config object
        config_dump = {"error": "config could not be serialised"}
    return {
        "app_version": APP_VERSION,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "config": _redact(config_dump),
        "providers_configured": providers,
        "provider_health": health,
        "memory": {"kg_facts": kg_count, "episodes": episodic_count},
        "audit_tail": tail,
    }
