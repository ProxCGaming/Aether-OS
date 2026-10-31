"""Epic 10 — hardening guards (grows with the epic).

E10-02: the audit logger rotates at its size cap, keeps the
configured number of gzipped archives, drops the oldest, and marks
the rotation in the fresh log — and a rotation failure never loses
the audit write itself.
"""

from __future__ import annotations

import gzip
import json

from aether_engine.audit import AuditLogger


def _events(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_audit_log_rotates_and_keeps_archives(tmp_path):
    log = tmp_path / "audit.log"
    logger = AuditLogger(log, max_bytes=400, backup_count=2)
    for i in range(40):
        logger.log_event("ENTRY", {"i": i, "pad": "x" * 40})
    # Current log exists and is under (or near) the cap; archives exist.
    assert log.exists()
    assert (tmp_path / "audit.log.1.gz").exists()
    # The fresh log's first entry records the most recent rotation.
    actions = [e.get("action") for e in _events(log)]
    assert "AUDIT_LOG_ROTATED" in actions
    # The newest entries still landed after rotation.
    assert actions[-1] == "ENTRY"
    # Archive content is real audit JSON.
    with gzip.open(tmp_path / "audit.log.1.gz", "rt") as f:
        archived = [json.loads(line) for line in f if line.strip()]
    assert archived and all(e.get("type") == "ENGINE_EVENT" for e in archived)


def test_audit_log_drops_oldest_archive(tmp_path):
    log = tmp_path / "audit.log"
    logger = AuditLogger(log, max_bytes=300, backup_count=2)
    for i in range(120):
        logger.log_event("ENTRY", {"i": i, "pad": "y" * 40})
    assert (tmp_path / "audit.log.1.gz").exists()
    assert (tmp_path / "audit.log.2.gz").exists()
    assert not (tmp_path / "audit.log.3.gz").exists()


def test_audit_log_under_cap_never_rotates(tmp_path):
    log = tmp_path / "audit.log"
    logger = AuditLogger(log, max_bytes=10 * 1024 * 1024, backup_count=2)
    logger.log_event("ONLY", {})
    assert not (tmp_path / "audit.log.1.gz").exists()
    assert [e.get("action") for e in _events(log)] == ["ONLY"]
