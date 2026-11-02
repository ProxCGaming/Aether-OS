"""F-16 (EGM C-B11): the retired PySide6 UI must not come back.

``aether_ui/`` was retired in favour of the Electron app in ``desktop_app/``
(ADR-0019; ``run_ui.py`` has launched Electron since Phase 10.3 D3) and was
deleted outright on 2 Nov 2026 (owner directive), together with its tests
and the ``legacy-ui`` optional extra that carried its dependencies. These
tests pin what remains of the retirement so it cannot silently regress:

- PySide6 / qasync / markdown — the legacy UI's exclusive dependencies —
  never (re)appear in the hard ``[project] dependencies`` list.
- No default-path code (engine, shared contracts, worker, launchers)
  imports ``aether_ui``, ``PySide6`` or ``qasync``.
"""

import ast
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

LEGACY_DEPS = {"pyside6", "qasync", "markdown"}
DEFAULT_PATH_TREES = ["aether_engine", "aether_common", "aether_worker"]
DEFAULT_PATH_FILES = ["run_engine.py", "run_ui.py"]
FORBIDDEN_IMPORT_ROOTS = {"aether_ui", "PySide6", "qasync"}


def _dep_name(spec: str) -> str:
    name = spec.split(";")[0].strip()
    for sep in (">=", "<=", "==", "!=", "~=", ">", "<", "[", " "):
        name = name.split(sep)[0]
    return name.strip().lower().replace("_", "-")


def _project_data() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]


def test_legacy_deps_are_not_hard_dependencies():
    project = _project_data()
    hard = {_dep_name(spec) for spec in project["dependencies"]}
    leaked = LEGACY_DEPS & hard
    assert not leaked, (
        f"legacy-UI dependencies still hard runtime deps: {sorted(leaked)}"
    )


def _iter_default_path_files():
    for tree in DEFAULT_PATH_TREES:
        yield from sorted((REPO_ROOT / tree).rglob("*.py"))
    for name in DEFAULT_PATH_FILES:
        yield REPO_ROOT / name


def test_no_default_path_imports_of_the_legacy_ui():
    offenders = []
    for path in _iter_default_path_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            roots = []
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = [node.module.split(".")[0]]
            for root in roots:
                if root in FORBIDDEN_IMPORT_ROOTS:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} "
                        f"imports {root}"
                    )
    assert not offenders, (
        "default-path code imports the retired UI stack:\n"
        + "\n".join(offenders)
    )
