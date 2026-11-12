"""Frontend build gate in CI (owner-approved 13 Nov 2026).

Jest cannot catch a missing or uninstalled frontend package: the
suite maps ESM-only dependencies such as remark-gfm to stubs (see
desktop_app/jest.config.cjs, moduleNameMapper), so every test passed
while the app could not start on the owner's PC — MarkdownRenderer
imports remark-gfm (declared in package.json and pinned in
package-lock.json), but his node_modules predated that dependency,
and only Vite's import resolution ever reported it.

These tests pin the CI contract that closes that gap, in the same
spirit as tests/test_locked_deps_ci.py: text assertions on the
workflow file, so a workflow edit that weakens the contract fails
here by name:

  * the frontend job in ci.yml installs from the lockfile
    (`npm ci`) and then runs the production build
    (`npm run build`, i.e. `vite build`) — the one check that
    resolves every import against the installed tree;
  * the build step runs BEFORE the Jest step, so a broken install
    fails fast with the resolver's message instead of a green
    test run on an app that cannot start.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _frontend_block() -> str:
    text = CI_YML.read_text(encoding="utf-8")
    assert "\n  frontend:\n" in text, "ci.yml has no frontend job"
    return text.split("\n  frontend:\n", 1)[1].split("\n  lint:\n", 1)[0]


def test_frontend_job_installs_from_lockfile():
    assert "run: npm ci" in _frontend_block()


def test_frontend_job_runs_vite_build():
    block = _frontend_block()
    assert "run: npm run build" in block, (
        "frontend job must run the vite production build — it is the "
        "only CI check that resolves every import (Jest stubs "
        "ESM-only packages such as remark-gfm)"
    )


def test_frontend_build_runs_before_jest():
    block = _frontend_block()
    build_at = block.index("run: npm run build")
    install_at = block.index("run: npm ci")
    jest_at = block.index("run: npm run test:coverage")
    assert install_at < build_at < jest_at, (
        "expected order in the frontend job: npm ci, then "
        "npm run build, then Jest"
    )
