"""E10-F05 — a restore that replaces a live WAL database must not be
undone by that database's write-ahead log.

Three shapes are pinned:

1. An ORPHANED non-empty WAL (the owning process died without
   closing): pre-fix the restore replaced only the main file, so
   the next open replayed the old WAL frames over the restored
   database — the live grant came back, the backed-up grant was
   gone, and the restore had reported success.
2. The LIVE process-wide ApprovalCache singleton: restore must
   quiesce its connection at the commit seam so the file swap is
   not defeated by the connection's WAL state, and the singleton
   must serve the restored rows afterwards (it reopens lazily).
3. A WAL that CANNOT be settled (a foreign connection holding a
   read transaction open): restore must refuse before committing
   anything — no member lands, the profile is untouched.

Controls: a zero-byte WAL sidecar is not a refusal, a restore with
no live state round-trips as before, and ApprovalCache close /
lazy reopen keeps normal grant semantics (E2-F04 threading model
unchanged).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from aether_engine import backup as backup_module
from aether_engine.backup import create_backup, restore_backup
from aether_engine.tools import approval_cache as approval_cache_module
from aether_engine.tools.approval_cache import ApprovalCache

REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolved(p: Path) -> str:
    return str(Path(p).resolve())


def _prefixes(cache: ApprovalCache) -> list[str]:
    return sorted(g["prefix"] for g in cache.list_grants())


def _force_close(cache: ApprovalCache) -> None:
    """Close a cache's raw connection without any public close API
    (used for source fixtures that must not be reused afterwards)."""
    conn = getattr(cache, "_conn", None)
    if conn is not None:
        conn.close()


def _source_backup(tmp_path: Path, grants: list[str]) -> Path:
    src_home = tmp_path / "src-home"
    src_home.mkdir()
    cache = ApprovalCache(src_home / "approval-cache.db")
    for prefix in grants:
        cache.grant(prefix, "write")
    _force_close(cache)
    zip_path = tmp_path / "backup.zip"
    create_backup(zip_path, home=src_home)
    return zip_path


def _leave_crashed_wal(db_path: Path, prefix: str) -> Path:
    """Create a WAL database whose owner exits WITHOUT closing, so
    the committed grant survives only in a non-empty -wal sidecar."""
    code = (
        "import os\n"
        "from aether_engine.tools.approval_cache import ApprovalCache\n"
        f"cache = ApprovalCache({str(db_path)!r})\n"
        f"cache.grant({prefix!r}, 'write')\n"
        "os._exit(0)\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    subprocess.run([sys.executable, "-c", code], check=True, env=env)
    wal = Path(f"{db_path}-wal")
    assert wal.exists() and wal.stat().st_size > 0, "fixture must leave a live WAL"
    return wal


# ── shape 1: orphaned WAL must not replay over the restore ──────


def test_restore_discards_orphaned_wal(tmp_path):
    target_home = tmp_path / "target-home"
    target_home.mkdir()
    db = target_home / "approval-cache.db"
    live_prefix = _resolved(tmp_path / "live-dir")
    _leave_crashed_wal(db, live_prefix)

    backed_prefix = _resolved(tmp_path / "backed-up-dir")
    zip_path = _source_backup(tmp_path, [backed_prefix])

    result = restore_backup(zip_path, home=target_home)
    assert "approval-cache.db" in result["restored"]
    assert not Path(f"{db}-wal").exists()
    assert not Path(f"{db}-shm").exists()

    fresh = ApprovalCache(db)
    try:
        assert _prefixes(fresh) == [backed_prefix]
    finally:
        _force_close(fresh)


# ── shape 2: the live singleton is quiesced and serves restored ─


def test_restore_quiesces_live_default_cache(tmp_path, monkeypatch):
    target_home = tmp_path / "target-home"
    target_home.mkdir()
    db = target_home / "approval-cache.db"
    live_prefix = _resolved(tmp_path / "live-dir")
    live = ApprovalCache(db)
    live.grant(live_prefix, "write")
    monkeypatch.setattr(approval_cache_module, "_default_cache", live)
    assert Path(f"{db}-wal").stat().st_size > 0

    backed_prefix = _resolved(tmp_path / "backed-up-dir")
    zip_path = _source_backup(tmp_path, [backed_prefix])

    restore_backup(zip_path, home=target_home)

    # The same process-wide singleton now serves the restored file.
    assert _prefixes(live) == [backed_prefix]
    _force_close(live)
    fresh = ApprovalCache(db)
    try:
        assert _prefixes(fresh) == [backed_prefix]
    finally:
        _force_close(fresh)


# ── shape 3: an unsettleable live WAL refuses the whole restore ─


def test_restore_refuses_when_live_wal_cannot_be_checkpointed(tmp_path):
    target_home = tmp_path / "target-home"
    target_home.mkdir()
    (target_home / "config.json").write_text("PREVIOUS_CONFIG")
    db = target_home / "foreign.db"
    holder = sqlite3.connect(db)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("CREATE TABLE t (x TEXT)")
    holder.execute("INSERT INTO t VALUES ('live-row')")
    holder.commit()
    # An open read transaction pins the WAL: it cannot be truncated
    # or settled by anyone else while this connection holds it.
    holder.execute("BEGIN")
    assert holder.execute("SELECT x FROM t").fetchall() == [("live-row",)]
    assert Path(f"{db}-wal").stat().st_size > 0

    src_home = tmp_path / "src-home"
    src_home.mkdir()
    (src_home / "config.json").write_text("BACKUP_CONFIG")
    src_db = sqlite3.connect(src_home / "foreign.db")
    src_db.execute("CREATE TABLE t (x TEXT)")
    src_db.execute("INSERT INTO t VALUES ('backup-row')")
    src_db.commit()
    src_db.close()
    zip_path = tmp_path / "backup.zip"
    create_backup(zip_path, home=src_home)

    try:
        with pytest.raises(ValueError, match="(?i)wal|write-ahead"):
            restore_backup(zip_path, home=target_home)
        # Nothing committed: the config member did not land either,
        # and no staging temps are left behind.
        assert (target_home / "config.json").read_text() == "PREVIOUS_CONFIG"
        assert not list(target_home.rglob("*.aether-restore-tmp"))
    finally:
        holder.rollback()
        holder.close()

    fresh = sqlite3.connect(db)
    try:
        assert fresh.execute("SELECT x FROM t").fetchall() == [("live-row",)]
    finally:
        fresh.close()


# ── controls ─────────────────────────────────────────────────────


def test_restore_with_zero_byte_wal_sidecar_succeeds(tmp_path):
    target_home = tmp_path / "target-home"
    target_home.mkdir()
    db = target_home / "approval-cache.db"
    stale = ApprovalCache(db)
    stale.grant(_resolved(tmp_path / "stale-dir"), "write")
    _force_close(stale)
    Path(f"{db}-wal").write_bytes(b"")  # an empty sidecar settles trivially

    backed_prefix = _resolved(tmp_path / "backed-up-dir")
    zip_path = _source_backup(tmp_path, [backed_prefix])
    restore_backup(zip_path, home=target_home)

    fresh = ApprovalCache(db)
    try:
        assert _prefixes(fresh) == [backed_prefix]
    finally:
        _force_close(fresh)


def test_restore_without_any_live_state_round_trips(tmp_path):
    target_home = tmp_path / "target-home"
    target_home.mkdir()
    backed_prefix = _resolved(tmp_path / "backed-up-dir")
    zip_path = _source_backup(tmp_path, [backed_prefix])

    result = restore_backup(zip_path, home=target_home)
    assert "approval-cache.db" in result["restored"]

    fresh = ApprovalCache(target_home / "approval-cache.db")
    try:
        assert _prefixes(fresh) == [backed_prefix]
    finally:
        _force_close(fresh)


def test_approval_cache_close_is_idempotent_and_reopens_lazily(tmp_path):
    db = tmp_path / "approval-cache.db"
    cache = ApprovalCache(db)
    keep = _resolved(tmp_path / "keep-dir")
    drop = _resolved(tmp_path / "drop-dir")
    keep_id = cache.grant(keep, "write")
    drop_id = cache.grant(drop, "read_only")

    cache.close()
    cache.close()  # idempotent — no error, no state change

    # Lazy reopen serves the same grants and full grant semantics.
    assert _prefixes(cache) == sorted([keep, drop])
    assert cache.revoke(drop_id) is True
    assert _prefixes(cache) == [keep]
    covered = cache.covering_grant(risk="read_only", args={"path": f"{keep}/file.txt"})
    assert covered is not None and covered["id"] == keep_id

    cache.close()
    fresh = ApprovalCache(db)
    try:
        assert _prefixes(fresh) == [keep]
    finally:
        _force_close(fresh)


def test_restore_without_matching_db_member_leaves_default_cache_alone(tmp_path, monkeypatch):
    # A restore whose members do not include the live cache's file
    # must not close or replace the singleton's connection.
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    cache = ApprovalCache(other_home / "approval-cache.db")
    prefix = _resolved(tmp_path / "other-dir")
    cache.grant(prefix, "write")
    monkeypatch.setattr(approval_cache_module, "_default_cache", cache)
    conn_before = cache._conn

    target_home = tmp_path / "target-home"
    target_home.mkdir()
    src_home = tmp_path / "src-home"
    src_home.mkdir()
    (src_home / "config.json").write_text("{}")
    zip_path = tmp_path / "backup.zip"
    create_backup(zip_path, home=src_home)
    restore_backup(zip_path, home=target_home)

    assert cache._conn is conn_before
    assert _prefixes(cache) == [prefix]
    _force_close(cache)
    assert backup_module is not None
