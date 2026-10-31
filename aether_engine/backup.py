"""Backup / restore for the Aether data home (~/.aether).

Epic 10, E10-03. A backup is a zip with a `manifest.json` at its
root: format version, app version, creation time, and for every
file its relative path, size, and sha256. SQLite databases are
snapshotted through the sqlite3 backup API, so a backup taken
while the engine runs is still transaction-consistent; WAL/SHM
sidecars are never copied.

Scope: config.json, mcp_servers.json, *.db, skills/, plugins/,
logs/. The secrets store (secrets.db) is EXCLUDED unless
`include_secrets=True` — its contents are encrypted at rest by the
platform protector, so including it copies ciphertext, never
plaintext keys.

Restore validates the ENTIRE manifest (every member present, every
sha256 matching, every target path inside the data home) before
writing a single byte; any failure refuses the whole restore.
Restoring replaces files on disk — the running engine keeps its
in-memory state, so a restart is required for a restore to take
effect (the WS layer says so in its response).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from aether_engine.version import APP_VERSION

FORMAT_VERSION = 1
SECRETS_DB = "secrets.db"
_INCLUDE_FILES = {"config.json", "mcp_servers.json"}
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
    home = home or data_home()
    dest_zip = Path(dest_zip)
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as td:
        staging = Path(td)
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for src in _collect(home, include_secrets):
                rel = src.relative_to(home).as_posix()
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
                else:
                    data = src.read_bytes()
                entries.append({"path": rel, "size": len(data), "sha256": _sha256(data)})
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
    for target, data in staged:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return {"restored": [str(t.relative_to(home)) for t, _ in staged], "files": len(staged)}
