"""Wave 2, N-17 + N-29: plugin package size bounds and directory parity.

N-17 [S2]: zip validation/extraction had no size governance at all —
``zf.read(member)`` loaded every member whole into RAM for hashing,
and extraction wrote whole members back out. A small zip expanding
to gigabytes (a zip bomb) OOMs validation or fills the disk; the
self-consistent manifest's hash/allowlist checks do not help because
the attacker authors those too. The central directory already
declares every member's uncompressed size, so the refusal can (and
must) happen BEFORE any member is decompressed.

N-29 [S3]: directory installs copied *everything* with
``copytree``/``copy2`` — which dereference symlinks — so undeclared
files, dotfiles, and symlinked content from outside the folder (a
symlinked ``~/.ssh`` file) entered the installed plugin tree:
content the approval card never showed. Zip installs were gated;
directory installs had no equivalent.
"""
from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest

from aether_engine.plugins import installer as installer_mod
from aether_engine.plugins.installer import PluginInstaller


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def installer(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return PluginInstaller(tmp_path / "plugins")


def _write_zip(path: Path, manifest: dict, members: dict) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest))
        for name, data in members.items():
            z.writestr(name, data)
    return path


def _signed_zip(path: Path, members: dict, name="BombPlugin") -> Path:
    manifest = {"name": name, "version": "1.0",
                "files": {n: _sha(d) for n, d in members.items()}}
    return _write_zip(path, manifest, members)


def _install(installer, source: Path):
    return installer.install(installer.prepare_install(str(source)))


# ---------------------------------------------------------------------
# N-17 — zip size governance
# ---------------------------------------------------------------------

def test_zip_member_over_size_cap_refused_before_extraction(
        installer, tmp_path, monkeypatch):
    monkeypatch.setattr(installer_mod, "PLUGIN_ZIP_MAX_MEMBER_BYTES", 1024)
    # Highly compressible payload: the archive stays tiny while the
    # member expands well past the cap — the classic bomb shape. The
    # hash in the manifest is HONEST: only the size gate can refuse.
    members = {"big.bin": b"A" * 4096}
    zip_path = _signed_zip(tmp_path / "bomb.zip", members)
    with pytest.raises(ValueError):
        _install(installer, zip_path)
    assert not (installer.plugins_dir / "BombPlugin").exists()


def test_zip_total_size_cap_refused(installer, tmp_path, monkeypatch):
    monkeypatch.setattr(installer_mod, "PLUGIN_ZIP_MAX_MEMBER_BYTES", 100_000)
    monkeypatch.setattr(installer_mod, "PLUGIN_ZIP_MAX_TOTAL_BYTES", 1500)
    members = {"a.bin": b"B" * 1000, "b.bin": b"C" * 1000}
    zip_path = _signed_zip(tmp_path / "total.zip", members)
    with pytest.raises(ValueError):
        _install(installer, zip_path)
    assert not (installer.plugins_dir / "BombPlugin").exists()


def test_zip_member_count_cap_refused(installer, tmp_path, monkeypatch):
    monkeypatch.setattr(installer_mod, "PLUGIN_ZIP_MAX_MEMBERS", 3)
    members = {f"f{i}.txt": b"x" for i in range(5)}
    zip_path = _signed_zip(tmp_path / "many.zip", members)
    with pytest.raises(ValueError):
        _install(installer, zip_path)
    assert not (installer.plugins_dir / "BombPlugin").exists()


def test_honest_zip_within_bounds_still_installs(installer, tmp_path):
    members = {"code.py": b"print('hi')\n", "notes/readme.md": b"# hi\n"}
    zip_path = _signed_zip(tmp_path / "good.zip", members, name="GoodPlugin")
    _install(installer, zip_path)
    target = installer.plugins_dir / "GoodPlugin"
    assert (target / "code.py").read_bytes() == b"print('hi')\n"
    assert (target / "notes" / "readme.md").read_bytes() == b"# hi\n"


# ---------------------------------------------------------------------
# N-29 — directory installs: symlinks + files-map parity
# ---------------------------------------------------------------------

def _write_dir_plugin(root: Path, files: dict, manifest_extra=None,
                      name="DirPlugin") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest = {"name": name, "version": "1.0"}
    if manifest_extra:
        manifest.update(manifest_extra)
    (root / "manifest.json").write_text(json.dumps(manifest),
                                        encoding="utf-8")
    for rel, data in files.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return root


def test_dir_install_refuses_symlink_to_outside_file(installer, tmp_path):
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("ssh-key-material", encoding="utf-8")
    src = _write_dir_plugin(tmp_path / "src", {"real.txt": b"real"})
    os.symlink(outside, src / "leaked.txt")
    with pytest.raises(ValueError):
        _install(installer, src)
    target = installer.plugins_dir / "DirPlugin"
    assert not target.exists() or not (target / "leaked.txt").exists()


def test_dir_install_refuses_symlinked_directory(installer, tmp_path):
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "loot.txt").write_text("loot", encoding="utf-8")
    src = _write_dir_plugin(tmp_path / "src", {"real.txt": b"real"})
    os.symlink(outside_dir, src / "linked_dir", target_is_directory=True)
    with pytest.raises(ValueError):
        _install(installer, src)
    target = installer.plugins_dir / "DirPlugin"
    assert not target.exists() or not (target / "linked_dir").exists()


def test_dir_install_with_files_map_refuses_undeclared_file(
        installer, tmp_path):
    declared = {"declared.txt": b"shown on the card"}
    files_map = {n: _sha(d) for n, d in declared.items()}
    src = _write_dir_plugin(
        tmp_path / "src",
        {**declared, "undeclared.txt": b"never shown"},
        manifest_extra={"files": files_map})
    with pytest.raises(ValueError):
        _install(installer, src)
    target = installer.plugins_dir / "DirPlugin"
    assert not target.exists() or not (target / "undeclared.txt").exists()


def test_dir_install_with_files_map_refuses_hash_mismatch(
        installer, tmp_path):
    files_map = {"declared.txt": _sha(b"the bytes the card approved")}
    src = _write_dir_plugin(
        tmp_path / "src", {"declared.txt": b"different bytes entirely"},
        manifest_extra={"files": files_map})
    with pytest.raises(ValueError):
        _install(installer, src)
    assert not (installer.plugins_dir / "DirPlugin").exists()


def test_dir_install_with_files_map_refuses_missing_declared(
        installer, tmp_path):
    files_map = {"ghost.txt": _sha(b"not on disk")}
    src = _write_dir_plugin(
        tmp_path / "src", {"declared.txt": b"present"},
        manifest_extra={"files": files_map})
    with pytest.raises(ValueError):
        _install(installer, src)
    assert not (installer.plugins_dir / "DirPlugin").exists()


def test_dir_install_with_matching_files_map_installs(installer, tmp_path):
    declared = {"declared.txt": b"shown", "sub/more.txt": b"more"}
    files_map = {n: _sha(d) for n, d in declared.items()}
    src = _write_dir_plugin(
        tmp_path / "src", declared, manifest_extra={"files": files_map})
    _install(installer, src)
    target = installer.plugins_dir / "DirPlugin"
    assert (target / "declared.txt").read_bytes() == b"shown"
    assert (target / "sub" / "more.txt").read_bytes() == b"more"


def test_dir_install_without_files_map_still_installs(installer, tmp_path):
    # The documented local-dev path: a folder the user pointed at
    # directly, manifest carrying no files map (the shape every
    # existing directory-install fixture uses). Symlink-free content
    # installs exactly as before.
    src = _write_dir_plugin(
        tmp_path / "src", {"code.py": b"x = 1\n", "README.md": b"hi"})
    _install(installer, src)
    target = installer.plugins_dir / "DirPlugin"
    assert (target / "code.py").read_bytes() == b"x = 1\n"
    assert (target / "README.md").read_bytes() == b"hi"
