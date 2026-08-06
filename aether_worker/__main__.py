import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from aether_common.auth import read_token


async def run_worker(task_payload: dict):
    tool_name = task_payload.get("tool_name")
    args = task_payload.get("args", {})

    if tool_name == "execute_shell":
        command = args.get("command")
        if not command:
            raise ValueError("Shell command is required")
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return {"returncode": proc.returncode, "stdout": stdout.decode(errors="ignore"), "stderr": stderr.decode(errors="ignore")}

    if tool_name == "write_file":
        path = args.get("path")
        content = args.get("content", "")
        if not path:
            raise ValueError("File path is required")
        Path(path).write_text(content, encoding="utf-8")
        return {"path": path, "written": True}

    if tool_name == "delete_file":
        path = args.get("path")
        if not path:
            raise ValueError("File path is required")
        Path(path).unlink(missing_ok=True)
        return {"path": path, "deleted": True}

    return {"status": "noop"}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    args = parser.parse_args()

    payload = json.loads(args.payload)
    result = await run_worker(payload)
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
