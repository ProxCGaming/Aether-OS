"""Filesystem write/execute tools (Epic 2): write_file, edit_file,
create_directory, delete_file, execute_shell.

The mutation half of the filesystem family — ``tools/filesystem.py``
is the read/search half (read_file, find_files, search_text, all
READ_ONLY and containment-checked). One of the built-in tool
factories split out of ``tools/registry.py`` (Wave 5 registry split):
the registry keeps the tool types, the assembly point and the
execution pipeline; each family module owns its factories. The
registry re-exports :func:`create_file_tools` lazily, so existing
imports from it keep working.
"""

from aether_engine.tools.registry import Tool, ToolExecutionError, ToolRisk


def create_file_tools() -> list[Tool]:
    def _write_file(path: str, content: str = "") -> str:
        from pathlib import Path
        # Epic 2, E2-04 (decision D3): missing parents inside the
        # workspace are created implicitly — one approval covers the
        # write; containment is pre-flight's job, as for every tool.
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote file {path}"

    def _create_directory(path: str) -> str:
        from pathlib import Path
        Path(path).mkdir(parents=True, exist_ok=True)
        return f"Created directory {path}"

    def _edit_file(path: str, expected_hash: str | None = None, edits: list | None = None) -> str:
        # Epic 2, E2-03: the graph path executes this in the worker; this
        # in-process implementation serves direct registry callers and
        # shares the exact aether_common contract with the worker, so
        # behaviour cannot drift between the two.
        from pathlib import Path

        from aether_common.text_edit import (
            TextEditError,
            apply_text_edits,
            atomic_write_bytes,
            content_hash,
        )

        target = Path(path)
        if not target.is_file():
            raise ToolExecutionError(f"edit_file failed: '{path}' does not exist or is not a file.")
        data = target.read_bytes()
        actual = content_hash(data)
        # E2-F01: a SUPPLIED hash is checked exactly — including an
        # empty string, which can never match and so refuses like any
        # stale hash. Only an absent/None hash means "no hash", and
        # only that skips the check (the normal no-hash flow).
        if expected_hash is not None and actual != expected_hash:
            raise ToolExecutionError(
                f"edit_file failed: '{path}' changed since it was read "
                f"(expected sha256 {expected_hash[:12]}…, found {actual[:12]}…). Re-read the file."
            )
        try:
            new_text = apply_text_edits(data.decode("utf-8"), edits or [])
        except (TextEditError, UnicodeDecodeError) as exc:
            raise ToolExecutionError(f"edit_file failed for '{path}': {exc}") from exc
        atomic_write_bytes(path, new_text.encode("utf-8"))
        return f"Edited file {path} ({len(edits or [])} edit(s) applied)"

    def _delete_file(path: str) -> str:
        from pathlib import Path
        Path(path).unlink(missing_ok=True)
        return f"Deleted file {path}"

    def _execute_shell(command: str) -> str:
        import subprocess
        completed = subprocess.run(command, shell=True, capture_output=True, text=True, check=False)
        return f"exit={completed.returncode}\nstdout={completed.stdout}\nstderr={completed.stderr}"

    return [
        Tool(
            name="write_file",
            description="Create or overwrite a file on disk with supplied content.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path"],
            },
            execute_fn=_write_file,
            risk=ToolRisk.WRITE,
        ),
        Tool(
            name="edit_file",
            description=(
                "Edit an existing file with anchored replacements. Each edit's old_text must "
                "occur exactly once in the file (include surrounding context to make it unique); "
                "the edit fails loudly if an anchor is missing or ambiguous, and nothing is "
                "written unless every edit applies. Pass expected_hash from read_file's header: "
                "if the file changed since you read it, the edit is refused. Prefer this over "
                "write_file for changes to existing files."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "expected_hash": {
                        "type": "string",
                        "description": "The sha256 from read_file's header for this file.",
                    },
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_text": {"type": "string"},
                                "new_text": {"type": "string"},
                            },
                            "required": ["old_text", "new_text"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
            execute_fn=_edit_file,
            risk=ToolRisk.WRITE,
        ),
        Tool(
            name="create_directory",
            description="Create a directory (and any missing parents) inside the workspace.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            execute_fn=_create_directory,
            risk=ToolRisk.WRITE,
        ),
        Tool(
            name="delete_file",
            description="Delete a file from disk if it exists.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            execute_fn=_delete_file,
            risk=ToolRisk.DESTRUCTIVE,
        ),
        Tool(
            name="execute_shell",
            description="Run a local shell command.",
            input_schema={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            execute_fn=_execute_shell,
            risk=ToolRisk.EXECUTE,
        ),
    ]
