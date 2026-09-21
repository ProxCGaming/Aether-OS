"""Inspector script to view every tool call, thoughts, and reasoning across tasks.

Usage:
  python inspect_tools.py          # View all tool calls and memory calls (latest 20)
  python inspect_tools.py --all    # View full history of tool calls
  python inspect_tools.py --tail   # Live monitor of new tool calls (tail -f style)
"""
import sys
import json
import time
from pathlib import Path

AUDIT_LOG_PATH = Path("logs/audit.log")

def format_event(record: dict) -> str:
    ev_type = record.get("type", "")
    action = record.get("action", "")
    ts = record.get("ts", 0)
    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    details = record.get("details", {})
    
    if action == "SUPERVISOR_MEMORY_TOOL_CALLED":
        tool_name = details.get("tool_name", "unknown")
        args = details.get("args", {})
        result_prev = details.get("result_preview", "")
        return (
            f"\033[96m[{time_str}]\033[0m \033[93m[MEMORY TOOL]\033[0m \033[1m{tool_name}\033[0m\n"
            f"  Arguments : {json.dumps(args)}\n"
            f"  Result    : {result_prev}\n"
        )
        
    elif action == "WORKSPACE_TOOL_CALLED":
        tool_name = details.get("tool_name", "unknown")
        args = details.get("args", {})
        result = details.get("result", "")
        return (
            f"\033[96m[{time_str}]\033[0m \033[92m[WORKSPACE TOOL]\033[0m \033[1m{tool_name}\033[0m\n"
            f"  Arguments : {json.dumps(args)}\n"
            f"  Result    : {result}\n"
        )

    elif action in ("KNOWLEDGE_FACT_STORED", "EPISODIC_MEMORY_STORED"):
        return (
            f"\033[96m[{time_str}]\033[0m \033[95m[{action}]\033[0m\n"
            f"  Details   : {json.dumps(details)}\n"
        )

    elif ev_type == "TASK_TRANSITION":
        task_id = record.get("task_id", "")[:8]
        to_st = record.get("to_state", "")
        payload = record.get("payload", {})
        prompt = payload.get("prompt", "")
        resp = payload.get("response", "")
        if prompt:
            return f"\033[96m[{time_str}]\033[0m \033[94m[TASK START]\033[0m Task {task_id}... Prompt: \033[1m'{prompt}'\033[0m\n"
        elif resp:
            return f"\033[96m[{time_str}]\033[0m \033[92m[TASK DONE]\033[0m Task {task_id}... Response: \033[1m'{resp}'\033[0m\n"

    return ""


def main():
    if not AUDIT_LOG_PATH.exists():
        print(f"No audit log found at {AUDIT_LOG_PATH.resolve()}")
        return

    show_all = "--all" in sys.argv
    tail_mode = "--tail" in sys.argv

    print(f"\n=======================================================")
    print(f"      AETHER TOOL & MEMORY CALL AUDIT INSPECTOR        ")
    print(f"  Source: {AUDIT_LOG_PATH.resolve()}")
    print(f"=======================================================\n")

    with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()

    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            formatted = format_event(data)
            if formatted:
                events.append(formatted)
        except Exception:
            continue

    if not show_all and len(events) > 20:
        events = events[-20:]

    for ev in events:
        print(ev)

    if not events:
        print("No tool or memory calls logged yet.\n")

    if tail_mode:
        print("Waiting for new events (Ctrl+C to stop)...")
        with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    try:
                        data = json.loads(line.strip())
                        out = format_event(data)
                        if out:
                            print(out)
                    except Exception:
                        pass
                else:
                    time.sleep(0.5)

if __name__ == "__main__":
    main()
