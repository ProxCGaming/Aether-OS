"""Skill registry CLI — install / search / list (Epic 7, E7-F09).

The command surface the owner-approved addendum (omp #28) promises:

    aether-skills install [<name>] [--source <locator>]
    aether-skills search <query> --source <locator>
    aether-skills list

over ``skills.lock.json`` — pinned and reproducible (lockfile.py,
sources.py, skill_registry.py do the work; this module is argument
parsing, scope resolution and reporting only).

Scopes (the lockfile is "project or user", as in the reference
design): by default the lockfile is the user one,
``~/.aether/skills.lock.json``, and skills install into the global
skills root ``~/.aether/skills`` that SkillManager always scans.
``--project <dir>`` switches to ``<dir>/skills.lock.json`` and the
workspace skills root ``<dir>/.agents/skills``. ``--lockfile`` and
``--skills-dir`` override either path individually.

``install`` with no name replays every pin in the lockfile (the
reproducible path); with a name it resolves that skill from
``--source``, pins it, and installs it. Exit code is 0 only when
every requested install actually holds on disk; refusals, drift and
unreproducible pins are reported on stderr and exit 1. Nothing is
ever silently overwritten and nothing is fabricated: an unreadable
source or lockfile is a loud error, not an empty result.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aether_engine.skills import skill_registry
from aether_engine.skills.lockfile import LOCKFILE_NAME, SkillLockError
from aether_engine.skills.sources import SkillSourceError, open_source


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    if args.project:
        project = Path(args.project)
        lockfile = project / LOCKFILE_NAME
        skills_dir = project / ".agents" / "skills"
    else:
        data_home = Path.home() / ".aether"
        lockfile = data_home / LOCKFILE_NAME
        skills_dir = data_home / "skills"
    if args.lockfile:
        lockfile = Path(args.lockfile)
    if args.skills_dir:
        skills_dir = Path(args.skills_dir)
    return lockfile, skills_dir


def _report_outcome(outcome: skill_registry.InstallOutcome) -> bool:
    if outcome.ok:
        verb = "already installed" if outcome.action == "already-installed" \
            else "installed"
        print(f"{verb} {outcome.name} ({outcome.detail})")
        return True
    print(f"error: {outcome.name}: {outcome.detail}", file=sys.stderr)
    return False


def _cmd_install(args: argparse.Namespace) -> int:
    lockfile, skills_dir = _resolve_paths(args)
    if args.name:
        if not args.source:
            print(
                "error: installing a skill by name needs --source "
                "<locator> (there is no default registry to resolve "
                "names from)", file=sys.stderr)
            return 1
        source = open_source(
            args.source_type, args.source, base_dir=lockfile.parent)
        outcome = skill_registry.install_named(
            args.name, source,
            source_type=args.source_type, source_locator=args.source,
            lockfile_path=lockfile, skills_root=skills_dir,
            ref=args.ref, skill_path=args.skill_path)
        return 0 if _report_outcome(outcome) else 1
    outcomes = skill_registry.install_locked(lockfile, skills_dir)
    if not outcomes:
        print(f"no pinned skills in {lockfile}")
        return 0
    # Eager on purpose: every pin's outcome is reported, even after
    # an earlier pin failed (all() over a generator would stop early).
    results = [_report_outcome(o) for o in outcomes]
    return 0 if all(results) else 1


def _cmd_search(args: argparse.Namespace) -> int:
    if not args.source:
        print(
            "error: search needs --source <locator> (there is no "
            "default registry to search)", file=sys.stderr)
        return 1
    lockfile, _ = _resolve_paths(args)
    source = open_source(args.source_type, args.source, base_dir=lockfile.parent)
    for entry in skill_registry.search_source(source, args.query):
        print(f"{entry.name}: {entry.description} (version {entry.version})")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    lockfile, skills_dir = _resolve_paths(args)
    rows = skill_registry.list_pins(lockfile, skills_dir)
    if not rows:
        print(f"no pinned skills in {lockfile}")
        return 0
    for pin, status in rows:
        print(
            f"{pin.name}: {status} "
            f"(source: {pin.source_type}:{pin.source}, "
            f"ref: {pin.ref or '-'}, hash: {pin.computed_hash[:12]}…)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aether-skills",
        description="Skill registry: install/search/list over skills.lock.json")
    parser.add_argument(
        "--lockfile", default=None,
        help=f"path to {LOCKFILE_NAME} (default: per scope)")
    parser.add_argument(
        "--skills-dir", default=None,
        help="skills root to install into (default: per scope)")
    parser.add_argument(
        "--project", default=None,
        help="project directory: use <dir>/skills.lock.json and "
             "<dir>/.agents/skills instead of the user scope")
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser(
        "install", help="install a named skill, or replay all pins")
    p_install.add_argument("name", nargs="?", default=None)
    p_install.add_argument("--source", default=None)
    p_install.add_argument(
        "--source-type", default="local", choices=["local", "github"])
    p_install.add_argument("--ref", default=None)
    p_install.add_argument("--skill-path", default=None)
    p_install.set_defaults(func=_cmd_install)

    p_search = sub.add_parser("search", help="search a source's catalogue")
    p_search.add_argument("query")
    p_search.add_argument("--source", default=None)
    p_search.add_argument(
        "--source-type", default="local", choices=["local", "github"])
    p_search.set_defaults(func=_cmd_search)

    p_list = sub.add_parser("list", help="list pinned skills and their state")
    p_list.set_defaults(func=_cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (SkillLockError, SkillSourceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
