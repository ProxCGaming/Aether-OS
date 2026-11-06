"""Wave 2 platform correctness for the shell safety chain (E3-N03/N06).

The safety layer runs on the user's Windows machine, so its verdicts
cannot be POSIX-only: the Windows spellings of the already-ruled
operations must inherit the POSIX analogues' verdicts. Pre-flight, in
turn, has to parse the shell grammar it wraps — command lists, pipes
and environment assignments — instead of treating glued operators or
assignments as part of an executable name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aether_engine.tools.approval_cache import ApprovalCache
from aether_engine.tools.policy import resolve_tool_policy
from aether_engine.tools.registry import Tool, ToolRisk
from aether_engine.validation.pre_flight import validate_tool_request
from aether_engine.validation.shell_rules import evaluate_shell_command

SHELL = Tool(name="execute_shell", description="", risk=ToolRisk.EXECUTE)
WRITE_FILE = Tool(name="write_file", description="", risk=ToolRisk.WRITE)


@pytest.mark.parametrize("command", [
    "Remove-Item -Recurse -Force C:\\",
    r"Remove-Item -Force -Recurse C:/",
    r"Remove-Item -Recurse -Force $env:USERPROFILE",
    r"Remove-Item -Recurse -Force %USERPROFILE%",
    r"Remove-Item -Recurse -Force C:\Windows",
    r"Remove-Item -Recurse -Force $env:SystemRoot",
    "rd /s /q C:\\",
    r"rmdir /s /q %USERPROFILE%",
    r"rd /s /q C:\Windows\System32",
    "del /s /f /q C:\\",
    r"del /f /q C:\Windows\System32\config\SAM",
    "format C:",
    "format /q /y D:",
    r'powershell -Command "Remove-Item -Recurse -Force C:\"',
    "cmd /c rd /s /q C:\\",
    r'cmd.exe /c "del /s /f /q C:\"',
])
def test_windows_catastrophic_spellings_deny(command):
    verdict = evaluate_shell_command(command)
    assert verdict.verdict == "deny", command


@pytest.mark.parametrize("command", [
    r"Remove-Item -Recurse -Force .\build",
    r"Remove-Item -Force -Recurse C:\work\build",
    r"rd /s /q build",
    r"rmdir /q /s C:\work\build",
    r"del /s /f /q build\*",
])
def test_windows_recursive_force_spellings_ask(command):
    verdict = evaluate_shell_command(command)
    assert verdict.verdict == "ask", command


@pytest.mark.parametrize("command", [
    r"del /f /q file.txt",
    r"Remove-Item -Force file.txt",
    r"Remove-Item -Recurse .\build",
    r"rd /q build",
    "Get-ChildItem C:\\",
    r'echo "Remove-Item -Recurse -Force C:\ is dangerous"',
    "rm -r build/",
    "rm -f file.txt",
])
def test_windows_lookalikes_and_posix_controls_stay_allow(command):
    assert evaluate_shell_command(command).verdict == "allow", command


def test_windows_shell_verdicts_reach_policy_and_preflight():
    command = r'powershell -Command "Remove-Item -Recurse -Force C:\"'
    assert resolve_tool_policy(
        SHELL, {"execute_shell": "Always Allow"}, args={"command": command}
    ) == "deny"
    result = validate_tool_request(
        "execute_shell", {"command": "cmd /c rd /s /q C:\\"}, workspace_roots=[]
    )
    assert not result.is_valid and "safety rule" in (result.error or "")


@pytest.mark.parametrize("path", [
    r"C:\Windows\System32\evil.dll",
    r"C:\Program Files\Vendor\evil.dll",
    r"%SystemRoot%\System32\evil.dll",
    r"$env:SystemRoot\System32\evil.dll",
])
def test_windows_system_paths_are_floored_in_policy(path):
    # The Always-Allow rule's prefix is the candidate's own resolved
    # parent, so the path rule genuinely fires on any host — the only
    # thing that can keep the verdict at "ask" is the system floor.
    from aether_engine.tools.paths import candidate_paths

    prefix = str(candidate_paths({"path": path})[0].parent)
    verdict = resolve_tool_policy(
        WRITE_FILE,
        {},
        args={"path": path},
        path_rules=[{"prefix": prefix, "verdict": "Always Allow"}],
    )
    assert verdict == "ask", path


@pytest.mark.parametrize("path", [
    r"C:\Windows\System32\evil.dll",
    r"C:\Program Files (x86)\Vendor\evil.dll",
    r"D:\ProgramData\evil.dll",
    "C:\\",
    r"%WinDir%\evil.dll",
    "/etc/hostname",
])
def test_system_floor_classifier(path):
    from aether_engine.tools.policy import is_system_floor_path

    assert is_system_floor_path(Path(path)) is True, path


def test_system_floor_classifier_leaves_user_paths_alone():
    from aether_engine.tools.policy import is_system_floor_path

    assert is_system_floor_path(Path(r"C:\work\build\out.dll")) is False
    assert is_system_floor_path(Path(r"C:\Users\arindam\notes.txt")) is False


def test_windows_system_paths_are_ungrantable(tmp_path):
    cache = ApprovalCache(tmp_path / "grants.db")
    cache.grant(str(Path.cwd()), "execute")
    assert cache.covering_grant(
        risk="write", args={"path": r"C:\Windows\System32\evil.dll"}
    ) is None


@pytest.mark.parametrize("command", [
    "ls; pwd",
    "ls|head",
    "FOO=bar ls",
    "FOO='two words' BAR=2 ls -la",
    '"ls" -la',
    "echo hello; ls",
    "ls && pwd",
])
def test_preflight_accepts_shell_grammar(command):
    result = validate_tool_request(
        "execute_shell", {"command": command}, workspace_roots=[]
    )
    assert result.is_valid, (command, result.error)


@pytest.mark.parametrize("command", [
    "totally_missing_tool_xyz --help",
    "ls; totally_missing_tool_xyz",
    "ls|totally_missing_tool_xyz",
    "echo hello; totally_missing_tool_xyz",
    "FOO=bar totally_missing_tool_xyz",
])
def test_preflight_still_rejects_missing_executables(command):
    result = validate_tool_request(
        "execute_shell", {"command": command}, workspace_roots=[]
    )
    assert not result.is_valid
    assert "totally_missing_tool_xyz" in (result.error or "")
    assert "not found in PATH" in (result.error or "")
