"""Wave 2, N-39 + N-41: discovered-path fences in the file tools.

N-39 [S4]: ``find_files`` yielded every glob match with no
resolved-path containment re-check and no sensitive-floor check —
its sibling ``search_text`` performs both, with a comment explaining
why (E2-F03: policy judged only the call's starting path; everything
the walk DISCOVERS was never judged). Existence/names of
symlinked-out and floored files leaked to the model.

N-41 [S4]: ``workspace_search``'s citation verification built
``Path(root) / rel_path`` and read it with no resolve/containment
proof — an absolute ``rel_path`` discards the root entirely and
``../`` escapes it, so a poisoned/stale index entry could make the
tool read (and cite) files outside the workspace.
"""
from __future__ import annotations

import os

from aether_engine.tools.filesystem import _find_files_fn
from aether_engine.tools.workspace_search import _verify_citation

# ---------------------------------------------------------------------
# N-39 — find_files
# ---------------------------------------------------------------------

def test_find_files_hides_symlinked_out_file_names(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "visible.py").write_text("x = 1\n", encoding="utf-8")
    outside = tmp_path / "outside_secret.py"
    outside.write_text("x = 2\n", encoding="utf-8")
    os.symlink(outside, workspace / "escaped_link.py")

    out = _find_files_fn("**/*.py", workspace_roots=[str(workspace)])
    assert "visible.py" in out
    assert "escaped_link.py" not in out
    assert "outside_secret.py" not in out


def test_find_files_hides_sensitive_floor_names(tmp_path, monkeypatch):
    # A workspace rooted inside a floored prefix: every discovered
    # file sits on the sensitive floor (~/.ssh shape) and must be
    # excluded even though the walk itself was granted the root —
    # the same verdict search_text gives discovered descendants.
    home = tmp_path / "home"
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True)
    (ssh_dir / "id_notes.txt").write_text("not a key, still floored\n",
                                          encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    out = _find_files_fn("**/*.txt", workspace_roots=[str(ssh_dir)])
    assert "id_notes.txt" not in out


def test_find_files_normal_results_unchanged(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "a.py").write_text("", encoding="utf-8")
    (workspace / "pkg" / "b.py").write_text("", encoding="utf-8")
    out = _find_files_fn("**/*.py", workspace_roots=[str(workspace)])
    assert "a.py" in out
    assert "pkg/b.py" in out


# ---------------------------------------------------------------------
# N-41 — workspace_search citation verification
# ---------------------------------------------------------------------

def test_verify_citation_refuses_dotdot_escape(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("line one\nline two\n", encoding="utf-8")
    # Even when the outside file's span matches the indexed content
    # exactly, the citation must not be verified — the entry points
    # outside the workspace, so the hit is dropped as stale.
    verified, text = _verify_citation(
        str(root), "../outside.txt", 1, 1, "line one")
    assert verified is False
    assert text == ""


def test_verify_citation_refuses_absolute_path(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("line one\n", encoding="utf-8")
    verified, text = _verify_citation(
        str(root), str(outside), 1, 1, "line one")
    assert verified is False
    assert text == ""


def test_verify_citation_still_verifies_inside_file(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "doc.md").write_text("alpha\nbeta\n", encoding="utf-8")
    verified, text = _verify_citation(str(root), "doc.md", 2, 2, "beta")
    assert verified is True
    assert text == "beta"
