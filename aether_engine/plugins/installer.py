import hashlib
import hmac
import json
import logging
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .manifest import PluginManifest

logger = logging.getLogger("aether_engine.plugins.installer")

# Phase 10.2 B2 — plugin install trust boundary.
#
# What was wrong (verified): ``install()`` called ``z.extractall(target_dir)``
# with no member sanitisation, and ``PluginManifest.name`` / capability
# names / ``file_path`` values were bare strings joined straight into
# filesystem paths. A zip containing ``../../evil`` was installed (CPython
# 3.12's zipfile silently strips ``..`` components, so the file landed
# *inside* the plugin dir at a path the manifest never declared), and a
# manifest with ``name="../../escape"`` installed the plugin OUTSIDE the
# plugins directory entirely. No hash or signature was checked on any zip.
#
# What this module now enforces, before anything is written to disk:
#   1. Zip-Slip guards: every member path is rejected if absolute, if it
#      contains a ``..`` component or a backslash, or if it is a symlink /
#      non-regular file. ``extractall`` is never called; members are written
#      one by one with a resolve()-based containment check as defence in
#      depth.
#   2. Allowlist: every non-manifest member must be declared in the
#      manifest's ``files`` map, and every declared file must be present.
#      Directory entries are allowed only as parents of declared files.
#      The manifest itself (``manifest.json``) must sit at the archive root
#      and is the trust anchor — it is not hashed (a self-hash is impossible).
#   3. Hash check: each member's bytes are SHA-256-hashed and compared
#      (constant-time) against the manifest's declared digest BEFORE
#      extraction. A manifest with no ``files`` map, a member missing from
#      it, or a digest mismatch refuses the install.
#
# Trust model (no PKI was invented): the ``files`` map is a
# publisher-declared integrity manifest. It detects post-packaging
# tampering or corruption; it does NOT authenticate the publisher, since
# anyone can author a self-consistent zip+manifest. The trust gate for
# *who* may install remains the user's explicit approval in the Phase 10.1
# A1 flow (``pending_plugin_installs``), which shows the manifest before
# ``install()`` runs. Directory sources are local folders the user pointed
# at directly, so no hash check applies to them — but name / file_path
# validation does (via the manifest validators).
#
# On any refusal the install raises ``ValueError`` before touching the
# target directory (an existing installed version is left untouched) and,
# when an audit logger is supplied, records a ``PLUGIN_INSTALL_REFUSED``
# event. Failures mid-install still roll back as before and now also emit
# ``PLUGIN_INSTALL_FAILED``. The logger is an optional constructor argument
# (same convention as ``aether_engine.workers.sandbox``); wire
# ``engine_state.audit_logger`` into ``PluginInstaller`` in
# ``aether_engine/state.py`` for the production path to emit these events.

_MANIFEST_MEMBER = "manifest.json"


def _read_mcp_config_file(source: Path, file_path: str) -> dict:
    """Read one MCP server config JSON from a plugin package (zip or dir).

    Raises ValueError with a named message when the file is missing,
    is not valid JSON, or does not contain a JSON object. Never invents
    a fallback config: a corrupt config is a validation failure, not a
    dummy server.

    Issue 011 (S4): *file_path* must stay inside the package. An
    absolute path or any ``..`` segment is rejected outright, and in
    the dir branch the resolved path must stay inside the resolved
    package directory — a crafted manifest must not read an arbitrary
    JSON file on disk as a server config (with an arbitrary
    ``command``). The same guard runs before the zip branch, where a
    ``..`` entry name would otherwise be looked up verbatim.
    """
    path = Path(file_path)
    if path.is_absolute():
        raise ValueError(
            f"MCP server config file '{file_path}' must be inside the "
            "plugin package: absolute paths are not allowed."
        )
    if ".." in path.parts:
        raise ValueError(
            f"MCP server config file '{file_path}' must be inside the "
            "plugin package: '..' segments are not allowed."
        )
    raw: bytes
    if source.is_file() and source.suffix == ".zip":
        try:
            with zipfile.ZipFile(source, "r") as z:
                try:
                    raw = z.read(file_path)
                except KeyError:
                    raise ValueError(
                        f"MCP server config file '{file_path}' not found "
                        "in plugin package."
                    )
        except zipfile.BadZipFile as e:
            raise ValueError(f"Plugin package is not a valid zip file: {e}")
    elif source.is_dir():
        cfg_path = (source / file_path).resolve()
        if not cfg_path.is_relative_to(source.resolve()):
            raise ValueError(
                f"MCP server config file '{file_path}' must be inside the "
                "plugin package."
            )
        if not cfg_path.exists():
            raise ValueError(
                f"MCP server config file '{file_path}' not found "
                "in plugin package."
            )
        raw = cfg_path.read_bytes()
    else:
        raise ValueError(f"Plugin source not found: {source}")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(
            f"MCP server config file '{file_path}' is not valid JSON: {e}"
        )
    if not isinstance(data, dict):
        raise ValueError(  # noqa: TRY004 — ValueError is the documented contract here; callers catch it
            f"MCP server config file '{file_path}' must contain a JSON object."
        )
    return data


def resolve_mcp_server_configs(source: Any, manifest: PluginManifest) -> list[tuple[str, dict]]:
    """Load + validate every MCP server a manifest declares.

    Validate first, add second: this returns the *normalised* configs
    (same schema as issue 001, ``validate_mcp_server_config``) and
    raises before the caller mutates anything — no file is copied, no
    skill installed, no server added — when any entry is invalid.
    Raises ValueError / MCPConfigError with a named message.
    """
    from aether_engine.mcp.registry import validate_mcp_server_config

    src_path = Path(source)
    resolved: list[tuple[str, dict]] = []
    for mcp_def in manifest.mcp_servers:
        if not mcp_def.file_path:
            raise ValueError(
                f"MCP server {mcp_def.name!r} declares no config file "
                "(file_path): a plugin MCP server needs a JSON config "
                "inside the package."
            )
        raw_config = _read_mcp_config_file(src_path, mcp_def.file_path)
        resolved.append(
            (mcp_def.name, validate_mcp_server_config(mcp_def.name, raw_config))
        )
    return resolved


class PluginInstaller:
    def __init__(self, plugins_dir: Path, audit_logger=None):
        self.plugins_dir = plugins_dir
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.registry_file = self.plugins_dir / "registry.json"
        self._registry: dict[str, Any] = self._load_registry()
        self.pending_installs: dict[str, dict] = {}
        self._audit_logger = audit_logger

    def _load_registry(self) -> dict[str, Any]:
        if self.registry_file.exists():
            import json
            try:
                return json.loads(self.registry_file.read_text())
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                return {}
        return {}

    def _save_registry(self):
        import json
        self.registry_file.write_text(json.dumps(self._registry, indent=2))

    # -- Phase 10.2 B2: refusal + zip validation -------------------------

    def _audit(self, action: str, plugin_name: str, reason: str,
               source: str | None = None) -> None:
        if self._audit_logger is None:
            return
        details: dict[str, Any] = {"plugin_name": plugin_name, "reason": reason}
        if source is not None:
            details["source"] = source
        self._audit_logger.log_event(action, details)

    def _refuse(self, reason: str, plugin_name: str = "unknown",
                source: str | None = None) -> None:
        """Refuse an install: audit event (when a logger is wired) + raise.

        Raised BEFORE any write, so a refused install leaves the target
        directory — and any previously installed version — untouched.
        """
        self._audit("PLUGIN_INSTALL_REFUSED", plugin_name, reason, source)
        raise ValueError(f"Plugin install refused: {reason}")

    @staticmethod
    def _check_member_path(member: str) -> PurePosixPath:
        """Reject any zip member path that could escape the target dir.

        Returns the path as PurePosixPath for callers that need its parts.
        Raises ValueError on absolute paths, drive/UNC prefixes, backslashes
        (a separator on Windows), NUL bytes, or any ``..`` component.
        """
        if not member or "\x00" in member or "\\" in member:
            raise ValueError(f"Unsafe zip member path: {member!r}")
        if member.startswith("/") or re.match(r"^[A-Za-z]:", member):
            raise ValueError(f"Absolute zip member path: {member!r}")
        path = PurePosixPath(member)
        if ".." in path.parts:
            raise ValueError(f"Zip member escapes target directory: {member!r}")
        return path

    @staticmethod
    def _is_dir_entry(info: zipfile.ZipInfo) -> bool:
        return info.filename.endswith("/")

    def _validate_zip_archive(self, source: Path, manifest: PluginManifest) -> None:
        """Validate a plugin zip fully BEFORE extraction.

        Checks, in order: root manifest present; every member path safe
        (Zip-Slip guards) and not a symlink/special file; every regular
        member allowlisted in ``manifest.files`` with a matching SHA-256;
        every declared file present. Raises ValueError (via ``_refuse``)
        on the first failure — nothing is extracted on any failure.
        """
        src = str(source)
        try:
            zf = zipfile.ZipFile(source, "r")
        except zipfile.BadZipFile as e:
            self._refuse(f"Not a valid zip archive: {e}",
                         manifest.name, src)
        with zf:
            infos = zf.infolist()
            names = [i.filename for i in infos]
            if _MANIFEST_MEMBER not in names:
                self._refuse("manifest.json must be at the archive root",
                             manifest.name, src)
            if not manifest.files:
                self._refuse(
                    "Unsigned plugin: manifest declares no file hashes "
                    "(files map is empty)", manifest.name, src)
            declared = set(manifest.files)
            seen: set = set()
            for info in infos:
                member = info.filename
                try:
                    self._check_member_path(member.rstrip("/"))
                except ValueError as e:
                    self._refuse(str(e), manifest.name, src)
                # Reject symlink / fifo / device entries: extractall would
                # materialise them as regular files, but a later step (or a
                # future extractor) could follow the link out of the target.
                mode = (info.external_attr >> 16) & 0o170000
                if mode and mode not in (0o100000, 0o040000):
                    self._refuse(
                        f"Zip member is not a regular file or directory: "
                        f"{member!r}", manifest.name, src)
                if member == _MANIFEST_MEMBER:
                    seen.add(member)
                    continue
                if self._is_dir_entry(info):
                    # Directory entries are allowed only as parents of a
                    # declared file (e.g. `skills/` for `skills/x.md`).
                    prefix = member.rstrip("/") + "/"
                    if not any(d.startswith(prefix) for d in declared):
                        self._refuse(
                            f"Zip directory {member!r} is not declared in "
                            f"the manifest files map", manifest.name, src)
                    continue
                if member not in declared:
                    self._refuse(
                        f"Zip member {member!r} is not declared in the "
                        f"manifest files map", manifest.name, src)
                seen.add(member)
                data = zf.read(member)
                actual = hashlib.sha256(data).hexdigest()
                expected = manifest.files[member]
                if not hmac.compare_digest(actual, expected):
                    self._refuse(
                        f"SHA-256 mismatch for zip member {member!r}: "
                        f"manifest declares {expected}, archive has "
                        f"{actual}", manifest.name, src)
            missing = declared - seen
            if missing:
                self._refuse(
                    f"Manifest declares files missing from the archive: "
                    f"{sorted(missing)}", manifest.name, src)

    @staticmethod
    def _safe_extract(source: Path, target_dir: Path) -> None:
        """Extract a previously validated zip, member by member.

        ``extractall`` is never used. Each destination is resolved and
        checked to stay inside ``target_dir`` (defence in depth — the
        members were already validated, so this must never fire).
        """
        target_root = target_dir.resolve()
        with zipfile.ZipFile(source, "r") as z:
            for info in z.infolist():
                member = info.filename
                if member.endswith("/"):
                    continue  # directory entries are created implicitly
                dest = target_root.joinpath(*PurePosixPath(member).parts)
                if not dest.resolve().is_relative_to(target_root):
                    raise ValueError(
                        f"Refusing to extract {member!r}: escapes target directory")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(member))

    @staticmethod
    def _contained_path(base: Path, relative: str, what: str) -> Path:
        """Join ``relative`` to ``base`` and prove the result stays inside.

        ``relative`` was already validated by the manifest validators (no
        ``..``, no absolute path); this resolve() check is defence in depth
        against symlinked path components.
        """
        dest = (base / relative).resolve()
        if not dest.is_relative_to(base.resolve()):
            raise ValueError(f"{what} {relative!r} escapes its directory")
        return dest

    def prepare_install(self, source: str) -> dict:
        """Parses manifest from source (zip or dir) and returns prepare data."""
        src_path = Path(source)
        if not src_path.exists():
            raise FileNotFoundError(f"Source not found: {source}")

        manifest_data = None
        if src_path.is_file() and src_path.suffix == ".zip":
            with zipfile.ZipFile(src_path, 'r') as z:
                # The manifest is the trust anchor: it must be at the root.
                if _MANIFEST_MEMBER not in z.namelist():
                    raise ValueError("Invalid plugin package: manifest.json not found")
                import json
                manifest_data = json.loads(z.read(_MANIFEST_MEMBER).decode())
        elif src_path.is_dir():
            m_path = src_path / "manifest.json"
            if m_path.exists():
                import json
                manifest_data = json.loads(m_path.read_text(encoding="utf-8"))

        if not manifest_data:
            raise ValueError("Invalid plugin package: manifest.json not found")

        # Pydantic validators reject unsafe plugin/capability names and
        # file_paths here, before the approval card is ever shown.
        manifest = PluginManifest(**manifest_data)
        if src_path.is_file() and src_path.suffix == ".zip":
            # Fail fast: refuse a bad zip before the user is asked to
            # approve it. install() re-validates (authoritative, before
            # extraction) in case the prepare data was tampered with.
            self._validate_zip_archive(src_path, manifest)
        # Issue 008: fail fast, before the approval step — a manifest
        # whose MCP servers do not validate is rejected here with the
        # named error, not after the user approves the install.
        resolve_mcp_server_configs(src_path, manifest)
        return {
            "source": source,
            "manifest": manifest.model_dump()
        }

    def install(self, prepare_data: dict):
        source = Path(prepare_data["source"])
        manifest = PluginManifest(**prepare_data["manifest"])
        plugin_name = manifest.name

        # Phase 10.2 B2: validate the zip (paths, allowlist, hashes) BEFORE
        # the backup step, so a refused install leaves any existing version
        # untouched and writes nothing anywhere.
        if source.is_file() and source.suffix == ".zip":
            self._validate_zip_archive(source, manifest)
        # Issue 008: validate FIRST, before any mutation — before the
        # backup move, before any file is copied, before any server is
        # added. A bad manifest fails here with the named error and the
        # previous install (if any) is left untouched.
        try:
            mcp_configs = resolve_mcp_server_configs(source, manifest)
        except Exception as e:  # noqa: BLE001 — install boundary: any validation failure becomes the rollback RuntimeError
            raise RuntimeError(f"Installation failed, rolled back. Error: {e}")

        target_dir = self.plugins_dir / plugin_name

        # Rollback mechanism
        backup = None
        if target_dir.exists():
            backup = self.plugins_dir / f"{plugin_name}_backup"
            if backup.exists():
                shutil.rmtree(backup)
            shutil.move(str(target_dir), str(backup))

        installed_skills = []
        installed_tools = []
        installed_mcp = []

        from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            if source.is_file() and source.suffix == ".zip":
                self._safe_extract(source, target_dir)
            elif source.is_dir():
                # Copy contents
                for item in source.iterdir():
                    s = item
                    d = target_dir / item.name
                    if s.is_dir():
                        shutil.copytree(s, d)
                    else:
                        shutil.copy2(s, d)

            # 1. Install Skills
            skills_dest = Path.home() / ".aether" / "skills"
            skills_dest.mkdir(parents=True, exist_ok=True)
            for skill in manifest.skills:
                if skill.file_path:
                    src_skill = self._contained_path(
                        target_dir, skill.file_path, "skill file_path")
                    if src_skill.exists():
                        dest_skill_dir = skills_dest / skill.name
                        dest_skill_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_skill, dest_skill_dir / "SKILL.md")
                        installed_skills.append(dest_skill_dir)

            # 2. Record Tools (no global registry)
            for tool_def in manifest.tools:
                installed_tools.append(tool_def.name)
                
            # 3. Install MCP Servers — configs were already loaded and
            # validated (issue 008) before any mutation above, so this
            # only adds the pre-validated configs. No re-reading, no
            # dummy fallback: a corrupt config can never become a
            # silently installed server.
            for mcp_name, config_dict in mcp_configs:
                GLOBAL_MCP_REGISTRY.add_server(mcp_name, config_dict)
                installed_mcp.append(mcp_name)

            self._registry[plugin_name] = {
                "manifest": manifest.model_dump(),
                "enabled": True,
                "installed_skills": [str(p) for p in installed_skills],
                "installed_tools": installed_tools,
                "installed_mcp": installed_mcp
            }
            self._save_registry()

            if backup and backup.exists():
                shutil.rmtree(backup)

        except Exception as e:  # noqa: BLE001 — rollback handler: must catch everything to restore the previous install
            # Atomic Rollback
            if target_dir.exists():
                shutil.rmtree(target_dir)
            if backup and backup.exists():
                shutil.move(str(backup), str(target_dir))

            for skill_dir in installed_skills:
                if skill_dir.exists():
                    shutil.rmtree(skill_dir)
            for mcp_name in installed_mcp:
                # Issue 011 (S6): a connected server refuses a sync
                # remove — that refusal must not mask the install
                # failure being rolled back, so it is best-effort here.
                try:
                    GLOBAL_MCP_REGISTRY.remove_server_sync(mcp_name)
                except Exception as remove_err:  # noqa: BLE001 — best-effort rollback cleanup (issue 011 S6); must not mask the install failure
                    logger.debug(
                        "Rollback could not remove MCP server %s: %s",
                        mcp_name, remove_err,
                    )

            self._audit("PLUGIN_INSTALL_FAILED", plugin_name, str(e),
                        str(source))
            raise RuntimeError(f"Installation failed, rolled back. Error: {e}")

    def uninstall(self, plugin_name: str):
        if plugin_name not in self._registry:
            return

        data = self._registry[plugin_name]

        # Remove MCP
        from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
        for mcp_name in data.get("installed_mcp", []):
            GLOBAL_MCP_REGISTRY.remove_server_sync(mcp_name)

        # Remove skills
        for skill_path_str in data.get("installed_skills", []):
            skill_dir = Path(skill_path_str)
            if skill_dir.exists():
                shutil.rmtree(skill_dir)

        # Remove plugin files
        target_dir = self.plugins_dir / plugin_name
        if target_dir.exists():
            shutil.rmtree(target_dir)

        del self._registry[plugin_name]
        self._save_registry()

    def toggle(self, plugin_name: str, enabled: bool):
        if plugin_name in self._registry:
            self._registry[plugin_name]["enabled"] = enabled
            self._save_registry()

            # Since toggle doesn't uninstall, if disabled we might want to unregister tools temporarily.
            # For now, toggling is just state update, and the engine logic should check enabled state.
        else:
            raise KeyError(f"Plugin not found: {plugin_name}")

    def get_all(self) -> dict[str, Any]:
        return self._registry
