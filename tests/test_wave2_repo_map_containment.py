"""Wave 2, N-08 [S2]: repo map must not read outside the workspace.

``rag/repo_map.py`` ``_discover()`` collected symlinked files from
the walk and ``_parse_file`` read them — no resolved-path
containment check anywhere. ``os.walk`` won't descend a symlinked
*directory*, but a symlinked *file* inside the workspace was read
and its signatures rendered into the model's prompt context. The
prose indexer got exactly this fence in F-04 (resolve +
``is_relative_to`` before stat/open); the repo map never did.
"""
from __future__ import annotations

import os

from aether_engine.rag.repo_map import build_repo_map

INSIDE = "def inside_workspace_fn():\n    return 1\n"
OUTSIDE = "def outside_secret_fn():\n    return 2\n"


def test_symlinked_file_pointing_outside_is_never_mapped(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "real.py").write_text(INSIDE, encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text(OUTSIDE, encoding="utf-8")
    os.symlink(outside, workspace / "linked.py")

    text = build_repo_map([str(workspace)], prompt="inside_workspace_fn")
    assert "inside_workspace_fn" in text
    assert "outside_secret_fn" not in text
    assert "linked.py" not in text


def test_symlink_resolving_inside_still_maps(tmp_path):
    # The F-04 doctrine is containment of the RESOLVED path, not a
    # blanket symlink ban: a link whose target lives inside the
    # workspace is legitimate and maps normally.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "real.py").write_text(INSIDE, encoding="utf-8")
    os.symlink(workspace / "real.py", workspace / "alias.py")
    text = build_repo_map([str(workspace)], prompt="inside_workspace_fn")
    assert "inside_workspace_fn" in text
