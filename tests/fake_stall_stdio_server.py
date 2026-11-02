"""Fake stdio MCP server that stalls on one method (issue 011, S1).

Run as a script: ``python tests/fake_stall_stdio_server.py <method>``.
Speaks minimal newline-delimited JSON-RPC over stdin/stdout: answers
``initialize`` and every method except the stalled one (with an empty
result of the right shape), and never answers the stalled method — so
the client must time out instead of hanging forever.
"""
import json
import sys

STALL = sys.argv[1] if len(sys.argv) > 1 else "tools/list"

RESULTS = {
    "tools/list": {"tools": []},
    "resources/list": {"resources": []},
    "prompts/list": {"prompts": []},
    "tools/call": {"content": [{"type": "text", "text": "ok"}], "isError": False},
    "prompts/get": {"messages": [{"role": "user", "content": "hi"}]},
}


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
                "serverInfo": {"name": "stall", "version": "1.0"},
            }
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
            continue
        if method == STALL:
            continue  # never answer: the client must time out
        result = RESULTS.get(method, {})
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)


if __name__ == "__main__":
    main()
