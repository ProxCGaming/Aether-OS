import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def run_environment_diagnostics() -> list[dict[str, Any]]:
    logs = []
    
    # 1. Python Environment
    try:
        version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        logs.append({
            "target_name": "Python Environment",
            "status": "OK",
            "details": f"v{version}",
            "timestamp": time.time()
        })
    except Exception as e:  # noqa: BLE001 — a diagnostics probe records any failure as its result, never raises
        logs.append({
            "target_name": "Python Environment",
            "status": "FAILED",
            "details": str(e),
            "timestamp": time.time()
        })

    # 2. Terminal Access
    try:
        result = subprocess.run(["echo", "hello"], capture_output=True, text=True, timeout=2, shell=True, check=False)
        if result.returncode == 0:
            logs.append({
                "target_name": "Terminal Access",
                "status": "OK",
                "details": "Permissions granted",
                "timestamp": time.time()
            })
        else:
            logs.append({
                "target_name": "Terminal Access",
                "status": "FAILED",
                "details": "Process exited with error",
                "timestamp": time.time()
            })
    except Exception as e:  # noqa: BLE001 — a diagnostics probe records any failure as its result, never raises
        logs.append({
            "target_name": "Terminal Access",
            "status": "FAILED",
            "details": str(e),
            "timestamp": time.time()
        })

    # 3. SQLite Storage
    try:
        db_path = Path.home() / ".aether" / "diag_test.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER)")
            conn.execute("INSERT INTO test (id) VALUES (1)")
        if db_path.exists():
            try:
                db_path.unlink()
            except OSError:
                pass
        logs.append({
            "target_name": "SQLite Storage",
            "status": "OK",
            "details": "Read/Write verified",
            "timestamp": time.time()
        })
    except Exception as e:  # noqa: BLE001 — a diagnostics probe records any failure as its result, never raises
        logs.append({
            "target_name": "SQLite Storage",
            "status": "FAILED",
            "details": str(e),
            "timestamp": time.time()
        })

    # 4. Ollama Local Service
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2) as response:
            if response.status == 200:
                logs.append({
                    "target_name": "Ollama Local Service",
                    "status": "OK",
                    "details": "Service running",
                    "timestamp": time.time()
                })
            else:
                logs.append({
                    "target_name": "Ollama Local Service",
                    "status": "WARN",
                    "details": f"HTTP {response.status}",
                    "timestamp": time.time()
                })
    except urllib.error.URLError:
        logs.append({
            "target_name": "Ollama Local Service",
            "status": "FAILED",
            "details": "Connection refused",
            "timestamp": time.time()
        })
    except Exception as e:  # noqa: BLE001 — a diagnostics probe records any failure as its result, never raises
        logs.append({
            "target_name": "Ollama Local Service",
            "status": "FAILED",
            "details": str(e),
            "timestamp": time.time()
        })

    # 5. Web Search API
    try:
        tavily_key = os.environ.get("TAVILY_API_KEY")
        if tavily_key:
            status, details = _probe_tavily_key(tavily_key)
            logs.append({
                "target_name": "Web Search API",
                "status": status,
                "details": details,
                "timestamp": time.time()
            })
        else:
            logs.append({
                "target_name": "Web Search API",
                "status": "WARN",
                "details": "Missing TAVILY_API_KEY",
                "timestamp": time.time()
            })
    except Exception as e:  # noqa: BLE001 — a diagnostics probe records any failure as its result, never raises
        logs.append({
            "target_name": "Web Search API",
            "status": "FAILED",
            "details": str(e),
            "timestamp": time.time()
        })

    return logs


def _probe_tavily_key(api_key: str) -> tuple[str, str]:
    """E5-F18: verify a Tavily key with a real probe, not its presence.

    One cheap authenticated request (the usage endpoint, 3s bound).
    Only a definitive answer changes the label: HTTP 200 earns
    "Key verified"; 401/403 means the key was rejected; anything
    else (other statuses, DNS/timeout/connection failures) reports
    the key as present but NOT verified rather than guessing.
    """
    req = urllib.request.Request(
        "https://api.tavily.com/usage",
        method="GET",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            if response.status == 200:
                return "OK", "Key verified"
            return (
                "WARN",
                f"Key present — could not be verified (HTTP {response.status})",
            )
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "FAILED", f"Key rejected by Tavily (HTTP {e.code})"
        return "WARN", f"Key present — could not be verified (HTTP {e.code})"
    except Exception as e:  # noqa: BLE001 — an unverifiable key is a WARN result, never an exception
        return "WARN", f"Key present — could not be verified ({e})"
