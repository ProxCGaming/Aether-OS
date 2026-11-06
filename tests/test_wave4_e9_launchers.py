"""Wave 4 / E9 remainder — root launchers (E9-U1, E9-L1).

E9-U1: ``run_ui.py --dry-run`` ran the npm / node_modules checks
BEFORE the dry-run branch, so on exactly the machines a dry run is
meant to diagnose (no Node, no ``npm ci`` yet) it exited with an
install error and never printed the command.

E9-L1: the launchers had no behavioural covering test — only a
forbidden-import scan. These tests exercise what the launchers
actually do: run_ui's step construction / dry-run / short-circuit,
and run_engine / run_skills delegating to their documented entry
points when run as ``__main__`` (and NOT when merely imported).
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

import run_ui

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# run_ui helpers
# ---------------------------------------------------------------------------

def _desktop_app(tmp_path, *, package_json=True, node_modules=True):
    app = tmp_path / "desktop_app"
    app.mkdir()
    if package_json:
        (app / "package.json").write_text("{}", encoding="utf-8")
    if node_modules:
        (app / "node_modules").mkdir()
    return app


def _which_factory(mapping):
    return lambda name: mapping.get(name)


# ---------------------------------------------------------------------------
# E9-U1 — --dry-run is not gated behind the failures it diagnoses
# ---------------------------------------------------------------------------

class TestU1DryRunUngated:
    def test_dry_run_without_npm_still_prints_the_command(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(run_ui, "DESKTOP_APP", _desktop_app(tmp_path, package_json=False, node_modules=False))
        monkeypatch.setattr("shutil.which", _which_factory({}))
        code = run_ui.main(["--dry-run"])
        assert code == 0
        out = capsys.readouterr().out
        assert "[dry-run]" in out
        assert "electron:dev" in out

    def test_dry_run_without_node_modules_still_prints(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(run_ui, "DESKTOP_APP", _desktop_app(tmp_path, node_modules=False))
        monkeypatch.setattr("shutil.which", _which_factory({"npm": "/fake/npm"}))
        code = run_ui.main(["--dry-run"])
        assert code == 0
        assert "[dry-run]" in capsys.readouterr().out

    def test_real_run_still_refuses_without_node_modules(self, tmp_path, monkeypatch):
        # The gate itself is not weakened: only the dry run bypasses it.
        monkeypatch.setattr(run_ui, "DESKTOP_APP", _desktop_app(tmp_path, node_modules=False))
        monkeypatch.setattr("shutil.which", _which_factory({"npm": "/fake/npm"}))
        with pytest.raises(SystemExit):
            run_ui.main([])


# ---------------------------------------------------------------------------
# E9-L1 — run_ui behavioural coverage
# ---------------------------------------------------------------------------

class TestRunUiBehaviour:
    def _setup(self, tmp_path, monkeypatch, run_rc=0):
        app = _desktop_app(tmp_path)
        monkeypatch.setattr(run_ui, "DESKTOP_APP", app)
        monkeypatch.setattr(
            "shutil.which",
            _which_factory({"npm": "/fake/npm", "npx": "/fake/npx"}))
        monkeypatch.setattr(run_ui, "_warn_if_engine_down", lambda: None)
        recorded = []

        def fake_run(cmd):
            recorded.append(list(cmd))
            return run_rc

        monkeypatch.setattr(run_ui, "_run", fake_run)
        return app, recorded

    def test_dev_run_executes_electron_dev(self, tmp_path, monkeypatch):
        _app, recorded = self._setup(tmp_path, monkeypatch)
        assert run_ui.main([]) == 0
        assert recorded == [["/fake/npm", "run", "electron:dev"]]

    def test_prod_run_builds_then_launches_bundled_electron(self, tmp_path, monkeypatch):
        app, recorded = self._setup(tmp_path, monkeypatch)
        electron = app / "node_modules" / ".bin" / "electron"
        electron.parent.mkdir(parents=True, exist_ok=True)
        electron.write_text("", encoding="utf-8")
        assert run_ui.main(["--prod"]) == 0
        assert recorded == [
            ["/fake/npm", "run", "build"],
            [str(electron), "."],
        ]

    def test_prod_run_stops_at_a_failed_build(self, tmp_path, monkeypatch):
        _app, recorded = self._setup(tmp_path, monkeypatch, run_rc=5)
        assert run_ui.main(["--prod"]) == 5
        assert recorded == [["/fake/npm", "run", "build"]]

    def test_prod_dry_run_prints_both_steps_and_runs_nothing(self, tmp_path, monkeypatch, capsys):
        app, recorded = self._setup(tmp_path, monkeypatch)
        electron = app / "node_modules" / ".bin" / "electron"
        electron.parent.mkdir(parents=True, exist_ok=True)
        electron.write_text("", encoding="utf-8")
        assert run_ui.main(["--prod", "--dry-run"]) == 0
        assert recorded == []
        out = capsys.readouterr().out
        assert out.count("[dry-run]") == 2
        assert "build" in out


# ---------------------------------------------------------------------------
# E9-L1 — run_engine / run_skills delegation
# ---------------------------------------------------------------------------

def _load_module(name, path, monkeypatch, stubs):
    for dotted, module in stubs.items():
        monkeypatch.setitem(sys.modules, dotted, module)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _package_stub():
    pkg = types.ModuleType("aether_engine")
    pkg.__path__ = []
    return pkg


class TestRunEngineLauncher:
    def _stubs(self, calls):
        main_mod = types.ModuleType("aether_engine.main")
        main_mod.start_engine = lambda *a, **k: calls.append((a, k))
        return {"aether_engine": _package_stub(), "aether_engine.main": main_mod}

    def test_import_does_not_start_the_engine(self, monkeypatch):
        calls = []
        _load_module("run_engine_probe", REPO_ROOT / "run_engine.py",
                     monkeypatch, self._stubs(calls))
        assert calls == []

    def test_main_block_starts_the_engine_with_defaults(self, monkeypatch):
        calls = []
        _load_module("__main__", REPO_ROOT / "run_engine.py",
                     monkeypatch, self._stubs(calls))
        assert calls == [((), {})]


class TestRunSkillsLauncher:
    def _stubs(self, calls):
        skills_pkg = types.ModuleType("aether_engine.skills")
        skills_pkg.__path__ = []
        cli_mod = types.ModuleType("aether_engine.skills.cli")

        def fake_main():
            calls.append("main")
            return 3

        cli_mod.main = fake_main
        return {
            "aether_engine": _package_stub(),
            "aether_engine.skills": skills_pkg,
            "aether_engine.skills.cli": cli_mod,
        }

    def test_import_does_not_run_the_cli(self, monkeypatch):
        calls = []
        _load_module("run_skills_probe", REPO_ROOT / "run_skills.py",
                     monkeypatch, self._stubs(calls))
        assert calls == []

    def test_main_block_exits_with_the_cli_return_code(self, monkeypatch):
        calls = []
        with pytest.raises(SystemExit) as excinfo:
            _load_module("__main__", REPO_ROOT / "run_skills.py",
                         monkeypatch, self._stubs(calls))
        assert calls == ["main"]
        assert excinfo.value.code == 3
