import asyncio
import copy
import hashlib
import hmac
import json
import logging
import os
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
# at directly; name / file_path validation applies to them (via the
# manifest validators), and since N-29 they are gated in
# ``_validate_dir_source``: symlinks are refused unconditionally (the
# copy dereferences them, which would import content from outside the
# folder the user pointed at), and when a directory's manifest DOES
# declare a ``files`` map the full allowlist/hash gate applies to it
# exactly as for a zip. Only a manifest with no files map — the
# local-dev shape — installs on name/path validation alone.
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

# N-17: zip size governance. The manifest's hash/allowlist checks
# cannot stop a zip bomb — the attacker authors a self-consistent
# manifest — so sizes are gated on the central directory's DECLARED
# uncompressed sizes, before any member is decompressed, and again
# by byte accounting during extraction (defence in depth). A plugin
# package is code, markdown and small assets; these caps sit far
# above any legitimate package and far below a disk/RAM threat.
PLUGIN_ZIP_MAX_MEMBERS = 4096
PLUGIN_ZIP_MAX_MEMBER_BYTES = 64 * 1024 * 1024
PLUGIN_ZIP_MAX_TOTAL_BYTES = 256 * 1024 * 1024

# Extraction streams members in chunks of this size; a member is
# never held whole in memory by the extractor.
_EXTRACT_CHUNK_BYTES = 256 * 1024


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
                    # N-17: same size gate as every other pre-validation
                    # read — a config member is small JSON by nature.
                    if z.getinfo(file_path).file_size > (
                            PLUGIN_ZIP_MAX_MEMBER_BYTES):
                        raise ValueError(
                            f"MCP server config file '{file_path}' declares "
                            f"over {PLUGIN_ZIP_MAX_MEMBER_BYTES} uncompressed "
                            "bytes."
                        )
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


def mcp_server_summary(name: str, config: dict) -> dict:
    """The approval-card summary of one resolved MCP server config.

    What the trust card may show (PC-11): the command line the
    server runs (or the URL it connects to), its working folder,
    and the NAMES of the environment variables / headers it is
    given. Values of env vars and headers are never included —
    they are the secret carriers (PC-12: secrets live in the
    vault, never in a rendered card or a plugin payload the UI
    displays back).
    """
    summary: dict[str, Any] = {"name": name}
    command = config.get("command")
    if command:
        args = [str(a) for a in (config.get("args") or [])]
        summary["runs"] = " ".join([str(command), *args])
    url = config.get("url")
    if url:
        summary["url"] = str(url)
    if config.get("cwd"):
        summary["cwd"] = str(config["cwd"])
    env = config.get("env") or {}
    if isinstance(env, dict) and env:
        summary["env_keys"] = sorted(str(k) for k in env.keys())
    headers = config.get("headers") or {}
    if isinstance(headers, dict) and headers:
        summary["header_keys"] = sorted(str(k) for k in headers.keys())
    return summary


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
        config = validate_mcp_server_config(mcp_def.name, raw_config)
        # A plugin's servers are confirmed by the plugin's own
        # approval gate: the user approves the bundle — its MCP
        # servers included — before install runs, so the resolved
        # configs carry the marker the MCP auto-connect gate
        # requires. (Prepare calls this too, but only to validate;
        # nothing is persisted from a prepare.)
        config["confirmed"] = True
        resolved.append((mcp_def.name, config))
    return resolved


# ---------------------------------------------------------------------------
# E7-F08 — capability ownership and conflict handling.
#
# What was wrong: install copied skills into a bare global skill name and
# assigned MCP configs by bare server name with no check against any
# other owner's claim, so a second plugin's body/config silently replaced
# the first's — and disable/uninstall then removed those bare names
# without checking who owned them NOW, deleting the surviving plugin's
# live registrations. There was no ownership record at all.
#
# The model added here (the smallest one that closes the repro):
#
# * Every successful install records CLAIMS in its registry entry:
#   ``skill_claims`` / ``mcp_claims`` map each capability name to the
#   SHA-256 fingerprint of what was installed (skill body bytes /
#   canonical normalised config), plus a ``preexisting`` flag when the
#   identical content was already there, unowned by any plugin, before
#   this install (user content the plugin merely matches).
# * Install (and prepare) REFUSES a claim whose live content differs
#   from the incoming content when any other plugin owns the name, or
#   when the content is unowned user content — no silent overwrite.
#   Identical content collapses into a shared claim. A plugin may
#   always overwrite its OWN claims (upgrades).
# * Teardown (disable / uninstall / upgrade stale-removal) removes a
#   registration only when (a) no other ENABLED plugin claims the name,
#   (b) the claim is not pre-existing user content, and (c) the live
#   content still matches the fingerprint this plugin installed.
#   Anything else is left in place with a warning — teardown can never
#   destroy another owner's live registration.
# * Entries written before claims existed (no claim maps) keep the old
#   bare-name teardown behaviour for their OWN artefacts, but still
#   count as claimants for other plugins' conflict checks.
# * A qualified ``<ns>/<name>`` skill name is logical only: on disk it
#   is flattened to one segment (``ns+name`` — '+' is outside the safe
#   name alphabet, so a flattened dir can never collide with, or be
#   produced by, a bare name). The logical name is what the skill
#   manager indexes and what claims are keyed by.
# ---------------------------------------------------------------------------


class PluginConflictError(ValueError):
    """A capability claim collides with another owner's live content."""


def _fingerprint_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fingerprint_config(config: dict) -> str:
    # ``confirmed`` is Aether-side trust metadata (whether the user
    # confirmed the server's add), not part of what the server is or
    # runs — two configs differing only in it are the same server,
    # so the key is excluded from the content fingerprint. Without
    # this, adding the marker would have invalidated every claim
    # recorded before it existed.
    content = {k: v for k, v in config.items() if k != "confirmed"}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _skill_dir_name(skill_name: str) -> str:
    """On-disk directory name for a skill: the logical name with the
    qualified-name separator flattened to '+', a single safe segment."""
    return skill_name.replace("/", "+")


class PluginInstaller:
    def __init__(self, plugins_dir: Path, audit_logger=None):
        self.plugins_dir = plugins_dir
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.registry_file = self.plugins_dir / "registry.json"
        self._registry_load_error: str | None = None
        self._registry: dict[str, Any] = self._load_registry()
        self.pending_installs: dict[str, dict] = {}
        self._audit_logger = audit_logger

    # -- Registry persistence ------------------------------------------
    # This store follows the same file discipline as its sibling stores
    # (mcp/registry.py, skills/manager.py, config.py): a corrupt file is
    # quarantined to a .bak and surfaced, never silently replaced, and
    # a save is an atomic temp-file write that is flushed and fsynced
    # before os.replace() swaps it in.

    @property
    def registry_load_error(self) -> str | None:
        """Error from loading the on-disk registry, or None if it loaded."""
        return self._registry_load_error

    def _record_registry_load_error(self, detail: str) -> None:
        self._registry_load_error = (
            f"Plugin registry file {self.registry_file} is corrupt and "
            f"was not loaded: {detail}"
        )
        try:
            backup = self.registry_file.with_name(self.registry_file.name + ".bak")
            shutil.copyfile(self.registry_file, backup)
            self._registry_load_error += f" A backup was saved to {backup}."
        except OSError:
            pass
        logger.error(self._registry_load_error)

    def _load_registry(self) -> dict[str, Any]:
        if not self.registry_file.exists():
            return {}
        try:
            raw = json.loads(self.registry_file.read_bytes())
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            self._record_registry_load_error(str(e))
            return {}
        if not isinstance(raw, dict):
            self._record_registry_load_error("top level must be an object")
            return {}
        return raw

    def _save_registry(self):
        if self._registry_load_error is not None:
            # The registry on disk is corrupt and was refused at load.
            # Saving the empty in-memory registry would permanently
            # destroy the ownership/claim records it contains.
            raise ValueError(
                "Plugin registry file is corrupt and was not loaded; fix "
                "or delete it (a .bak backup was saved) before saving "
                "plugin changes."
            )
        # Atomic write (temp file + os.replace in the same directory):
        # a failed save leaves the previous registry file fully intact,
        # so on-disk state can never be a half-written mix of the old
        # and new entries. install()'s rollback relies on this. The
        # temp file is flushed and fsynced before the replace — the
        # discipline every sibling store follows (E6-NEW-14: this save
        # used to rename an unsynced file, so a crash could leave the
        # "replaced" registry empty on disk) — and is removed again if
        # the write fails partway.
        tmp_file = self.registry_file.with_name(self.registry_file.name + ".tmp")
        try:
            with tmp_file.open("w", encoding="utf-8") as f:
                f.write(json.dumps(self._registry, indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, self.registry_file)
        except BaseException:
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    # -- E7-F08: capability ownership / conflicts ----------------------

    def _read_package_bytes(self, source: Path, file_path: str) -> bytes | None:
        """Raw bytes of one package file, or None when unreadable.

        Same containment guards as ``_read_mcp_config_file``. Returning
        None (instead of raising) mirrors install's existing tolerance:
        a skill file that cannot be read is skipped by install, so the
        conflict check simply has nothing to check for it.
        """
        path = Path(file_path)
        if path.is_absolute() or ".." in path.parts:
            return None
        try:
            if source.is_file() and source.suffix == ".zip":
                with zipfile.ZipFile(source, "r") as z:
                    try:
                        return z.read(file_path)
                    except KeyError:
                        return None
            elif source.is_dir():
                target = (source / file_path).resolve()
                if not target.is_relative_to(source.resolve()):
                    return None
                if not target.exists():
                    return None
                return target.read_bytes()
        except (OSError, zipfile.BadZipFile):
            return None
        return None

    def _entry_claim(self, entry: dict, kind: str, name: str) -> dict | None:
        """The recorded E7-F08 claim of ``entry`` on a capability, or
        None when the entry predates claims (or does not claim it)."""
        key = "skill_claims" if kind == "skill" else "mcp_claims"
        raw = (entry.get(key) or {}).get(name)
        if isinstance(raw, dict) and "sha256" in raw:
            return {"sha256": raw["sha256"],
                    "preexisting": bool(raw.get("preexisting"))}
        return None

    def _entry_claims_name(self, entry: dict, kind: str, name: str) -> bool:
        """Does this registry entry claim the capability at all?
        Legacy entries (no claim maps) claim via their installed
        artefact lists — the only record they have."""
        if self._entry_claim(entry, kind, name) is not None:
            return True
        if kind == "skill":
            return any(
                Path(p).name == _skill_dir_name(name)
                for p in entry.get("installed_skills", [])
            )
        return name in entry.get("installed_mcp", [])

    def _claimants(self, kind: str, name: str, exclude: str) -> list[str]:
        return [
            plugin for plugin, entry in self._registry.items()
            if plugin != exclude and self._entry_claims_name(entry, kind, name)
        ]

    def _teardown_allowed(
        self, plugin_name: str, entry: dict, kind: str, name: str,
        current_fp: str | None,
    ) -> bool:
        """E7-F08 ownership check for removing one live registration.

        Removal is allowed only when no other ENABLED plugin claims
        the name, the departing plugin's claim is not pre-existing
        user content, and the live content still matches the
        fingerprint that plugin installed. Legacy entries (no
        recorded claim) keep the old bare-name behaviour for their
        own artefacts.
        """
        for other, other_entry in self._registry.items():
            if other == plugin_name:
                continue
            if not other_entry.get("enabled", True):
                continue
            if self._entry_claims_name(other_entry, kind, name):
                return False
        claim = self._entry_claim(entry, kind, name)
        if claim is None:
            return True
        if claim["preexisting"]:
            return False
        if current_fp is None:
            return False
        return current_fp == claim["sha256"]

    def _check_capability_conflicts(
        self, source: Path, manifest: PluginManifest,
        mcp_configs: list[tuple[str, dict]],
    ) -> dict[str, dict]:
        """Refuse claims that would silently overwrite another owner.

        Returns the claim plan to record on a successful install:
        ``{"skills": {name: claim}, "mcp": {name: claim}}``. Raises
        PluginConflictError (audited as PLUGIN_INSTALL_REFUSED) when a
        skill body or server config differs from live content owned
        by another plugin or by no plugin (user content). Identical
        content collapses; a plugin's own claims never conflict.
        """
        from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY

        skills_dest = Path.home() / ".aether" / "skills"
        own_entry = self._registry.get(manifest.name) or {}
        plan: dict[str, dict] = {"skills": {}, "mcp": {}}

        def owner_text(claimants: list[str]) -> str:
            return (f"plugin '{claimants[0]}'" if claimants
                    else "content not owned by any plugin")

        for skill in manifest.skills:
            if not skill.file_path:
                continue
            raw = self._read_package_bytes(source, skill.file_path)
            if raw is None:
                continue
            fp = _fingerprint_bytes(raw)
            claimants = self._claimants("skill", skill.name, manifest.name)
            own = self._entry_claims_name(own_entry, "skill", skill.name)
            dest = skills_dest / _skill_dir_name(skill.name)
            if not dest.exists():
                plan["skills"][skill.name] = {"sha256": fp, "preexisting": False}
                continue
            body_path = dest / "SKILL.md"
            current_fp = (
                _fingerprint_bytes(body_path.read_bytes())
                if body_path.exists() else None
            )
            if current_fp == fp:
                plan["skills"][skill.name] = {
                    "sha256": fp,
                    "preexisting": not claimants and not own,
                }
            elif own and not claimants:
                plan["skills"][skill.name] = {"sha256": fp, "preexisting": False}
            else:
                reason = (
                    f"skill '{skill.name}' is already provided by "
                    f"{owner_text(claimants)} with different content; "
                    "refusing to overwrite it"
                )
                self._audit("PLUGIN_INSTALL_REFUSED", manifest.name,
                            reason, str(source))
                raise PluginConflictError(
                    f"Plugin capability conflict: {reason}")

        for name, config in mcp_configs:
            fp = _fingerprint_config(config)
            claimants = self._claimants("mcp", name, manifest.name)
            own = self._entry_claims_name(own_entry, "mcp", name)
            existing = GLOBAL_MCP_REGISTRY.get_server(name)
            if existing is None:
                plan["mcp"][name] = {"sha256": fp, "preexisting": False}
            elif _fingerprint_config(existing) == fp:
                plan["mcp"][name] = {
                    "sha256": fp,
                    "preexisting": not claimants and not own,
                }
            elif own and not claimants:
                plan["mcp"][name] = {"sha256": fp, "preexisting": False}
            else:
                reason = (
                    f"MCP server '{name}' is already registered by "
                    f"{owner_text(claimants)} with a different "
                    "configuration; refusing to overwrite it"
                )
                self._audit("PLUGIN_INSTALL_REFUSED", manifest.name,
                            reason, str(source))
                raise PluginConflictError(
                    f"Plugin capability conflict: {reason}")
        return plan

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

    def _refuse_declared_tools(self, manifest: PluginManifest,
                               source: Any) -> None:
        """Refuse a manifest that declares ``tools`` (ADR 0035).

        A plugin is a bundle of skills + MCP servers (+ agents /
        hooks) — never in-engine code — so a tool a plugin wants to
        offer must arrive through an MCP server the plugin bundles,
        declared under ``mcp_servers``. A manifest declaring tools
        directly is refused here, at prepare time (and again in
        install(), authoritatively), instead of the tools being
        recorded and silently never callable.
        """
        if not manifest.tools:
            return
        names = ", ".join(tool.name for tool in manifest.tools)
        self._refuse(
            f"plugin '{manifest.name}' declares tool(s): {names}. "
            "Plugins cannot contribute tools directly — a tool must "
            "be exposed through an MCP server bundled with the "
            "plugin (declared under mcp_servers), never as "
            "in-engine code. Remove the tools declaration or ship "
            "the tools as an MCP server instead.",
            manifest.name, str(source))

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
            # N-17: size governance comes FIRST, judged on the
            # central directory's declared uncompressed sizes — a
            # bomb is refused before a single member is read into
            # memory, however honest its hashes are.
            if len(infos) > PLUGIN_ZIP_MAX_MEMBERS:
                self._refuse(
                    f"Zip declares {len(infos)} members, over the "
                    f"{PLUGIN_ZIP_MAX_MEMBERS}-member limit",
                    manifest.name, src)
            declared_total = 0
            for info in infos:
                if self._is_dir_entry(info):
                    continue
                if info.file_size > PLUGIN_ZIP_MAX_MEMBER_BYTES:
                    self._refuse(
                        f"Zip member {info.filename!r} declares "
                        f"{info.file_size} uncompressed bytes, over "
                        f"the {PLUGIN_ZIP_MAX_MEMBER_BYTES}-byte "
                        "per-member limit", manifest.name, src)
                declared_total += info.file_size
                if declared_total > PLUGIN_ZIP_MAX_TOTAL_BYTES:
                    self._refuse(
                        f"Zip members declare over "
                        f"{PLUGIN_ZIP_MAX_TOTAL_BYTES} uncompressed "
                        "bytes in total", manifest.name, src)
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

    def _validate_dir_source(self, source: Path, manifest: PluginManifest) -> None:
        """Validate a directory plugin source BEFORE anything is copied.

        N-29: directory installs used to copy the tree with
        ``copytree``/``copy2`` unchecked, and both dereference
        symlinks — so undeclared files and content from OUTSIDE the
        folder (a symlinked ``~/.ssh`` file) entered the installed
        tree, none of it shown on the approval card. Two gates now
        apply, mirroring the zip path as far as a folder allows:

        1. No symlinks anywhere in the tree, and no non-regular
           files (a fifo would block the copy; a device is not
           package content). Refused, never followed — unconditionally,
           whatever the manifest declares.
        2. When the manifest declares a ``files`` map, the zip
           allowlist applies in full: every regular file on disk must
           be declared with a matching SHA-256 and every declared
           file must be present. ``manifest.json`` is exempt, exactly
           as in the zip flow (the trust anchor cannot hash itself).

        A manifest with NO files map is the documented local-dev
        shape (see the header trust model): gate 1 still applies;
        the hash gate has nothing to judge against.
        """
        src = str(source)
        on_disk: dict[str, Path] = {}
        for dirpath, dirnames, filenames in os.walk(source):
            for entry_name in (*dirnames, *filenames):
                entry = Path(dirpath) / entry_name
                if entry.is_symlink():
                    self._refuse(
                        f"Directory plugin source contains a symlink: "
                        f"{entry.relative_to(source).as_posix()!r} — "
                        "directory installs never follow symlinks, so "
                        "content outside the folder the user pointed "
                        "at cannot enter the installed plugin",
                        manifest.name, src)
            for entry_name in filenames:
                entry = Path(dirpath) / entry_name
                if not entry.is_file():
                    self._refuse(
                        f"Directory plugin source contains a "
                        f"non-regular file: "
                        f"{entry.relative_to(source).as_posix()!r}",
                        manifest.name, src)
                rel = entry.relative_to(source).as_posix()
                if rel != _MANIFEST_MEMBER:
                    on_disk[rel] = entry
        if not manifest.files:
            return
        declared = set(manifest.files)
        undeclared = sorted(set(on_disk) - declared)
        if undeclared:
            self._refuse(
                f"Directory plugin file {undeclared[0]!r} is not "
                "declared in the manifest files map",
                manifest.name, src)
        missing = sorted(declared - set(on_disk))
        if missing:
            self._refuse(
                f"Manifest declares files missing from the "
                f"directory: {missing}", manifest.name, src)
        for rel, entry in sorted(on_disk.items()):
            actual = hashlib.sha256(entry.read_bytes()).hexdigest()
            expected = manifest.files[rel]
            if not hmac.compare_digest(actual, expected):
                self._refuse(
                    f"SHA-256 mismatch for directory plugin file "
                    f"{rel!r}: manifest declares {expected}, directory "
                    f"has {actual}", manifest.name, src)

    @staticmethod
    def _safe_extract(
        source: Path,
        target_dir: Path,
        expected_hashes: dict[str, str] | None = None,
    ) -> None:
        """Extract a previously validated zip, member by member.

        ``extractall`` is never used. Each destination is resolved and
        checked to stay inside ``target_dir`` (defence in depth — the
        members were already validated, so this must never fire).

        When ``expected_hashes`` is given (install passes the
        manifest's files map), every member it names is re-hashed as
        it streams out and the extraction refuses on a mismatch.
        Validation hashed the archive at an earlier moment; without
        the re-check, bytes swapped between validation and extraction
        would land on disk unverified (E6-NEW-13). ``manifest.json``
        itself is the trust anchor and is never in the map — it is
        skipped here exactly as validation skips it. Members absent
        from the map keep the byte-budget checks only.
        """
        target_root = target_dir.resolve()
        total_written = 0
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
                # N-17: stream the member out in chunks under a byte
                # budget instead of materialising it whole — the
                # extractor enforces the same caps validation judged
                # from the directory, so a lying directory entry (or
                # a caller that skipped validation) still cannot
                # expand without limit.
                member_written = 0
                hasher = hashlib.sha256()
                with z.open(info) as src_f, dest.open("wb") as out:
                    while True:
                        chunk = src_f.read(_EXTRACT_CHUNK_BYTES)
                        if not chunk:
                            break
                        member_written += len(chunk)
                        total_written += len(chunk)
                        if member_written > PLUGIN_ZIP_MAX_MEMBER_BYTES:
                            raise ValueError(
                                f"Refusing to extract {member!r}: exceeds "
                                f"the {PLUGIN_ZIP_MAX_MEMBER_BYTES}-byte "
                                "per-member limit")
                        if total_written > PLUGIN_ZIP_MAX_TOTAL_BYTES:
                            raise ValueError(
                                "Refusing to extract: archive exceeds "
                                f"the {PLUGIN_ZIP_MAX_TOTAL_BYTES}-byte "
                                "total limit")
                        hasher.update(chunk)
                        out.write(chunk)
                if (
                    expected_hashes is not None
                    and member != _MANIFEST_MEMBER
                    and member in expected_hashes
                    and not hmac.compare_digest(
                        hasher.hexdigest(), expected_hashes[member]
                    )
                ):
                    raise ValueError(
                        f"Refusing to extract {member!r}: SHA-256 of the "
                        "extracted bytes does not match the manifest "
                        "(archive changed after validation?)")

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
                # N-17: this read happens BEFORE archive validation,
                # so the manifest member itself is size-gated on its
                # declared uncompressed size — a bomb must not be
                # able to OOM the prepare step through the one member
                # every flow has to read.
                if z.getinfo(_MANIFEST_MEMBER).file_size > (
                        PLUGIN_ZIP_MAX_MEMBER_BYTES):
                    raise ValueError(
                        "Invalid plugin package: manifest.json declares "
                        f"over {PLUGIN_ZIP_MAX_MEMBER_BYTES} uncompressed "
                        "bytes")
                manifest_data = json.loads(z.read(_MANIFEST_MEMBER).decode())
        elif src_path.is_dir():
            m_path = src_path / "manifest.json"
            if m_path.exists():
                manifest_data = json.loads(m_path.read_text(encoding="utf-8"))

        if not manifest_data:
            raise ValueError("Invalid plugin package: manifest.json not found")

        # Pydantic validators reject unsafe plugin/capability names and
        # file_paths here, before the approval card is ever shown.
        manifest = PluginManifest(**manifest_data)
        # ADR 0035: a manifest declaring tools is refused here,
        # before the approval card — tools arrive only via a bundled
        # MCP server. install() re-checks authoritatively.
        self._refuse_declared_tools(manifest, source)
        if src_path.is_file() and src_path.suffix == ".zip":
            # Fail fast: refuse a bad zip before the user is asked to
            # approve it. install() re-validates (authoritative, before
            # extraction) in case the prepare data was tampered with.
            self._validate_zip_archive(src_path, manifest)
        elif src_path.is_dir():
            # N-29: the same fail-fast for directory sources, before
            # the approval step; install() re-validates authoritatively.
            self._validate_dir_source(src_path, manifest)
        # Issue 008: fail fast, before the approval step — a manifest
        # whose MCP servers do not validate is rejected here with the
        # named error, not after the user approves the install.
        mcp_configs = resolve_mcp_server_configs(src_path, manifest)
        # E7-F08: fail fast on capability conflicts too — a claim that
        # would overwrite another owner's live content is refused here,
        # before approval, and again authoritatively in install().
        self._check_capability_conflicts(src_path, manifest, mcp_configs)
        return {
            "source": source,
            "manifest": manifest.model_dump(),
            # Wave 5 (D4-4): the approval card's per-server summary
            # (what each bundled server runs / connects to / is
            # given, names only — see mcp_server_summary). Additive:
            # install() re-resolves the configs itself and ignores
            # this key.
            "mcp_server_summaries": [
                mcp_server_summary(name, config)
                for name, config in mcp_configs
            ],
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
        elif source.is_dir():
            # N-29: directory sources get their gate at the same
            # authoritative point — symlinks refused, and the files
            # map enforced when the manifest declares one.
            self._validate_dir_source(source, manifest)
        # Issue 008: validate FIRST, before any mutation — before the
        # backup move, before any file is copied, before any server is
        # added. A bad manifest fails here with the named error and the
        # previous install (if any) is left untouched.
        try:
            # ADR 0035, authoritatively: prepare data may be stale or
            # tampered with, so the declared-tools refusal is repeated
            # here, inside the same validation gate as the MCP configs.
            self._refuse_declared_tools(manifest, source)
            mcp_configs = resolve_mcp_server_configs(source, manifest)
            # E7-F08: the authoritative conflict check — prepare data
            # may be stale or tampered with, and ownership may have
            # changed since prepare ran.
            claim_plan = self._check_capability_conflicts(
                source, manifest, mcp_configs)
        except Exception as e:  # noqa: BLE001 — install boundary: any validation failure becomes the rollback RuntimeError
            raise RuntimeError(f"Installation failed, rolled back. Error: {e}")

        target_dir = self.plugins_dir / plugin_name
        skills_dest = Path.home() / ".aether" / "skills"

        from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY

        # E0-01: an install/upgrade mutates four surfaces — the package
        # dir, the global skill dirs (overwritten in place), the global
        # MCP registry (same-named configs overwritten and persisted at
        # once), and this installer's registry entry (replaced in
        # memory, then saved). The old code backed up only the package
        # dir, so a failure after any other surface had moved destroyed
        # the previous working installation while the error still
        # claimed "rolled back". Snapshot ALL four surfaces before
        # mutating any of them; the except block below restores every
        # one, and reports "rolled back" only when the restore actually
        # completed.
        old_entry = copy.deepcopy(self._registry.get(plugin_name))
        snapshot_dir = self.plugins_dir / f"{plugin_name}_snapshot"
        skill_snapshot: dict[Path, Path | None] = {}
        mcp_snapshot: dict[str, dict | None] = {}

        backup = None
        installed_skills: list[Path] = []
        installed_tools: list[str] = []
        installed_mcp: list[str] = []

        try:
            # --- Snapshot phase (no live state is mutated here) ---
            if snapshot_dir.exists():
                shutil.rmtree(snapshot_dir)
            touched_skill_dirs = {
                skills_dest / _skill_dir_name(skill.name)
                for skill in manifest.skills if skill.file_path
            }
            if old_entry:
                touched_skill_dirs |= {
                    Path(p) for p in old_entry.get("installed_skills", [])
                }
            for dest_dir in touched_skill_dirs:
                if dest_dir.exists():
                    snap_copy = snapshot_dir / "skills" / dest_dir.name
                    snap_copy.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(dest_dir, snap_copy)
                    skill_snapshot[dest_dir] = snap_copy
                else:
                    skill_snapshot[dest_dir] = None
            mcp_names = {name for name, _ in mcp_configs}
            if old_entry:
                mcp_names |= set(old_entry.get("installed_mcp", []))
            for name in mcp_names:
                existing = GLOBAL_MCP_REGISTRY.get_server(name)
                mcp_snapshot[name] = (
                    copy.deepcopy(existing) if existing is not None else None
                )

            # --- Package backup (pre-existing mechanism) ---
            if target_dir.exists():
                backup = self.plugins_dir / f"{plugin_name}_backup"
                if backup.exists():
                    shutil.rmtree(backup)
                shutil.move(str(target_dir), str(backup))

            # --- Mutation phase ---
            target_dir.mkdir(parents=True, exist_ok=True)
            if source.is_file() and source.suffix == ".zip":
                self._safe_extract(source, target_dir, manifest.files)
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
            skills_dest.mkdir(parents=True, exist_ok=True)
            for skill in manifest.skills:
                if skill.file_path:
                    src_skill = self._contained_path(
                        target_dir, skill.file_path, "skill file_path")
                    if src_skill.exists():
                        # E7-F08: a qualified skill name is logical —
                        # its directory is the flattened single segment.
                        dest_skill_dir = skills_dest / _skill_dir_name(skill.name)
                        dest_skill_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_skill, dest_skill_dir / "SKILL.md")
                        installed_skills.append(dest_skill_dir)

            # 2. Record Tools — always empty here: a manifest
            # declaring tools was refused in the validation gate
            # above (tools arrive only via a bundled MCP server).
            # The record field stays so the registry-entry shape is
            # unchanged for entries written before the refusal.
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

            # 4. Remove the previous version's artefacts that the new
            # manifest no longer declares, so a successful upgrade
            # leaves nothing stale behind. Both sets are covered by the
            # snapshots above, so a failure later still restores them.
            # E7-F08: stale removal is ownership-checked like every
            # other teardown — an artefact another enabled plugin has
            # since claimed is left in place, never deleted from under
            # its new owner.
            new_skill_dir_names = {
                _skill_dir_name(skill.name) for skill in manifest.skills}
            if old_entry:
                for old_path in old_entry.get("installed_skills", []):
                    stale_dir = Path(old_path)
                    if stale_dir.name in new_skill_dir_names:
                        continue
                    if not stale_dir.exists():
                        continue
                    stale_name = next(
                        (n for n in (old_entry.get("skill_claims") or {})
                         if _skill_dir_name(n) == stale_dir.name), None)
                    stale_body = stale_dir / "SKILL.md"
                    stale_fp = (
                        _fingerprint_bytes(stale_body.read_bytes())
                        if stale_body.exists() else None)
                    if stale_name is not None:
                        stale_allowed = self._teardown_allowed(
                            plugin_name, old_entry, "skill", stale_name,
                            stale_fp)
                    else:
                        # Legacy entries (no claims) keep bare-name
                        # removal; a claims-bearing entry whose dir
                        # maps to no claim is left alone.
                        stale_allowed = not old_entry.get("skill_claims")
                    if stale_allowed:
                        shutil.rmtree(stale_dir)
                    else:
                        logger.warning(
                            "Upgrade of %s: leaving stale skill dir %s "
                            "in place — its content is no longer solely "
                            "owned by this plugin (E7-F08)",
                            plugin_name, stale_dir)
            new_mcp_names = {name for name, _ in mcp_configs}
            if old_entry:
                for old_name in old_entry.get("installed_mcp", []):
                    if old_name not in new_mcp_names:
                        current = GLOBAL_MCP_REGISTRY.get_server(old_name)
                        current_fp = (
                            _fingerprint_config(current)
                            if current is not None else None)
                        if not self._teardown_allowed(
                                plugin_name, old_entry, "mcp", old_name,
                                current_fp):
                            logger.warning(
                                "Upgrade of %s: leaving stale MCP server "
                                "%s registered — it is no longer solely "
                                "owned by this plugin (E7-F08)",
                                plugin_name, old_name)
                            continue
                        try:
                            GLOBAL_MCP_REGISTRY.remove_server_sync(old_name)
                        except Exception as rm_err:  # noqa: BLE001 — a connected stale server must not fail an otherwise-good upgrade; it stays registered and the skip is logged
                            logger.warning(
                                "Upgrade of %s: could not remove stale "
                                "MCP server %s: %s",
                                plugin_name, old_name, rm_err,
                            )

            # 5. Commit point: replace the registry entry and persist
            # it (atomically — see _save_registry). Everything before
            # this is restorable from the snapshots; once the save
            # lands, the new installation is the recorded truth.
            # E7-F08: record the ownership claims for exactly the
            # artefacts this install landed.
            installed_dir_names = {p.name for p in installed_skills}
            self._registry[plugin_name] = {
                "manifest": manifest.model_dump(),
                # Wave 5 (FF4-04): where this install came from, so
                # the version state can later be re-checked against
                # the source via a fresh prepare. Entries written
                # before this field existed simply have no recorded
                # source and report exactly that at check time.
                "source": str(source),
                "enabled": True,
                "installed_skills": [str(p) for p in installed_skills],
                "installed_tools": installed_tools,
                "installed_mcp": installed_mcp,
                "skill_claims": {
                    name: claim for name, claim
                    in claim_plan["skills"].items()
                    if _skill_dir_name(name) in installed_dir_names
                },
                "mcp_claims": {
                    name: claim for name, claim
                    in claim_plan["mcp"].items()
                    if name in installed_mcp
                },
            }
            self._save_registry()

        except Exception as e:  # noqa: BLE001 — rollback handler: must catch everything to restore the previous install
            # Full rollback (E0-01): restore every snapshotted surface.
            # Each step is attempted independently and failures are
            # collected — the result reported to the caller must match
            # reality, so "rolled back" is claimed only when every
            # restore step actually completed.
            rollback_errors: list[str] = []

            # Package directory.
            try:
                if target_dir.exists():
                    shutil.rmtree(target_dir)
                if backup and backup.exists():
                    shutil.move(str(backup), str(target_dir))
            except Exception as rb_err:  # noqa: BLE001 — collect and continue restoring the other surfaces
                rollback_errors.append(f"package directory: {rb_err}")

            # Global skill dirs: drop whatever the failed attempt left
            # (including partially copied new skills) and put back the
            # snapshotted previous content.
            for dest_dir, snap_copy in skill_snapshot.items():
                try:
                    if dest_dir.exists():
                        shutil.rmtree(dest_dir)
                    if snap_copy is not None:
                        shutil.copytree(snap_copy, dest_dir)
                except Exception as rb_err:  # noqa: BLE001 — collect and continue
                    rollback_errors.append(f"skill {dest_dir.name}: {rb_err}")

            # Global MCP registry: re-add the snapshotted configs
            # (which also re-persists them) and remove servers the
            # failed attempt introduced. Issue 011 (S6): a connected
            # server refuses a sync remove — that refusal is collected
            # and reported, never silently swallowed.
            for name, snap_config in mcp_snapshot.items():
                try:
                    if snap_config is not None:
                        GLOBAL_MCP_REGISTRY.add_server(
                            name, copy.deepcopy(snap_config))
                    elif name in GLOBAL_MCP_REGISTRY.list_servers():
                        GLOBAL_MCP_REGISTRY.remove_server_sync(name)
                except Exception as rb_err:  # noqa: BLE001 — collect and continue
                    rollback_errors.append(f"MCP server {name}: {rb_err}")

            # This installer's registry entry: restore the previous
            # entry (or drop the new one, for a fresh install) and
            # persist it. If the persist fails but the file on disk
            # already matches the restored registry, state is
            # consistent — that is exactly the case when the original
            # failure WAS this save, which is atomic and never
            # half-lands — so it is not counted as a rollback failure.
            if old_entry is None:
                self._registry.pop(plugin_name, None)
            else:
                self._registry[plugin_name] = copy.deepcopy(old_entry)
            try:
                self._save_registry()
            except Exception as rb_err:  # noqa: BLE001 — judged against the on-disk state below
                try:
                    on_disk = json.loads(self.registry_file.read_text())
                except Exception:  # noqa: BLE001 — an unreadable file counts as not matching
                    on_disk = None
                if on_disk != self._registry:
                    rollback_errors.append(f"plugin registry: {rb_err}")

            if snapshot_dir.exists():
                shutil.rmtree(snapshot_dir, ignore_errors=True)

            self._audit("PLUGIN_INSTALL_FAILED", plugin_name, str(e),
                        str(source))
            if rollback_errors:
                raise RuntimeError(
                    "Installation failed and rollback was incomplete: "
                    + "; ".join(rollback_errors)
                    + f". Error: {e}")
            raise RuntimeError(f"Installation failed, rolled back. Error: {e}")

        # E7-F02: the install is committed — invalidate the live skill
        # index so the new skills are advertised / resolvable /
        # loadable without a restart, and bodies overwritten by an
        # upgrade are not served stale from the content cache. This
        # mirrors toggle()'s refresh and sits AFTER the commit point
        # on purpose: a failed install (rolled back above) must leave
        # the live index exactly as it was.
        from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER

        GLOBAL_SKILL_MANAGER.refresh()

        # Committed. Backup/snapshot cleanup is best-effort only: a
        # cleanup failure must never roll back a landed install (the
        # pre-fix code deleted the backup inside the try, so a cleanup
        # error there rolled back a fully committed upgrade).
        for debris in (backup, snapshot_dir):
            if debris is not None and debris.exists():
                try:
                    shutil.rmtree(debris)
                except OSError as cleanup_err:
                    logger.warning(
                        "Install of %s committed; could not remove %s: %s",
                        plugin_name, debris, cleanup_err)

    def uninstall(self, plugin_name: str):
        # E7-F05: an unknown (or missing) name must fail loudly,
        # exactly as toggle() does below. This used to return
        # silently, so the websocket branch reported success for a
        # no-op uninstall — the plugin stayed installed while the
        # Settings UI believed it was gone (the panel's buttons sent
        # an undefined name, which JSON serialisation dropped).
        if plugin_name not in self._registry:
            raise KeyError(f"Plugin not found: {plugin_name}")

        data = self._registry[plugin_name]

        # Remove MCP — E7-F08: ownership-checked. A server another
        # enabled plugin has since claimed (or whose live config no
        # longer matches what this plugin installed) is left
        # registered; uninstalling must not delete the new owner's
        # server from under it.
        from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY, MCPConfigError
        mcp_to_remove: list[str] = []
        for mcp_name in data.get("installed_mcp", []):
            current = GLOBAL_MCP_REGISTRY.get_server(mcp_name)
            if current is None:
                continue
            current_fp = _fingerprint_config(current)
            if not self._teardown_allowed(
                    plugin_name, data, "mcp", mcp_name, current_fp):
                logger.warning(
                    "Uninstall of %s: leaving MCP server %s registered "
                    "— it is no longer solely owned by this plugin "
                    "(E7-F08)", plugin_name, mcp_name)
                continue
            mcp_to_remove.append(mcp_name)
        # E6-NEW-02: the same connected-session pre-check toggle()
        # runs (E7-F01). remove_server_sync refuses a connected
        # server while an event loop is running in this thread; if
        # that refusal surfaced mid-loop here, the servers already
        # removed stayed removed while the skills, files and registry
        # entry survived — a half-uninstalled plugin. The websocket
        # uninstall handler disconnects first; a caller that did not
        # now gets the refusal BEFORE anything mutates, so a failed
        # uninstall leaves the plugin whole. The check covers only
        # the servers this uninstall will actually remove (E7-F08).
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loop_running = False
        else:
            loop_running = True
        if loop_running:
            for mcp_name in mcp_to_remove:
                status = GLOBAL_MCP_REGISTRY.get_server_status(mcp_name)
                if status.get("status") == "connected":
                    raise MCPConfigError(
                        f"Cannot uninstall {plugin_name}: MCP server "
                        f"'{mcp_name}' is still connected; disconnect "
                        "it first (await remove_server) — refusing "
                        "to uninstall over a live session."
                    )
        for mcp_name in mcp_to_remove:
            GLOBAL_MCP_REGISTRY.remove_server_sync(mcp_name)

        # Remove skills — same ownership rule for the skill dirs.
        for skill_path_str in data.get("installed_skills", []):
            skill_dir = Path(skill_path_str)
            if not skill_dir.exists():
                continue
            skill_name = next(
                (n for n in (data.get("skill_claims") or {})
                 if _skill_dir_name(n) == skill_dir.name), None)
            if skill_name is not None:
                body_path = skill_dir / "SKILL.md"
                current_fp = (
                    _fingerprint_bytes(body_path.read_bytes())
                    if body_path.exists() else None)
                if not self._teardown_allowed(
                        plugin_name, data, "skill", skill_name, current_fp):
                    logger.warning(
                        "Uninstall of %s: leaving skill dir %s in "
                        "place — its content is no longer solely owned "
                        "by this plugin (E7-F08)", plugin_name, skill_dir)
                    continue
            shutil.rmtree(skill_dir)

        # E7-F08: skills this plugin parked in the holding area when
        # it was disabled are exclusively its own (the holding path is
        # namespaced by plugin name) — an uninstall must not orphan
        # them there forever.
        holding = (Path.home() / ".aether" / "skills"
                   / ".disabled-by-plugin" / plugin_name)
        if holding.exists():
            shutil.rmtree(holding)

        # Remove plugin files
        target_dir = self.plugins_dir / plugin_name
        if target_dir.exists():
            shutil.rmtree(target_dir)

        del self._registry[plugin_name]
        self._save_registry()

        # E7-F02: invalidate the live skill index now the uninstall
        # has landed. refresh() rebuilds from disk and clears the
        # content cache, so the removed skills stop being advertised
        # and resolved — and a body cached before the uninstall is
        # never served for a skill whose directory is gone. Mirrors
        # toggle()'s refresh; a failed uninstall raises before this
        # point and leaves the live index untouched.
        from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER

        GLOBAL_SKILL_MANAGER.refresh()

    def toggle(self, plugin_name: str, enabled: bool):
        """Enable/disable with real teardown and rebuild (Epic 7, E7-05).

        Previously this flipped a flag and nothing stopped working —
        the plugin's skills stayed indexed and its MCP servers stayed
        registered. Now: disabling removes the plugin's MCP servers
        from the global registry and moves its skill dirs into
        ``~/.aether/skills/.disabled-by-plugin/<plugin>/`` (out of
        the scanned tree); enabling reverses both, re-validating the
        stored manifest's MCP configs before re-adding them. The
        skill manager's index is refreshed after any move.
        """
        if plugin_name not in self._registry:
            raise KeyError(f"Plugin not found: {plugin_name}")
        data = self._registry[plugin_name]
        if data.get("enabled", True) == enabled:
            return

        from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY, MCPConfigError
        from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER

        skills_root = Path.home() / ".aether" / "skills"
        holding = skills_root / ".disabled-by-plugin" / plugin_name

        if not enabled:
            # E7-F08: teardown is ownership-checked first — a server or
            # skill another enabled plugin has claimed, or whose live
            # content no longer matches what this plugin installed, is
            # NOT this plugin's to remove and stays in place.
            mcp_to_remove: list[str] = []
            for mcp_name in data.get("installed_mcp", []):
                current = GLOBAL_MCP_REGISTRY.get_server(mcp_name)
                if current is None:
                    continue
                if self._teardown_allowed(
                        plugin_name, data, "mcp", mcp_name,
                        _fingerprint_config(current)):
                    mcp_to_remove.append(mcp_name)
                else:
                    logger.warning(
                        "Disable of %s: leaving MCP server %s "
                        "registered — it is no longer solely owned by "
                        "this plugin (E7-F08)", plugin_name, mcp_name)
            # E7-F01: a live MCP session can only be closed by its
            # owner task, so remove_server_sync REFUSES a connected
            # server while an event loop is running in this thread.
            # The websocket toggle handler disconnects first (the
            # uninstall handler's pattern); a caller that did not must
            # get that refusal as a failure BEFORE anything mutates —
            # never a swallowed warning that lets the disable persist
            # ``enabled=False`` over a still-callable session. Raising
            # here keeps a failed disable atomic: flag unsaved, skills
            # unmoved, sessions untouched. The check covers only the
            # servers this disable will actually remove (E7-F08).
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                loop_running = False
            else:
                loop_running = True
            if loop_running:
                for mcp_name in mcp_to_remove:
                    status = GLOBAL_MCP_REGISTRY.get_server_status(mcp_name)
                    if status.get("status") == "connected":
                        raise MCPConfigError(
                            f"Cannot disable {plugin_name}: MCP server "
                            f"'{mcp_name}' is still connected; disconnect "
                            "it first (await remove_server) — refusing "
                            "to disable over a live session."
                        )
            for mcp_name in mcp_to_remove:
                GLOBAL_MCP_REGISTRY.remove_server_sync(mcp_name)
            for skill_path_str in data.get("installed_skills", []):
                src = Path(skill_path_str)
                if not src.exists():
                    continue
                skill_name = next(
                    (n for n in (data.get("skill_claims") or {})
                     if _skill_dir_name(n) == src.name), None)
                if skill_name is not None:
                    body_path = src / "SKILL.md"
                    current_fp = (
                        _fingerprint_bytes(body_path.read_bytes())
                        if body_path.exists() else None)
                    if not self._teardown_allowed(
                            plugin_name, data, "skill", skill_name,
                            current_fp):
                        logger.warning(
                            "Disable of %s: leaving skill dir %s in "
                            "place — its content is no longer solely "
                            "owned by this plugin (E7-F08)",
                            plugin_name, src)
                        continue
                holding.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(holding / src.name))
        else:
            # E7-F03: validate BEFORE restoring anything. The stored
            # manifest is re-resolved against the installed plugin
            # dir, exactly as at install time — and it must run
            # before the first held skill moves: pre-fix the moves
            # (and the holding dir's deletion) happened first, so a
            # validation failure left the plugin persisted disabled
            # with its skills restored into the scan root, fully
            # reachable by any skill manager. A failure here now
            # leaves the holding area intact, the scan root
            # unchanged and nothing re-registered.
            manifest = PluginManifest(**data["manifest"])
            plugin_dir = self.plugins_dir / plugin_name
            mcp_configs = resolve_mcp_server_configs(plugin_dir, manifest)
            # E7-F08: conflict validation joins the validate-first
            # phase. While this plugin was disabled, another plugin
            # may legitimately have taken over its names; re-enabling
            # must refuse rather than overwrite the new owner. An
            # identical live body/config is a shared claim and
            # collapses (nothing to restore over it).
            for mcp_name, config_dict in mcp_configs:
                existing = GLOBAL_MCP_REGISTRY.get_server(mcp_name)
                if existing is not None and (
                    _fingerprint_config(existing)
                    != _fingerprint_config(config_dict)
                ):
                    raise MCPConfigError(
                        f"Cannot enable {plugin_name}: MCP server "
                        f"'{mcp_name}' is now registered with a "
                        "different configuration by another owner; "
                        "refusing to overwrite it (capability conflict)."
                    )
            held_dirs = (
                list(holding.iterdir()) if holding.exists() else [])
            collapse_dirs: set[str] = set()
            for skill_dir in held_dirs:
                dest = skills_root / skill_dir.name
                if not dest.exists():
                    continue
                claim_name = next(
                    (n for n in (data.get("skill_claims") or {})
                     if _skill_dir_name(n) == skill_dir.name), None)
                if claim_name is None:
                    continue  # legacy entry: restore as before
                dest_body = dest / "SKILL.md"
                dest_fp = (
                    _fingerprint_bytes(dest_body.read_bytes())
                    if dest_body.exists() else None)
                claim = self._entry_claim(data, "skill", claim_name)
                if claim is not None and dest_fp != claim["sha256"]:
                    raise MCPConfigError(
                        f"Cannot enable {plugin_name}: skill "
                        f"'{claim_name}' is now provided with "
                        "different content by another owner; refusing "
                        "to overwrite it (capability conflict)."
                    )
                collapse_dirs.add(skill_dir.name)
            if holding.exists():
                for skill_dir in held_dirs:
                    dest = skills_root / skill_dir.name
                    if skill_dir.name in collapse_dirs and dest.exists():
                        # Identical body already live under the name:
                        # the held copy is a duplicate — drop it.
                        shutil.rmtree(skill_dir)
                    else:
                        shutil.move(str(skill_dir), str(dest))
                holding.rmdir()
            # E7-F08: a claimed skill whose live dir is gone entirely
            # (it was shared at disable time, then removed by the last
            # other claimant) is recreated from the installed package,
            # exactly as install placed it.
            for skill in manifest.skills:
                if not skill.file_path:
                    continue
                if not self._entry_claims_name(data, "skill", skill.name):
                    continue
                dest = skills_root / _skill_dir_name(skill.name)
                if dest.exists():
                    continue
                src_skill = self._contained_path(
                    plugin_dir, skill.file_path, "skill file_path")
                if src_skill.exists():
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_skill, dest / "SKILL.md")
            for mcp_name, config_dict in mcp_configs:
                GLOBAL_MCP_REGISTRY.add_server(mcp_name, config_dict)

        GLOBAL_SKILL_MANAGER.refresh()
        data["enabled"] = enabled
        self._save_registry()

    def get_all(self) -> dict[str, Any]:
        # Hand out a snapshot, not the live registry (E6-NEW-14): a
        # caller mutating the returned dict would otherwise change
        # installer state without going through _save_registry, and
        # the change would silently reach disk on the next save.
        # Callers only read (iteration, .get, spreads).
        return copy.deepcopy(self._registry)

    # -- Wave 5: per-plugin load honesty (D4-4 remainder) --------------
    # The registry-level load error (above) covers a corrupt
    # registry FILE. A single ENTRY can also fail to load — its
    # stored manifest no longer validates (hand-edited registry, a
    # record written by a build with different manifest rules) —
    # and until now that plugin rendered in Settings as if it were
    # fine, with a fallback name and version. This is the plugin
    # equivalent of MCP's invalid servers and skills' load_error
    # rows (contract C7): the failure is first-class data, carried
    # on the list reply as {state: "load_error", error}.

    @staticmethod
    def load_error_for(record: Any) -> str | None:
        """Why this registry record cannot load, or None if it can."""
        if not isinstance(record, dict):
            return "The saved record for this plugin is not readable."
        manifest_data = record.get("manifest")
        if not isinstance(manifest_data, dict) or not manifest_data:
            return (
                "The saved manifest for this plugin is missing, so "
                "the plugin cannot be loaded."
            )
        try:
            PluginManifest(**manifest_data)
        except Exception as e:  # noqa: BLE001 — the validation failure IS the load error being reported
            return (
                "The saved manifest for this plugin could not be "
                f"read: {e}"
            )
        return None

    def load_errors(self) -> dict[str, str]:
        """{plugin name: load error} for every record that fails."""
        errors: dict[str, str] = {}
        for name, record in self._registry.items():
            error = self.load_error_for(record)
            if error is not None:
                errors[name] = error
        return errors

    # -- Wave 5: update check (FF4-04 plugins half, PC-13) -------------

    def check_for_update(self, plugin_name: str) -> dict[str, Any]:
        """Re-check one installed plugin against its install source.

        Runs a fresh ``prepare_install`` on the source recorded at
        install time and compares what it declares now against the
        installed record: versions, the component name sets, and —
        via the E7-F08 claim fingerprints recorded at install — the
        content of claimed skills and the configs of claimed MCP
        servers. The check is a VIEW: nothing is installed, moved
        or re-approved by it. ``permissions_grew`` follows PC-13:
        the update needs fresh approval when it adds components or
        changes what a bundled MCP server runs/connects to (its
        declared reach), which is exactly what the result names.
        """
        result: dict[str, Any] = {
            "name": plugin_name,
            "checked": False,
            "error": None,
            "installed_version": None,
            "latest_version": None,
            "has_update": False,
            "permissions_grew": False,
            "added_skills": [],
            "removed_skills": [],
            "changed_skills": [],
            "added_mcp_servers": [],
            "removed_mcp_servers": [],
            "changed_mcp_servers": [],
        }
        entry = self._registry.get(plugin_name)
        if entry is None:
            result["error"] = f"Plugin not found: {plugin_name}"
            return result
        installed_manifest = entry.get("manifest") or {}
        result["installed_version"] = installed_manifest.get("version")
        source = entry.get("source")
        if not source:
            result["error"] = (
                "No install source was recorded for this plugin, "
                "so its version cannot be re-checked. Reinstall it "
                "from its source to record one."
            )
            return result
        try:
            prepared = self.prepare_install(source)
        except Exception as e:  # noqa: BLE001 — a source that no longer prepares (moved, changed, now refused) is the check's honest answer, not a crash
            result["error"] = f"The install source could not be re-checked: {e}"
            return result
        result["checked"] = True
        new_manifest = prepared["manifest"]
        result["latest_version"] = new_manifest.get("version")

        old_skills = {s.get("name") for s in installed_manifest.get("skills") or []}
        new_skills = {s.get("name") for s in new_manifest.get("skills") or []}
        old_mcp = {s.get("name") for s in installed_manifest.get("mcp_servers") or []}
        new_mcp = {s.get("name") for s in new_manifest.get("mcp_servers") or []}
        result["added_skills"] = sorted(new_skills - old_skills)
        result["removed_skills"] = sorted(old_skills - new_skills)
        result["added_mcp_servers"] = sorted(new_mcp - old_mcp)
        result["removed_mcp_servers"] = sorted(old_mcp - new_mcp)

        # Content drift on components present in both, judged
        # against the fingerprints recorded at install. Legacy
        # entries (no claims) simply have nothing to compare —
        # their changed lists stay empty rather than guessed.
        src_path = Path(source)
        new_skill_defs = {
            s.get("name"): s for s in new_manifest.get("skills") or []}
        for name in sorted(old_skills & new_skills):
            claim = self._entry_claim(entry, "skill", name)
            if claim is None:
                continue
            file_path = (new_skill_defs.get(name) or {}).get("file_path")
            if not file_path:
                continue
            raw = self._read_package_bytes(src_path, file_path)
            if raw is not None and _fingerprint_bytes(raw) != claim["sha256"]:
                result["changed_skills"].append(name)
        try:
            new_configs = dict(resolve_mcp_server_configs(
                src_path, PluginManifest(**new_manifest)))
        except Exception:  # noqa: BLE001 — prepare already resolved these; a re-resolve failure leaves config drift unjudged, never fabricated
            new_configs = {}
        for name in sorted(old_mcp & new_mcp):
            claim = self._entry_claim(entry, "mcp", name)
            if claim is None or name not in new_configs:
                continue
            if _fingerprint_config(new_configs[name]) != claim["sha256"]:
                result["changed_mcp_servers"].append(name)

        result["permissions_grew"] = bool(
            result["added_skills"]
            or result["added_mcp_servers"]
            or result["changed_mcp_servers"]
        )
        result["has_update"] = bool(
            result["latest_version"] != result["installed_version"]
            or result["added_skills"] or result["removed_skills"]
            or result["changed_skills"]
            or result["added_mcp_servers"] or result["removed_mcp_servers"]
            or result["changed_mcp_servers"]
        )
        return result
