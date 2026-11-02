"""Phase 10.2 B2: plugin install Zip-Slip / traversal / unsigned code.

Regression tests for ``aether_engine/plugins/installer.py``: before the fix,
``install()`` called ``z.extractall(target_dir)`` with no member
sanitisation, manifest/skill names were unvalidated, and no hash was
checked — a zip could plant undeclared files in the plugin dir, a manifest
with ``name="../../escape"`` installed the plugin outside the plugins
directory entirely, and any zip was trusted on arrival. These tests fail
against the pre-fix code (verified via ``git stash`` of the fix) and pass
after it.
"""
import hashlib
import json
import stat
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from aether_engine.audit import AuditLogger
from aether_engine.plugins.installer import PluginInstaller


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest(name="GoodPlugin", files=None, **extra):
    m = {"name": name, "version": "1.0"}
    if files is not None:
        m["files"] = files
    m.update(extra)
    return m


def _write_zip(path: Path, manifest: dict, members: dict) -> Path:
    """Write a plugin zip: manifest.json + ``members`` (name -> bytes)."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        for name, data in members.items():
            z.writestr(name, data)
    return path


def _write_signed_zip(path: Path, manifest: dict, members: dict) -> Path:
    """Write a zip whose manifest ``files`` map matches the members."""
    manifest = dict(manifest)
    manifest["files"] = {n: _sha(d) for n, d in members.items()}
    return _write_zip(path, manifest, members)


@pytest.fixture
def installer(tmp_path, monkeypatch):
    # install() writes skills under Path.home() / ".aether" — keep that in tmp.
    monkeypatch.setenv("HOME", str(tmp_path))
    return PluginInstaller(tmp_path / "plugins")


def _install_directly(installer, zip_path, manifest):
    """Call install() with hand-built prepare data (bypasses prepare_install).

    Proves install() itself refuses — the check must not live only in the
    prepare step, since prepare data could be forged between the two calls.
    """
    installer.install({"source": str(zip_path), "manifest": manifest})


# --- Zip-Slip / traversal ------------------------------------------------

def test_zip_with_parent_traversal_member_is_refused(tmp_path, installer):
    evil = tmp_path / "outside"
    evil.mkdir()
    # The traversal member is deliberately NOT in the files map (the
    # manifest validator would reject such a key outright); the installer's
    # member-path check is what must fire, before the allowlist check.
    members = {"skill.md": b"skill", "../../outside/evil.txt": b"pwned"}
    manifest = _manifest(files={"skill.md": _sha(b"skill")})
    zip_path = _write_zip(tmp_path / "evil.zip", manifest, members)

    with pytest.raises(ValueError, match="escapes target directory"):
        _install_directly(installer, zip_path, manifest)

    assert not (evil / "evil.txt").exists()
    assert not (tmp_path / "plugins" / "GoodPlugin").exists()
    assert "GoodPlugin" not in installer.get_all()


def test_zip_with_absolute_member_is_refused(tmp_path, installer):
    members = {"skill.md": b"skill", "/tmp/abs_evil.txt": b"pwned"}
    manifest = _manifest(files={"skill.md": _sha(b"skill")})
    zip_path = _write_zip(tmp_path / "evil.zip", manifest, members)

    with pytest.raises(ValueError, match="Absolute zip member"):
        _install_directly(installer, zip_path, manifest)

    assert not (tmp_path / "plugins" / "GoodPlugin").exists()


def test_zip_with_symlink_member_is_refused(tmp_path, installer):
    # The symlink member carries a correct hash, so the symlink check — not
    # the allowlist or hash check — is what must refuse it.
    manifest = _manifest(files={
        "skill.md": _sha(b"skill"),
        "link.md": _sha(b"../../outside/evil.txt"),
    })
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("skill.md", b"skill")
        info = zipfile.ZipInfo("link.md")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, b"../../outside/evil.txt")

    with pytest.raises(ValueError, match="not a regular file"):
        _install_directly(installer, zip_path, manifest)

    assert not (tmp_path / "plugins" / "GoodPlugin").exists()


def test_prepare_install_refuses_traversal_zip_before_approval(tmp_path, installer):
    # The normal flow refuses in prepare_install, so the user is never
    # asked to approve a zip that cannot install.
    members = {"skill.md": b"skill", "../evil.txt": b"pwned"}
    manifest = _manifest(files={"skill.md": _sha(b"skill")})
    zip_path = _write_zip(tmp_path / "evil.zip", manifest, members)

    with pytest.raises(ValueError, match="escapes target directory"):
        installer.prepare_install(str(zip_path))


# --- Unsigned / bad-hash ---------------------------------------------------

def test_zip_with_bad_hash_is_refused(tmp_path, installer):
    members = {"skill.md": b"skill"}
    manifest = _manifest(files={"skill.md": "0" * 64})
    zip_path = _write_zip(tmp_path / "bad.zip", manifest, members)

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _install_directly(installer, zip_path, manifest)

    assert not (tmp_path / "plugins" / "GoodPlugin").exists()
    assert "GoodPlugin" not in installer.get_all()


def test_zip_with_undeclared_member_is_refused(tmp_path, installer):
    members = {"skill.md": b"skill", "undeclared.py": b"print('hi')"}
    manifest = _manifest(files={"skill.md": _sha(b"skill")})
    zip_path = _write_zip(tmp_path / "undeclared.zip", manifest, members)

    with pytest.raises(ValueError, match="not declared in the manifest"):
        _install_directly(installer, zip_path, manifest)

    assert not (tmp_path / "plugins" / "GoodPlugin").exists()


def test_zip_with_unsigned_manifest_is_refused(tmp_path, installer):
    members = {"skill.md": b"skill"}
    manifest = _manifest()  # no "files" map at all
    zip_path = _write_zip(tmp_path / "unsigned.zip", manifest, members)

    with pytest.raises(ValueError, match="Unsigned plugin"):
        _install_directly(installer, zip_path, manifest)

    assert not (tmp_path / "plugins" / "GoodPlugin").exists()


def test_zip_missing_a_declared_file_is_refused(tmp_path, installer):
    members = {"skill.md": b"skill"}
    manifest = _manifest(files={
        "skill.md": _sha(b"skill"),
        "missing.md": _sha(b"not in the zip"),
    })
    zip_path = _write_zip(tmp_path / "missing.zip", manifest, members)

    with pytest.raises(ValueError, match="missing from the archive"):
        _install_directly(installer, zip_path, manifest)


# --- Unvalidated names -----------------------------------------------------

def test_traversal_plugin_name_is_refused(tmp_path, installer):
    src = tmp_path / "src"
    src.mkdir()
    (src / "manifest.json").write_text(json.dumps(
        {"name": "../../escape", "version": "1.0"}))

    with pytest.raises(ValidationError):
        installer.prepare_install(str(src))

    assert not (tmp_path / "escape").exists()
    assert not (tmp_path / "plugins" / "escape").exists()


def test_traversal_skill_name_and_file_path_are_refused(tmp_path, installer):
    src = tmp_path / "src"
    src.mkdir()
    (src / "manifest.json").write_text(json.dumps({
        "name": "GoodPlugin", "version": "1.0",
        "skills": [{"name": "../../evil_skill", "file_path": "sk.md"}],
    }))
    with pytest.raises(ValidationError):
        installer.prepare_install(str(src))

    (src / "manifest.json").write_text(json.dumps({
        "name": "GoodPlugin", "version": "1.0",
        "skills": [{"name": "sk", "file_path": "../../evil.md"}],
    }))
    with pytest.raises(ValidationError):
        installer.prepare_install(str(src))


# --- Refusal leaves prior installs alone + audit event ----------------------

def test_refused_install_leaves_existing_version_untouched(tmp_path, installer):
    members = {"skill.md": b"v1 skill"}
    manifest = _manifest(files={"skill.md": _sha(b"v1 skill")})
    good_zip = _write_signed_zip(tmp_path / "good.zip", manifest, members)
    installer.install(installer.prepare_install(str(good_zip)))
    assert (tmp_path / "plugins" / "GoodPlugin" / "skill.md").read_bytes() == b"v1 skill"

    bad_members = {"skill.md": b"v1 skill", "../../outside/evil.txt": b"pwned"}
    bad_manifest = _manifest(files={"skill.md": _sha(b"v1 skill")})
    bad_zip = _write_zip(tmp_path / "bad.zip", bad_manifest, bad_members)
    with pytest.raises(ValueError):
        _install_directly(installer, bad_zip, bad_manifest)

    # v1 still installed, byte-identical; nothing leaked outside.
    assert (tmp_path / "plugins" / "GoodPlugin" / "skill.md").read_bytes() == b"v1 skill"
    assert "GoodPlugin" in installer.get_all()


def test_refusal_emits_audit_event(tmp_path, monkeypatch):
    audit_log = tmp_path / "audit.log"
    monkeypatch.setenv("HOME", str(tmp_path))
    installer = PluginInstaller(
        tmp_path / "plugins", audit_logger=AuditLogger(audit_log))
    members = {"skill.md": b"skill"}
    manifest = _manifest(files={"skill.md": "0" * 64})
    zip_path = _write_zip(tmp_path / "bad.zip", manifest, members)

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _install_directly(installer, zip_path, manifest)

    events = [json.loads(line) for line in audit_log.read_text().splitlines()]
    refused = [e for e in events if e.get("action") == "PLUGIN_INSTALL_REFUSED"]
    assert len(refused) == 1
    assert refused[0]["details"]["plugin_name"] == "GoodPlugin"
    assert "SHA-256 mismatch" in refused[0]["details"]["reason"]


# --- Legitimate installs still work ------------------------------------------

def test_valid_plugin_zip_still_installs(tmp_path, installer):
    members = {
        "skill.md": b"# my skill",
        "config.json": b'{"command": "run"}',
    }
    manifest = _manifest(
        files={n: _sha(d) for n, d in members.items()},
        skills=[{"name": "sk1", "file_path": "skill.md"}],
        mcp_servers=[{"name": "srv1", "file_path": "config.json"}],
    )
    zip_path = _write_signed_zip(tmp_path / "good.zip", manifest, members)

    data = installer.prepare_install(str(zip_path))
    installer.install(data)

    target = tmp_path / "plugins" / "GoodPlugin"
    assert (target / "skill.md").read_bytes() == b"# my skill"
    assert (target / "config.json").read_bytes() == b'{"command": "run"}'
    assert (target / "manifest.json").exists()
    assert "GoodPlugin" in installer.get_all()
    # Skill file copied to the skills dir under the tmp HOME.
    assert (tmp_path / ".aether" / "skills" / "sk1" / "SKILL.md").read_bytes() == b"# my skill"


def test_valid_directory_install_still_works_without_hashes(tmp_path, installer):
    # Directory sources are local folders the user pointed at directly, so
    # no hash check applies to them (documented migration choice: only the
    # zip distribution channel requires hashes). Existing directory-based
    # fixtures keep working unchanged.
    src = tmp_path / "src"
    src.mkdir()
    (src / "manifest.json").write_text(json.dumps({
        "name": "DirPlugin", "version": "1.0",
        "skills": [{"name": "sk1", "file_path": "sk1.md"}],
    }))
    (src / "sk1.md").write_text("skill_content")

    installer.install(installer.prepare_install(str(src)))

    assert (tmp_path / "plugins" / "DirPlugin").exists()
    assert "DirPlugin" in installer.get_all()
    assert (tmp_path / ".aether" / "skills" / "sk1" / "SKILL.md").exists()
