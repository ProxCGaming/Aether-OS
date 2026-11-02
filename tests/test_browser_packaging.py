"""Browser runtime packaging (Epic 8 review finding N13): the
dependency the agent browser's launch path imports is declared in
pyproject.toml, so a standard install can actually start it.

Before this slice, Playwright was imported — lazily, at session
start (aether_engine/browser/session.py, PlaywrightDriver.launch)
— but declared nowhere. A fresh install built cleanly and every
browser test passed against fake drivers; only a real session
start discovered the gap, failing closed with "Playwright is not
installed". These tests pin the packaging contract, mirroring
tests/test_voice_packaging.py:

  * the distribution the launch path imports is a declared project
    dependency;
  * in a synced environment the import name resolves, including
    the exact submodule the launch path imports;
  * the exact import statement the launch path executes succeeds
    and yields the entry point it calls.

Absence behaviour is pinned elsewhere (test_browser_core.py: a
session start with the package hidden fails closed honestly).
This file pins the mirror image: presence.

Nothing launches a browser and nothing touches the network. The
Chromium build itself is the finding's second half — a bootstrap
decision (bundle vs first-run download) — and is deliberately NOT
pinned here: these tests cover the Python package only.
"""
import importlib.util
import re
import tomllib
from pathlib import Path

# Distribution name (PEP 503 normalised) -> the import name the code
# imports. Playwright's distribution and import names coincide.
BROWSER_DEPENDENCIES = {
    "playwright": "playwright",
}

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _declared_dependency_names() -> set[str]:
    with PYPROJECT.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    names = set()
    for entry in project.get("dependencies", []):
        match = re.match(r"\s*([A-Za-z0-9._-]+)", entry)
        if match:
            names.add(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return names


class TestPackagingDeclarations:
    def test_browser_runtime_dependencies_are_declared(self):
        declared = _declared_dependency_names()
        missing = set(BROWSER_DEPENDENCIES) - declared
        assert not missing, (
            f"browser runtime deps missing from pyproject dependencies: "
            f"{sorted(missing)}")

    def test_browser_import_names_resolve(self):
        for import_name in BROWSER_DEPENDENCIES.values():
            assert importlib.util.find_spec(import_name) is not None, (
                f"{import_name} is declared but not importable in this "
                f"environment (uv sync not run?)")
        # The launch path imports this exact submodule.
        assert importlib.util.find_spec("playwright.async_api") is not None


class TestLaunchImportSeam:
    def test_launch_path_import_resolves(self):
        """The exact import PlaywrightDriver.launch executes
        (session.py) must succeed in a synced environment and yield
        the callable the driver starts."""
        from playwright.async_api import async_playwright

        assert callable(async_playwright)
