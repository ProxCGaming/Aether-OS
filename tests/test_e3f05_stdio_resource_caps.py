"""E3-F05: stdio MCP servers must run under worker-equivalent resource caps.

The Astra full-product review verified the defect by reading the real
child's limits: a harmless stdio server reported **unlimited** CPU and
address-space soft/hard limits, inherited from the engine — the product
spawn passed only command/args/env/cwd to the SDK, while Aether's own
worker-executed tools run under Job Object CPU-time/memory caps
(master prompt: "stdio servers run with the same resource discipline
as workers").

These tests reproduce that verification (the fixture's ``limits`` tool
reports the server process's own rlimits — on POSIX the capped
launcher exec's into the server, so the fixture IS the capped process)
and the enforcement consequences: a CPU hog is killed by the cap (not
by the much longer call timeout), a memory hog is bounded by the cap,
and a stderr flood is relayed only up to a byte cap without blocking
the server or the session.

POSIX rlimit assertions are POSIX-only; the Windows path (Job Object
in the launcher) cannot execute in this VM and is covered by code
parity with ``workers/job_object.py`` + owner Windows QA.
"""
import io
import json
import sys
import time
from pathlib import Path

import pytest

from aether_engine.mcp.client import MCPClientError, MCPClientManager

FIXTURE = Path(__file__).parent / "fake_limits_stdio_server.py"

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="rlimit assertions are POSIX-only"
)

MB = 1024 * 1024


def limits_config(**overrides):
    config = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(FIXTURE)],
        "timeout": 10,
    }
    config.update(overrides)
    return config


@pytest.fixture()
def manager():
    return MCPClientManager()


async def _limits(manager, name="fake"):
    out = await manager.call_tool(name, "limits", {})
    return json.loads(out)


@posix_only
async def test_stdio_child_runs_under_default_caps(manager):
    from aether_engine.mcp.client import (
        MCP_STDIO_DEFAULT_MEMORY_CAP_MB,
        MCP_STDIO_DEFAULT_TIME_CAP_SECONDS,
    )

    result = await manager.connect("fake", limits_config())
    assert result["ok"] is True, result
    try:
        # A well-behaved tool still works exactly as before.
        assert (await manager.call_tool("fake", "add", {"a": 3, "b": 5})).strip() == "8"
        limits = await _limits(manager)
        # The review's verification, inverted: pre-fix both soft limits
        # are -1 (RLIMIT_INFINITY, inherited from the parent).
        assert limits["cpu"][0] == MCP_STDIO_DEFAULT_TIME_CAP_SECONDS, limits
        assert limits["data"][0] == MCP_STDIO_DEFAULT_MEMORY_CAP_MB * MB, limits
    finally:
        await manager.disconnect("fake")


@posix_only
async def test_stdio_child_honours_per_server_cap_overrides(manager):
    result = await manager.connect(
        "fake", limits_config(time_cap_seconds=25, memory_cap_mb=256)
    )
    assert result["ok"] is True, result
    try:
        limits = await _limits(manager)
        assert limits["cpu"][0] == 25, limits
        assert limits["data"][0] == 256 * MB, limits
    finally:
        await manager.disconnect("fake")


@posix_only
async def test_cpu_hog_is_killed_by_the_cap_not_the_call_timeout(manager):
    result = await manager.connect(
        "fake", limits_config(time_cap_seconds=1, timeout=10)
    )
    assert result["ok"] is True, result
    started = time.monotonic()
    try:
        with pytest.raises(MCPClientError):
            await manager.call_tool("fake", "spin", {}, timeout=20)
        elapsed = time.monotonic() - started
        # The 1s CPU cap must end the call long before the 20s call
        # timeout could. Pre-fix nothing kills the spinner and this
        # call only fails at the full 20s timeout.
        assert elapsed < 10, f"spin ended after {elapsed:.1f}s — cap did not bite"
        assert manager.get_status("fake")["status"] == "failed"
    finally:
        await manager.disconnect("fake")


@posix_only
async def test_memory_hog_is_bounded_by_the_cap(manager):
    result = await manager.connect(
        "fake", limits_config(memory_cap_mb=128, timeout=15)
    )
    assert result["ok"] is True, result
    try:
        out = await manager.call_tool("fake", "hog", {"mb": 384}, timeout=20)
        allocated = int(out.strip().split("=", 1)[1])
        # The fixture asks for 384 MB; under a 128 MB data cap it must
        # stop far short (its own interpreter baseline counts toward
        # the cap, so it stops below 128 MB of *chunks*). Pre-fix it
        # allocates the full 384 MB.
        assert 0 < allocated < 320, out
        # The server survived its own MemoryError and still answers.
        assert (await manager.call_tool("fake", "add", {"a": 1, "b": 2})).strip() == "3"
    finally:
        await manager.disconnect("fake")


async def test_invalid_cap_values_fail_loudly(manager):
    result = await manager.connect("fake", limits_config(time_cap_seconds=-5))
    assert result["ok"] is False, result
    assert "time_cap_seconds" in (result["error"] or ""), result

    result = await manager.connect("fake", limits_config(memory_cap_mb="lots"))
    assert result["ok"] is False, result
    assert "memory_cap_mb" in (result["error"] or ""), result


async def test_stderr_flood_does_not_block_or_break_the_session(manager):
    result = await manager.connect("fake", limits_config(timeout=15))
    assert result["ok"] is True, result
    try:
        # 4 MiB of stderr — far beyond the relay cap and the OS pipe
        # buffer. If nothing drained-and-discarded, the child would
        # block mid-flood and this call would never return.
        out = await manager.call_tool("fake", "flood", {"kb": 4096}, timeout=20)
        assert out.strip() == "flooded"
        assert (await manager.call_tool("fake", "add", {"a": 2, "b": 2})).strip() == "4"
    finally:
        await manager.disconnect("fake")


@posix_only
def test_stderr_relay_bounds_forwarded_bytes():
    from aether_engine.mcp.client import (
        MCP_STDIO_STDERR_CAP_BYTES,
        _BoundedStderrRelay,
    )

    class _Target:
        def __init__(self):
            self.buffer = io.BytesIO()

    # Small feed: forwarded verbatim, nothing truncated.
    target = _Target()
    relay = _BoundedStderrRelay(target)
    relay.start()
    relay.writer.write(b"hello stderr\n")
    relay.writer.flush()
    relay.close_writer()
    assert relay.wait(timeout=5)
    assert target.buffer.getvalue() == b"hello stderr\n"
    assert relay.truncated is False
    assert relay.total_bytes == relay.forwarded_bytes == len(b"hello stderr\n")

    # Oversized feed: forwarding stops at the cap; the rest is drained
    # and counted, never forwarded — engine logs stay bounded no
    # matter how much the server writes.
    target = _Target()
    relay = _BoundedStderrRelay(target)
    relay.start()
    fed = MCP_STDIO_STDERR_CAP_BYTES + 500_000
    block = b"y" * 65536
    remaining = fed
    while remaining > 0:
        piece = block[: min(remaining, len(block))]
        relay.writer.write(piece)
        remaining -= len(piece)
    relay.close_writer()
    assert relay.wait(timeout=10)
    assert relay.total_bytes == fed
    assert relay.forwarded_bytes <= MCP_STDIO_STDERR_CAP_BYTES
    assert len(target.buffer.getvalue()) == relay.forwarded_bytes
    assert relay.truncated is True
