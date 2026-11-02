"""
E6-F12: workspace activation reorders roots — the skill index must follow.

SET_ACTIVE moves the activated root to the front of ``workspace_roots``,
persists that order, and calls ``SkillManager.update_workspace_roots``
with the reordered list. The manager compared the derived skill dirs
as a SET, so an order-only change never replaced ``workspace_dirs``
and never rebuilt the index: same-name skills kept resolving by the
stale root order for the rest of the session, while a fresh manager
under the identical persisted roots resolved them the other way.

Discovery (E7-F08) resolves differing-body same-name conflicts by
injection priority, ties by scan order — the later-scanned root wins.
Both fixture copies carry the default priority, so root order is the
ONLY decider and a reorder must flip the winner.
"""

import textwrap
from pathlib import Path

from aether_engine.skills.manager import SkillManager

SKILL_MD = """\
---
name: shared-skill
description: Shared skill from {tag}.
---

## Instructions
BODY_FROM_{tag}
"""


def _make_root(base: Path, tag: str) -> Path:
    """A workspace root carrying one shared-skill under .agents/skills."""
    root = base / f"root{tag}"
    skill_dir = root / ".agents" / "skills" / "shared-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(SKILL_MD.format(tag=tag)), encoding="utf-8")
    return root


def _skills_dir(root: Path) -> Path:
    return root / ".agents" / "skills"


def _manager(base: Path, roots: list[Path]) -> SkillManager:
    return SkillManager(global_dir=base / "global",
                        workspace_dirs=[_skills_dir(r) for r in roots])


def _winner_dir(sm: SkillManager) -> Path:
    return sm._skill_paths["shared-skill"]


def test_reorder_rebuilds_index_and_flips_winner(tmp_path):
    root_a = _make_root(tmp_path, "A")
    root_b = _make_root(tmp_path, "B")
    sm = _manager(tmp_path, [root_a, root_b])

    # Control: with roots [A, B] the tie falls to scan order — B wins.
    assert _winner_dir(sm) == _skills_dir(root_b) / "shared-skill"
    assert sm._index["shared-skill"].description == "Shared skill from B."

    # The SET_ACTIVE path: activating B persists roots [B, A] and the
    # handler hands exactly that list to the manager.
    sm.update_workspace_roots([str(root_b), str(root_a)])

    assert sm.workspace_dirs == [_skills_dir(root_b), _skills_dir(root_a)]
    assert _winner_dir(sm) == _skills_dir(root_a) / "shared-skill"
    assert sm._index["shared-skill"].description == "Shared skill from A."
    # First content load (nothing cached yet) serves the new winner.
    assert "BODY_FROM_A" in sm.load_skill_content("shared-skill")

    # The live manager now agrees with a fresh one built from the
    # same persisted [B, A] roots (the finding's live-vs-fresh split).
    fresh = _manager(tmp_path, [root_b, root_a])
    assert _winner_dir(sm) == _winner_dir(fresh)


def test_same_order_reset_does_not_rebuild(tmp_path):
    root_a = _make_root(tmp_path, "A")
    root_b = _make_root(tmp_path, "B")
    sm = _manager(tmp_path, [root_a, root_b])

    rebuilds = 0
    original = sm._rebuild_index

    def counting_rebuild():
        nonlocal rebuilds
        rebuilds += 1
        original()

    sm._rebuild_index = counting_rebuild

    # A no-op write in the current order must not rebuild.
    sm.update_workspace_roots([str(root_a), str(root_b)])
    assert rebuilds == 0

    # A pure reorder rebuilds exactly once...
    sm.update_workspace_roots([str(root_b), str(root_a)])
    assert rebuilds == 1

    # ...and re-setting that same new order is a no-op again.
    sm.update_workspace_roots([str(root_b), str(root_a)])
    assert rebuilds == 1


def test_membership_change_still_rebuilds(tmp_path):
    root_a = _make_root(tmp_path, "A")
    root_b = _make_root(tmp_path, "B")
    root_c = _make_root(tmp_path, "C")
    # Give C a skill of its own so discovery of it is observable.
    own_dir = _skills_dir(root_c) / "cee-skill"
    own_dir.mkdir(parents=True)
    (own_dir / "SKILL.md").write_text(textwrap.dedent("""\
        ---
        name: cee-skill
        description: Only in root C.
        ---

        ## Instructions
        Cee body.
    """), encoding="utf-8")

    sm = _manager(tmp_path, [root_a, root_b])
    assert "cee-skill" not in sm._index

    sm.update_workspace_roots([str(root_a), str(root_b), str(root_c)])
    assert "cee-skill" in sm._index

    sm.update_workspace_roots([str(root_a), str(root_b)])
    assert "cee-skill" not in sm._index
