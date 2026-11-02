"""E10-F03 + E10-F04 — backup coverage of the LangMem store, and
restore write-phase atomicity.

F03: the backup collector admitted only two named root JSONs, root
``*.db`` files, and the skills/plugins/logs directories. The
LangMem backend's durable store (``langmem_store.json``, holding
both the facts and episodes namespaces) matched no rule, so a
backup/restore round trip reported success while silently losing
the selected backend's remembered state. ``learned.md`` — the
memory learned layer's primary store, a sibling root file in the
same one-line-include shape — was omitted the same way.

F04: restore validated the whole archive, then wrote each member
directly onto its live target. A write failure partway left a
mixed profile: earlier targets already replaced (their previous
contents lost), later targets untouched, no rollback.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from aether_engine.backup import create_backup, restore_backup
from aether_engine.memory.backends.langmem_backend import JsonFileStore

FACTS_NS = ("aether", "facts")
EPISODES_NS = ("aether", "episodes")
FACT_TEXT = "SYNTHETIC_LANGMEM_FACT"
EPISODE_TEXT = "SYNTHETIC_LANGMEM_EPISODE"


def _seed_langmem_home(tmp_path: Path) -> Path:
    home = tmp_path / "src_home"
    home.mkdir(parents=True)
    (home / "config.json").write_text('{"memory_backend": "langmem"}')
    store = JsonFileStore(home / "langmem_store.json")
    store.put(FACTS_NS, "fact-1", {"fact_text": FACT_TEXT, "entity_name": "User"})
    store.put(EPISODES_NS, "task-1", {"task_id": "task-1", "task_summary": EPISODE_TEXT})
    return home


def _manifest_paths(zpath: Path) -> set[str]:
    with zipfile.ZipFile(zpath) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    return {f["path"] for f in manifest["files"]}


# ---------------------------------------------------------------------------
# F03 — the collector covers the LangMem store
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("include_secrets", [False, True])
def test_backup_carries_langmem_store_both_flavours(tmp_path, include_secrets):
    home = _seed_langmem_home(tmp_path)
    zpath = tmp_path / "backup.zip"
    create_backup(zpath, include_secrets=include_secrets, home=home)

    assert "langmem_store.json" in _manifest_paths(zpath)
    with zipfile.ZipFile(zpath) as zf:
        assert zf.read("langmem_store.json") == (home / "langmem_store.json").read_bytes()


def test_restore_brings_back_langmem_fact_and_episode(tmp_path):
    home = _seed_langmem_home(tmp_path)
    zpath = tmp_path / "backup.zip"
    create_backup(zpath, home=home)

    dst = tmp_path / "dst_home"
    result = restore_backup(zpath, home=dst)
    assert "langmem_store.json" in result["restored"]

    fresh = JsonFileStore(dst / "langmem_store.json")
    fact = fresh.get(FACTS_NS, "fact-1")
    assert fact is not None
    assert fact.value["fact_text"] == FACT_TEXT
    episode = fresh.get(EPISODES_NS, "task-1")
    assert episode is not None
    assert episode.value["task_summary"] == EPISODE_TEXT


def test_backup_carries_learned_md_sibling_store(tmp_path):
    home = tmp_path / "src_home"
    home.mkdir()
    (home / "config.json").write_text("{}")
    (home / "learned.md").write_text("# Learned lessons\n\n- SYNTHETIC_LESSON\n")
    zpath = tmp_path / "backup.zip"
    create_backup(zpath, home=home)

    assert "learned.md" in _manifest_paths(zpath)
    dst = tmp_path / "dst_home"
    restore_backup(zpath, home=dst)
    assert (dst / "learned.md").read_text() == (home / "learned.md").read_text()


def test_old_backup_without_langmem_entry_restores_fine(tmp_path):
    # A pre-fix-era backup: the source home simply has no LangMem
    # store, so the archive carries no entry for it — the exact
    # shape older backups of real profiles have.
    src = tmp_path / "src_home"
    src.mkdir()
    (src / "config.json").write_text('{"default_model": "gemini-3.5-flash"}')
    zpath = tmp_path / "old.zip"
    create_backup(zpath, home=src)
    assert "langmem_store.json" not in _manifest_paths(zpath)

    # Restoring it must succeed and must not touch a store the
    # target already has (absence in the archive is not deletion).
    dst = tmp_path / "dst_home"
    dst.mkdir()
    store = JsonFileStore(dst / "langmem_store.json")
    store.put(FACTS_NS, "keep-me", {"fact_text": "PREEXISTING_FACT"})
    result = restore_backup(zpath, home=dst)
    assert result["files"] == 1
    fresh = JsonFileStore(dst / "langmem_store.json")
    kept = fresh.get(FACTS_NS, "keep-me")
    assert kept is not None
    assert kept.value["fact_text"] == "PREEXISTING_FACT"


# ---------------------------------------------------------------------------
# F04 — a failed restore leaves the profile exactly as it was
# ---------------------------------------------------------------------------


def _seed_profile_pair(tmp_path: Path) -> tuple[Path, Path, dict[str, bytes]]:
    """A source home (backup content) and a target home (previous
    content) with the same three members. Returns (src, dst,
    previous-bytes-by-name)."""
    src = tmp_path / "src_home"
    src.mkdir()
    (src / "config.json").write_text('{"default_model": "BACKUP_CONFIG"}')
    (src / "mcp_servers.json").write_text('{"servers": ["BACKUP_MCP"]}')
    conn = sqlite3.connect(src / "memory.db")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('BACKUP_DB')")
    conn.commit()
    conn.close()

    dst = tmp_path / "dst_home"
    dst.mkdir()
    previous = {
        "config.json": b'{"default_model": "PREVIOUS_CONFIG"}',
        "mcp_servers.json": b'{"servers": ["PREVIOUS_MCP"]}',
        "memory.db": b"PREVIOUS_DB_BYTES",
    }
    for name, data in previous.items():
        (dst / name).write_bytes(data)
    return src, dst, previous


def test_restore_write_failure_leaves_profile_untouched(tmp_path, monkeypatch):
    src, dst, previous = _seed_profile_pair(tmp_path)
    zpath = tmp_path / "backup.zip"
    create_backup(zpath, home=src)

    real_write_bytes = Path.write_bytes
    calls = {"n": 0}

    def flaky_write_bytes(self: Path, data: bytes) -> int:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("SYNTHETIC_DISK_FULL_SECOND_RESTORE_FILE")
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", flaky_write_bytes)
    with pytest.raises(OSError, match="SYNTHETIC_DISK_FULL"):
        restore_backup(zpath, home=dst)
    monkeypatch.undo()

    assert calls["n"] >= 2  # the failure really was injected mid-write-phase
    # Every pre-existing target is byte-identical to before the attempt.
    for name, data in previous.items():
        assert (dst / name).read_bytes() == data, name
    # No new files and no staging leftovers anywhere in the profile.
    assert {p.name for p in dst.iterdir()} == set(previous)


def test_restore_failure_on_first_write_also_leaves_profile_untouched(
    tmp_path, monkeypatch
):
    src, dst, previous = _seed_profile_pair(tmp_path)
    zpath = tmp_path / "backup.zip"
    create_backup(zpath, home=src)

    def always_fail(self: Path, data: bytes) -> int:
        raise OSError("SYNTHETIC_DISK_FULL_FIRST_RESTORE_FILE")

    monkeypatch.setattr(Path, "write_bytes", always_fail)
    with pytest.raises(OSError, match="SYNTHETIC_DISK_FULL"):
        restore_backup(zpath, home=dst)
    monkeypatch.undo()

    for name, data in previous.items():
        assert (dst / name).read_bytes() == data, name
    assert {p.name for p in dst.iterdir()} == set(previous)


def test_clean_restore_still_replaces_everything(tmp_path):
    src, dst, _previous = _seed_profile_pair(tmp_path)
    zpath = tmp_path / "backup.zip"
    create_backup(zpath, home=src)

    result = restore_backup(zpath, home=dst)
    assert result["files"] == 3
    assert set(result["restored"]) == {"config.json", "mcp_servers.json", "memory.db"}
    for name in ("config.json", "mcp_servers.json"):
        assert (dst / name).read_bytes() == (src / name).read_bytes()
    # DBs travel through the sqlite3 backup API, so the restored
    # file is logically identical rather than byte-identical.
    conn = sqlite3.connect(dst / "memory.db")
    try:
        assert conn.execute("SELECT v FROM t").fetchone() == ("BACKUP_DB",)
    finally:
        conn.close()
    # No staging leftovers after a successful restore either.
    assert {p.name for p in dst.iterdir()} == {
        "config.json",
        "mcp_servers.json",
        "memory.db",
    }


def test_tampered_backup_refused_without_touching_targets(tmp_path):
    src, dst, previous = _seed_profile_pair(tmp_path)
    zpath = tmp_path / "backup.zip"
    create_backup(zpath, home=src)

    # Rewrite the archive with one member's bytes altered — the
    # manifest hashes no longer match, so validation must refuse
    # the whole restore before any write (pre-existing behaviour).
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(zpath) as zin, zipfile.ZipFile(bad, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "config.json":
                data = b'{"default_model": "TAMPERED"}'
            zout.writestr(item, data)

    with pytest.raises(ValueError):
        restore_backup(bad, home=dst)
    for name, data in previous.items():
        assert (dst / name).read_bytes() == data, name
    assert {p.name for p in dst.iterdir()} == set(previous)
