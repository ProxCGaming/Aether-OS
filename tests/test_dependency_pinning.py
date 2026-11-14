"""Dependency pinning — one exact source of truth per ecosystem.

Owner directive, 15 Nov 2026, after a shipped installer failed every
agent task: the payload assembler installed the pyproject version
RANGES fresh at build time, so the installer carried litellm
1.108.0 while dev/CI tested the locked 1.106.4 — and litellm's
tool-carrying completion path needs `mcp.types.CacheableResult`,
which exists only in mcp 2.x, so any litellm >= 1.106 paired with
the project's mcp 1.30.0 crashes every tool call with a misleading
"network/service error". (The verified-good pair is litellm
1.108.5 — whose 1.108.4 patch made the MCP import safe on a base
install — with mcp 1.30.0, proven by a mock tool-carrying
acompletion against the project venv.)

The rule these tests pin, per ecosystem:

  * Python packages: pyproject.toml carries EXACT `==` pins for
    every direct dependency, and uv.lock (generated from it) is
    what every install consumes — dev/CI via `uv sync --locked`,
    the shipped payload via the assembler's hashed `uv export`.
  * Node packages: both package.json manifests carry exact
    versions, identical to what their package-lock.json resolves.
  * Interpreters/tools: .python-version == the payload's CPython,
    .nvmrc == the desktop manifest's engines, the workflows' uv
    version == the setup scripts' pinned uv.
  * CI/release: every GitHub Action is pinned to a commit SHA.
  * The payload's portable-runtime downloads are sha256-verified
    against the digests transcribed from the release's SHA256SUMS.

A future edit that floats any of these fails here by name.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSEMBLER = REPO_ROOT / "scripts" / "assemble_engine_payload.py"
WORKFLOWS = (
    REPO_ROOT / ".github" / "workflows" / "ci.yml",
    REPO_ROOT / ".github" / "workflows" / "release.yml",
)

EXACT_NODE_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def _pyproject() -> dict:
    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


def _locked_versions() -> dict[str, str]:
    with open(REPO_ROOT / "uv.lock", "rb") as fh:
        lock = tomllib.load(fh)
    return {
        pkg["name"].lower().replace("_", "-"): pkg["version"]
        for pkg in lock["package"]
    }


def _all_python_specs() -> list[str]:
    project = _pyproject()["project"]
    specs = list(project["dependencies"])
    for extra in project.get("optional-dependencies", {}).values():
        specs.extend(extra)
    full = _pyproject()
    for group in full.get("dependency-groups", {}).values():
        specs.extend(group)
    return specs


def _spec_name(spec: str) -> str:
    return re.split(r"[<>=!~;\[]", spec)[0].strip().lower().replace("_", "-")


# --- Python: exact pins that agree with the lock -----------------------


def test_every_python_direct_dependency_is_exactly_pinned():
    specs = _all_python_specs()
    assert len(specs) >= 30, "expected the full direct dependency set"
    for spec in specs:
        requirement = spec.split(";")[0].strip()
        assert "==" in requirement, f"non-exact Python specifier: {spec}"
        assert not re.search(r"[<>~^*]", requirement), (
            f"range operator survived pinning: {spec}"
        )


def test_python_pins_match_uv_lock_versions():
    locked = _locked_versions()
    for spec in _all_python_specs():
        name = _spec_name(spec)
        pinned = spec.split(";")[0].split("==")[1].strip()
        assert locked.get(name) == pinned, (
            f"{name}: pyproject pins {pinned} but uv.lock holds "
            f"{locked.get(name)} — run `uv lock` and commit both"
        )


def test_litellm_mcp_pair_is_the_verified_one():
    # The pair proven good by a mock tool-carrying acompletion
    # (see this file's header). Changing either side re-opens the
    # CacheableResult crash class; the payload assembler's litellm
    # tools smoke is the runtime proof, this is the static one.
    locked = _locked_versions()
    assert locked["litellm"] == "1.108.5"
    assert locked["mcp"] == "1.30.0"


# --- The payload installs from the lock, never from ranges -------------


def test_assembler_installs_from_uv_lock_export():
    source = ASSEMBLER.read_text(encoding="utf-8")
    assert "uv export" in source or '"export"' in source
    assert "--locked" in source
    assert "--no-dev" in source
    # The old range-based path must never come back.
    assert "project_dependencies" not in source


def test_assembler_records_lock_provenance_in_the_manifest():
    source = ASSEMBLER.read_text(encoding="utf-8")
    assert "uv_lock_sha256" in source
    assert "requirements_sha256" in source


def test_assembler_verifies_payload_with_a_litellm_tools_smoke():
    source = ASSEMBLER.read_text(encoding="utf-8")
    assert "payload litellm tools ok" in source
    assert "mock_response" in source


def test_runtime_downloads_are_checksum_pinned_for_every_platform():
    source = ASSEMBLER.read_text(encoding="utf-8")
    block = source.split("RUNTIME_SHA256", 1)[1].split("}", 1)[0]
    for platform in ("win32", "linux", "darwin"):
        match = re.search(rf'"{platform}":\s*"([0-9a-f]{{64}})"', block)
        assert match, f"RUNTIME_SHA256 has no 64-hex digest for {platform}"


# --- Node: manifests exact and identical to their locks -----------------


def _check_node_manifest(manifest_rel: str, lock_rel: str) -> None:
    manifest = json.loads((REPO_ROOT / manifest_rel).read_text(encoding="utf-8"))
    lock = json.loads((REPO_ROOT / lock_rel).read_text(encoding="utf-8"))
    lock_root = lock["packages"][""]
    for field in ("dependencies", "devDependencies"):
        for name, spec in manifest.get(field, {}).items():
            assert EXACT_NODE_VERSION.match(spec), (
                f"{manifest_rel}: {name} is not exactly pinned: {spec}"
            )
            resolved = lock["packages"][f"node_modules/{name}"]["version"]
            assert spec == resolved, (
                f"{manifest_rel}: {name} pins {spec} but {lock_rel} "
                f"resolves {resolved}"
            )
            assert lock_root.get(field, {}).get(name) == spec, (
                f"{lock_rel}: root specifier for {name} drifted from "
                f"{manifest_rel} — re-run npm install --package-lock-only"
            )


def test_root_node_manifest_is_exactly_pinned():
    _check_node_manifest("package.json", "package-lock.json")


def test_desktop_node_manifest_is_exactly_pinned():
    _check_node_manifest("desktop_app/package.json", "desktop_app/package-lock.json")


# --- Interpreters and tools: one pin, read everywhere --------------------


def test_python_interpreter_pin_matches_the_payload_cpython():
    pin = (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    source = ASSEMBLER.read_text(encoding="utf-8")
    match = re.search(r'CPYTHON_VERSION\s*=\s*"([^"]+)"', source)
    assert match, "assembler no longer names its CPython version"
    assert pin == match.group(1) == "3.12.16"


def test_workflow_tool_versions_match_the_pin_files():
    python_pin = (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    node_pin = (REPO_ROOT / ".nvmrc").read_text(encoding="utf-8").strip()
    for workflow in WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        assert f'python-version: "{python_pin}"' in text, workflow.name
        assert f'node-version: "{node_pin}"' in text, workflow.name
        assert 'version: "0.13.6"' in text, f"{workflow.name}: uv not pinned"


def test_setup_scripts_pin_the_same_uv_as_the_workflows():
    for script in ("setup.sh", "setup.ps1"):
        text = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
        assert "0.13.6" in text, f"{script}: pinned uv version missing"


def test_every_github_action_is_pinned_to_a_commit_sha():
    uses_pattern = re.compile(r"uses:\s*([^\s#]+)(?:\s*#\s*(v\d+\.\d+\.\d+))?")
    for workflow in WORKFLOWS:
        for line in workflow.read_text(encoding="utf-8").splitlines():
            if "uses:" not in line:
                continue
            match = uses_pattern.search(line)
            assert match, f"{workflow.name}: unparsable uses line: {line}"
            ref = match.group(1).rsplit("@", 1)[-1]
            assert re.fullmatch(r"[0-9a-f]{40}", ref), (
                f"{workflow.name}: action not pinned to a commit SHA: {line.strip()}"
            )
            assert match.group(2), (
                f"{workflow.name}: SHA pin lacks its version comment: {line.strip()}"
            )
