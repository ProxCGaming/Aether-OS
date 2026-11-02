"""Fake stdio MCP server for E3-F05 resource-cap tests.

Run as a script: ``python tests/fake_limits_stdio_server.py``. Speaks
minimal newline-delimited JSON-RPC over stdin/stdout (same pattern as
``fake_stall_stdio_server.py``). Tools:

- ``limits`` — report THIS process's own RLIMIT_CPU / RLIMIT_DATA /
  RLIMIT_AS soft+hard values as JSON. This is how the Astra review
  verified E3-F05 (the real child reported unlimited limits); after the
  fix the values must be the launcher-applied caps, because on POSIX
  the launcher exec's into this process image.
- ``add`` — a well-behaved tool (a + b).
- ``spin`` — burn CPU forever; only a CPU cap (or the call timeout)
  ends it.
- ``hog`` — allocate+touch memory in 16 MB chunks up to ``mb`` MB,
  stopping on MemoryError; returns ``allocated=<MB>``.
- ``flood`` — write ``kb`` KiB to stderr, then return; a server doing
  this must not block on a full stderr pipe or grow engine logs
  without limit.
"""
import json
import sys


def _limits() -> dict:
    try:
        import resource

        def fmt(which):
            soft, hard = resource.getrlimit(which)
            return [soft, hard]

        return {
            "cpu": fmt(resource.RLIMIT_CPU),
            "data": fmt(resource.RLIMIT_DATA),
            "as": fmt(resource.RLIMIT_AS),
        }
    except Exception as e:  # noqa: BLE001 — report, never crash the fixture
        return {"error": str(e)}


def _call_tool(name: str, args: dict) -> str:
    if name == "add":
        return str(int(args.get("a", 0)) + int(args.get("b", 0)))
    if name == "limits":
        return json.dumps(_limits())
    if name == "spin":
        while True:
            pass
    if name == "hog":
        target = int(args.get("mb", 0))
        chunks = []
        allocated = 0
        try:
            while allocated < target:
                chunk = bytearray(16 * 1024 * 1024)
                # Zeroing already commits the pages; touch explicitly
                # as well so lazy allocators cannot under-report.
                for off in range(0, len(chunk), 4096):
                    chunk[off] = 1
                chunks.append(chunk)
                allocated += 16
        except MemoryError:
            pass
        return f"allocated={allocated}"
    if name == "flood":
        kb = int(args.get("kb", 0))
        block = b"x" * 65536
        remaining = kb * 1024
        while remaining > 0:
            piece = block[: min(remaining, len(block))]
            sys.stderr.buffer.write(piece)
            sys.stderr.buffer.flush()
            remaining -= len(piece)
        return "flooded"
    return f"unknown tool {name!r}"


TOOLS = [
    {"name": "add", "description": "Add two numbers.", "inputSchema": {"type": "object"}},
    {"name": "limits", "description": "Report this process's rlimits.", "inputSchema": {"type": "object"}},
    {"name": "spin", "description": "Burn CPU forever.", "inputSchema": {"type": "object"}},
    {"name": "hog", "description": "Allocate up to mb MB.", "inputSchema": {"type": "object"}},
    {"name": "flood", "description": "Write kb KiB to stderr.", "inputSchema": {"type": "object"}},
]


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method", "")
        if method.startswith("notifications/"):
            continue
        rid = req.get("id")
        if method == "initialize":
            params = req.get("params", {}) or {}
            result = {
                "protocolVersion": params.get("protocolVersion", "2025-03-26"),
                "capabilities": {},
                "serverInfo": {"name": "limits-fixture", "version": "1.0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = req.get("params", {}) or {}
            try:
                text = _call_tool(params.get("name", ""), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            except Exception as e:  # noqa: BLE001 — fixture error surface
                result = {
                    "content": [{"type": "text", "text": f"fixture error: {e}"}],
                    "isError": True,
                }
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)


if __name__ == "__main__":
    main()
