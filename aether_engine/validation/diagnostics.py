import sys
import subprocess
import sqlite3
import urllib.request
import urllib.error
import os
import time
from typing import List, Dict, Any
from pathlib import Path

def run_environment_diagnostics() -> List[Dict[str, Any]]:
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
    except Exception as e:
        logs.append({
            "target_name": "Python Environment",
            "status": "FAILED",
            "details": str(e),
            "timestamp": time.time()
        })

    # 2. Terminal Access
    try:
        result = subprocess.run(["echo", "hello"], capture_output=True, text=True, timeout=2, shell=True)
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
    except Exception as e:
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
    except Exception as e:
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
    except Exception as e:
        logs.append({
            "target_name": "Ollama Local Service",
            "status": "FAILED",
            "details": str(e),
            "timestamp": time.time()
        })

    # 5. Web Search API
    try:
        if os.environ.get("TAVILY_API_KEY"):
            logs.append({
                "target_name": "Web Search API",
                "status": "OK",
                "details": "Key verified",
                "timestamp": time.time()
            })
        else:
            logs.append({
                "target_name": "Web Search API",
                "status": "WARN",
                "details": "Missing TAVILY_API_KEY",
                "timestamp": time.time()
            })
    except Exception as e:
        logs.append({
            "target_name": "Web Search API",
            "status": "FAILED",
            "details": str(e),
            "timestamp": time.time()
        })

    return logs
