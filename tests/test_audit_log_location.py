"""S1-N1: the audit trail has ONE canonical location.

The engine's many AuditLogger instances and the worker subprocess all
append to ~/.aether/logs/audit.log, independent of any process's
working directory. Before the fix the default was the CWD-relative
Path("logs/audit.log"), so the trail scattered by launch directory.
"""
from pathlib import Path

from aether_engine.audit import DEFAULT_AUDIT_LOG_PATH as ENGINE_PATH
from aether_worker.__main__ import DEFAULT_AUDIT_LOG_PATH as WORKER_PATH


def test_engine_default_is_the_canonical_home_location():
    assert ENGINE_PATH == Path.home() / ".aether" / "logs" / "audit.log"
    assert ENGINE_PATH.is_absolute()


def test_worker_uses_the_same_canonical_location():
    assert WORKER_PATH == ENGINE_PATH
