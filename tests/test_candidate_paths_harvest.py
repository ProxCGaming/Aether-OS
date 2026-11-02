"""S2-N1: candidate_paths must see the paths a call actually carries.

Policy floors (E2-06) and approval-grant matching (E2-09) judge a
call by candidate_paths(args). It only read args["path"], so a call
carrying its target under an MCP-style key (file_path, directory, …)
or inside an execute_shell command was judged path-less and slipped
past the path rules. These tests pin the widened harvest — and its
documented limits (non-path strings and relative shell tokens are
NOT harvested).
"""
from pathlib import Path

from aether_engine.tools.paths import candidate_paths


def _resolved(raw):
    return Path(raw).expanduser().resolve()


def test_canonical_path_key_unchanged():
    assert candidate_paths({"path": "/tmp/a.txt"}) == [_resolved("/tmp/a.txt")]
    assert candidate_paths({"path": "relative.txt"}) == [_resolved("relative.txt")]


def test_alternate_path_keys_are_harvested():
    assert candidate_paths({"file_path": "/tmp/b.txt"}) == [_resolved("/tmp/b.txt")]
    assert candidate_paths({"directory": "/tmp/dir"}) == [_resolved("/tmp/dir")]
    assert candidate_paths({"destination": "/tmp/dest/x.bin"}) == [
        _resolved("/tmp/dest/x.bin")
    ]
    assert candidate_paths({"target_path": "~/out.md"}) == [_resolved("~/out.md")]


def test_non_path_strings_under_alternate_keys_are_not_harvested():
    assert candidate_paths({"file_path": "just some words"}) == []
    assert candidate_paths({"directory": "subfolder"}) == []  # no separator: documented limit
    assert candidate_paths({"file_path": 42}) == []
    assert candidate_paths({"file_path": ""}) == []


def test_multiple_keys_dedupe_in_order():
    out = candidate_paths({"path": "/tmp/a.txt", "file_path": "/tmp/a.txt",
                           "directory": "/tmp/d"})
    assert out == [_resolved("/tmp/a.txt"), _resolved("/tmp/d")]


def test_shell_command_absolute_tokens_are_harvested():
    out = candidate_paths({"command": "cat /etc/passwd && ls ~/docs"})
    assert out == [_resolved("/etc/passwd"), _resolved("~/docs")]


def test_shell_command_flag_values_and_quotes():
    out = candidate_paths({"command": 'cp a.txt --output="/tmp/out file.txt"'})
    assert _resolved("/tmp/out") in out  # token split at the space inside quotes


def test_shell_relative_tokens_are_not_harvested():
    assert candidate_paths({"command": "cat notes.txt && rm ./local.log"}) == []


def test_empty_args():
    assert candidate_paths(None) == []
    assert candidate_paths({}) == []
    assert candidate_paths({"unrelated": "/tmp/x"}) == []
