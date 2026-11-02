import json
import re
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field, field_validator

# Phase 10.2 B2: names become filesystem paths at install time
# (``plugins_dir / manifest.name`` and ``skills_dest / skill.name``), so a
# bare ``str`` is a traversal primitive: a manifest with
# ``name="../../escape"`` previously installed a plugin outside the plugins
# directory. A safe name is exactly one path segment — no separators, no
# leading dot (so ``.`` / ``..`` can never match), no empty string.
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _validate_safe_name(value: str, field: str) -> str:
    if not SAFE_NAME_RE.match(value):
        raise ValueError(
            f"Invalid {field} {value!r}: must be a single path segment "
            "(letters, digits, '.', '_' or '-', not starting with '.')"
        )
    return value


def _validate_relative_path(value: str, field: str) -> str:
    """A path that must stay inside the plugin directory when joined to it.

    Rejects absolute paths (POSIX and Windows drive/UNC), backslashes
    (a separator on Windows), NUL bytes, and any ``..`` component.
    """
    if not value or "\x00" in value or "\\" in value:
        raise ValueError(f"Invalid {field} {value!r}: must be a non-empty relative path")
    p = PurePosixPath(value)
    if p.is_absolute() or re.match(r"^[A-Za-z]:", value):
        raise ValueError(f"Invalid {field} {value!r}: absolute paths are not allowed")
    if ".." in p.parts:
        raise ValueError(f"Invalid {field} {value!r}: '..' traversal is not allowed")
    return value


class CapabilityDef(BaseModel):
    name: str
    description: str | None = None
    file_path: str | None = None

    @field_validator("name")
    @classmethod
    def _name_is_safe(cls, v: str) -> str:
        return _validate_safe_name(v, "capability name")

    @field_validator("file_path")
    @classmethod
    def _file_path_is_safe(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_relative_path(v, "file_path")


class PluginManifest(BaseModel):
    name: str
    version: str
    description: str | None = None
    skills: list[CapabilityDef] = Field(default_factory=list)
    tools: list[CapabilityDef] = Field(default_factory=list)
    mcp_servers: list[CapabilityDef] = Field(default_factory=list)
    # Phase 10.2 B2: integrity manifest for zip installs. Maps each member
    # path in the plugin zip (relative, as stored in the archive, excluding
    # manifest.json itself) to the SHA-256 hex digest of that member's bytes.
    # This is an INTEGRITY check, not authentication: it proves the zip
    # contents match the manifest the publisher packaged, i.e. it detects
    # post-packaging tampering or a corrupted download. It does NOT prove
    # who published the plugin — anyone can author a zip with a
    # self-consistent manifest. There is no signature, key, or PKI in this
    # project (deliberately, per the Phase 10.2 prompt); the trust gate for
    # *who* may install remains the user's explicit approval in the
    # Phase 10.1 A1 flow. A zip whose manifest carries no ``files`` map (or
    # is missing an entry for any member) is refused as unsigned.
    files: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name_is_safe(cls, v: str) -> str:
        return _validate_safe_name(v, "plugin name")

    @field_validator("files")
    @classmethod
    def _files_are_safe_hashes(cls, v: dict[str, str]) -> dict[str, str]:
        for path, digest in v.items():
            _validate_relative_path(path, "files entry")
            if not _SHA256_HEX_RE.match(digest or ""):
                raise ValueError(
                    f"Invalid files entry {path!r}: hash must be a "
                    "lowercase SHA-256 hex digest"
                )
        return v


def parse_manifest(path: Path) -> PluginManifest:
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return PluginManifest(**data)
