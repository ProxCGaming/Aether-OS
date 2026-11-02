"""Skill registry CLI: install/search/list over skills.lock.json.

Thin launcher in the run_engine.py / run_ui.py pattern; the work
lives in aether_engine.skills.cli (also declared as the
``aether-skills`` console script in pyproject.toml).
"""
import sys

from aether_engine.skills.cli import main

if __name__ == "__main__":
    sys.exit(main())
