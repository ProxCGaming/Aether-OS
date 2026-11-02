"""Backup / restore for the Aether data home (~/.aether).

Epic 10, E10-03. A backup is a zip with a `manifest.json` at its
root: format version, app version, creation time, and for every
file its relative path, size, and sha256. SQLite databases are
snapshotted through the sqlite3 backup API, so a backup taken
while the engine runs is still transaction-consistent; WAL/SHM
sidecars are never copied.

Scope: config.json, mcp_servers.json, langmem_store.json,
learned.md, *.db, skills/, plugins/, logs/. The named JSON/Markdown
files are the profile's non-SQLite durable stores: the LangMem
memory backend keeps both its facts and episodes namespaces in
langmem_store.json (E10-F03 — the native backend's stores are
*.db files and ride the glob), and learned.md is the memory
learned layer's primary store. The secrets store (secrets.db) is
EXCLUDED unless `include_secrets=True` — its contents are
encrypted at rest by the platform protector, so including it
copies ciphertext, never plaintext keys.

mcp_servers.json is always collected, but without secrets consent
it is collected SANITISED (E10-F01): the registry classifies MCP
header/env *values* as API-key secrets, so those values are
replaced with "[REDACTED]" while server names, commands, URLs and
the header/env names survive — a default backup carries no API
keys yet still restores the server definitions (keys are simply
re-entered afterwards). A config that cannot be parsed cannot be
verified secret-free, so it is omitted from a no-secrets backup
rather than copied raw. With consent the file is copied raw.

Restore validates the ENTIRE manifest (every member present, every
sha256 matching, every target path inside the data home) before
writing a single byte; any failure refuses the whole restore. The
write phase itself is staged, then committed (E10-F04): every
member is written in full to a temp sibling of its target first,
and only when all members are staged are the temp files renamed
over the live targets (the same temp + os.replace commit step as
E3-F04/E6-F07). A write failure therefore lands in staging, while
no live target has changed — the profile is either fully replaced
or left exactly as it was, never a mix of the two. Restoring
replaces files on disk — the running engine keeps its in-memory
state, so a restart is required for a restore to take effect (the
WS layer says so in its response).

One piece of on-disk state can defeat a file swap, though: a
database in WAL mode whose write-ahead log still holds the
pre-restore pages (E10-F05). Left in place, the ``-wal`` sidecar
is replayed over the restored file on the next open — or, while
its owning connection is still live, that connection goes on
serving and checkpointing the OLD database — and the reportedly
restored database silently reverts. So between staging and
commit, every database member is quiesced: a live owner the
restore can reach (the process-wide ApprovalCache, the engine's
one long-lived WAL connection) is closed first, and each target's
sidecars are then settled — checkpointed away via a short
probe connection if one is needed — and removed. A WAL that
cannot be settled (a foreign connection holding it open) refuses
the whole restore BEFORE any member commits: success is never
reported over a database that would revert.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from aether_engine.version import APP_VERSION

FORMAT_VERSION = 1
SECRETS_DB = "secrets.db"
_INCLUDE_FILES = {"config.json", "mcp_servers.json", "langmem_store.json", "learned.md"}
_INCLUDE_DIRS = ("skills", "plugins", "logs")


def data_home() -> Path:
    return Path.home() / ".aether"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _collect(home: Path, include_secrets: bool) -> list[Path]:
    files: list[Path] = []
    for name in sorted(_INCLUDE_FILES):
        p = home / name
        if p.is_file():
            files.append(p)
    for p in sorted(home.glob("*.db")):
        if p.name == SECRETS_DB and not include_secrets:
            continue
        files.append(p)
    for dirname in _INCLUDE_DIRS:
        d = home / dirname
        if d.is_dir():
            files.extend(sorted(f for f in d.rglob("*") if f.is_file()))
    return files


# Same marker the audit redactor uses (aether_engine/audit.py).
_REDACTED = "[REDACTED]"


def _sanitize_mcp_config(data: bytes) -> bytes | None:
    """Return an mcp_servers.json body with all credential values
    redacted, or None if *data* is not parseable JSON.

    The MCP registry classifies header/env *values* as secrets (see
    ``redact_mcp_config_for_ui``); everything else in the file is
    ordinary configuration. The walk is recursive, so the values are
    caught wherever they sit in the document (top-level server map,
    preserved invalid entries, a nested ``mcpServers`` wrapper).
    When nothing needs redacting the original bytes are returned
    unchanged, so callers can tell a sanitised copy from a raw one.
    """
    try:
        parsed = json.loads(data)
    except ValueError:
        return None

    changed = False

    def walk(node: Any) -> Any:
        nonlocal changed
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, value in node.items():
                if key in ("headers", "env") and isinstance(value, dict):
                    if any(v != _REDACTED for v in value.values()):
                        changed = True
                    out[key] = {k: _REDACTED for k in value}
                else:
                    out[key] = walk(value)
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    cleaned = walk(parsed)
    if not changed:
        return data
    return json.dumps(cleaned, indent=2).encode()


def _snapshot_db(src: Path, dest: Path) -> None:
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dest_conn = sqlite3.connect(dest)
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


def create_backup(dest_zip: Path, *, include_secrets: bool = False,
                  home: Path | None = None) -> dict[str, Any]:
    """Write a backup zip to *dest_zip*; return its manifest."""
    # Consent is explicit (E10-F02): only the boolean True includes
    # secrets, whatever an untyped caller passed.
    include_secrets = include_secrets is True
    home = home or data_home()
    dest_zip = Path(dest_zip)
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as td:
        staging = Path(td)
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for src in _collect(home, include_secrets):
                rel = src.relative_to(home).as_posix()
                sanitized = False
                if src.suffix == ".db":
                    snap = staging / rel.replace("/", "_")
                    try:
                        _snapshot_db(src, snap)
                        data = snap.read_bytes()
                    except sqlite3.DatabaseError:
                        # Not actually a SQLite file despite the
                        # suffix — copy it raw rather than failing
                        # the whole backup.
                        data = src.read_bytes()
                elif rel == "mcp_servers.json" and not include_secrets:
                    # E10-F01: never let MCP credential values into a
                    # no-secrets backup; keep the definitions.
                    raw = src.read_bytes()
                    data = _sanitize_mcp_config(raw)
                    if data is None:
                        # Unparseable — cannot be verified
                        # secret-free, so it is left out rather
                        # than copied unverified.
                        continue
                    sanitized = data != raw
                else:
                    data = src.read_bytes()
                entry = {"path": rel, "size": len(data), "sha256": _sha256(data)}
                if sanitized:
                    entry["sanitized"] = True
                entries.append(entry)
                zf.writestr(rel, data)
            manifest = {
                "format": FORMAT_VERSION,
                "app_version": APP_VERSION,
                "created_at": int(time.time()),
                "include_secrets": include_secrets,
                "files": entries,
            }
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return manifest


def _close_approval_cache_owner(db_targets: list[Path]) -> None:
    """Close the process-wide ApprovalCache if a restore is about
    to replace ITS database file (E10-F05).

    The cache is the engine's one long-lived WAL connection; left
    open across the swap it would keep serving — and on close,
    checkpointing — the pre-restore database. ``close()`` is
    serialised by the cache's own lock, checkpoints its WAL into
    the current file, and the cache reopens lazily onto the
    restored file on next use. A cache rooted at any other path is
    not this restore's business and is left alone.
    """
    targets = [t for t in db_targets if t.name == "approval-cache.db"]
    if not targets:
        return
    from aether_engine.tools.approval_cache import get_default_cache_if_open

    cache = get_default_cache_if_open()
    if cache is None or cache.db_path.resolve() != targets[0].resolve():
        return
    try:
        cache.close()
    except Exception as e:
        raise ValueError(
            "cannot restore approval-cache.db: its live connection "
            f"could not be closed ({e})"
        ) from e


def _settle_db_sidecars(target: Path) -> None:
    """Make sure no pre-restore WAL state can replay over the
    restored *target* (E10-F05).

    A non-empty ``-wal`` is settled by checkpointing it into the
    current (pre-restore) file through a short probe connection —
    possible exactly when no live connection is pinning it — and
    the sidecars are then removed. If the WAL cannot be fully
    checkpointed (a foreign connection holds the database open), or
    still holds frames afterwards, the restore is refused: the
    caller raises before any member commits.
    """
    wal = Path(f"{target}-wal")
    shm = Path(f"{target}-shm")
    if wal.exists() and wal.stat().st_size > 0:
        settled = False
        if target.is_file():
            try:
                probe = sqlite3.connect(target, isolation_level=None)
                try:
                    row = probe.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    settled = bool(row) and row[0] == 0
                finally:
                    probe.close()
            except sqlite3.Error:
                settled = False
        if not settled or (wal.exists() and wal.stat().st_size > 0):
            raise ValueError(
                f"cannot restore {target.name}: its write-ahead log "
                f"({wal.name}) could not be settled — the database is "
                "still in use; stop the running work and retry the restore"
            )
    wal.unlink(missing_ok=True)
    shm.unlink(missing_ok=True)


def _quiesce_live_databases(targets: list[Path]) -> None:
    """Quiesce every database a restore is about to replace
    (E10-F05): close the live owners the restore can reach, then
    settle each target's WAL sidecars. Raises ValueError — before
    anything commits — when a database cannot be quiesced."""
    db_targets = [t for t in targets if t.name.endswith(".db")]
    if not db_targets:
        return
    _close_approval_cache_owner(db_targets)
    for target in db_targets:
        _settle_db_sidecars(target)


def restore_backup(zip_path: Path, *, home: Path | None = None) -> dict[str, Any]:
    """Restore *zip_path* into the data home after full validation.

    Returns {"restored": [paths], "files": n}. Raises ValueError on
    any manifest/hash/path problem — before writing anything.
    """
    home = home or data_home()
    home.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except KeyError as e:
            raise ValueError("not an Aether backup: manifest.json missing") from e
        if manifest.get("format") != FORMAT_VERSION:
            raise ValueError(f"unsupported backup format: {manifest.get('format')!r}")
        names = set(zf.namelist())
        staged: list[tuple[Path, bytes]] = []
        for entry in manifest.get("files", []):
            rel = entry["path"]
            target = (home / rel).resolve()
            if home.resolve() not in target.parents and target != home.resolve():
                raise ValueError(f"backup path escapes the data home: {rel}")
            if rel not in names:
                raise ValueError(f"backup member missing: {rel}")
            data = zf.read(rel)
            if len(data) != entry["size"] or _sha256(data) != entry["sha256"]:
                raise ValueError(f"backup member corrupt (hash/size mismatch): {rel}")
            staged.append((home / rel, data))
    # E10-F04: stage every member to a temp sibling of its target
    # BEFORE any live target changes, then commit by renaming the
    # temp files over the targets. All fallible byte-writing
    # happens in the staging phase; if any of it fails, the staged
    # temps are removed and the profile is left exactly as it was.
    # The commit phase performs only same-directory renames of
    # fully written files (os.replace — the E3-F04/E6-F07 commit
    # step), which is also what keeps this compatible with the
    # live-DB handling a later fix (E10-F05) adds at this seam.
    temps: list[Path] = []
    try:
        for target, data in staged:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.aether-restore-tmp")
            tmp.write_bytes(data)
            temps.append(tmp)
        # E10-F05: with every member staged and none committed yet,
        # quiesce the databases being replaced — a refusal here
        # still leaves the whole profile untouched.
        _quiesce_live_databases([target for target, _ in staged])
        for tmp, (target, _data) in zip(temps, staged):
            os.replace(tmp, target)
    except BaseException:
        for tmp in temps:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return {"restored": [str(t.relative_to(home)) for t, _ in staged], "files": len(staged)}
