"""Epic 9 measurement harness — baselines before any optimisation.

In-process, no network, no Ollama. Every figure is a median of
repeated runs; the run counts are printed with the table so the
numbers can be re-derived. Sections:

  cold     engine cold start (subprocess): import + initialize + RSS
  ttft     fast-chat time-to-first-event / time-to-completed through
           the real websocket handler with the Epic 1 fake provider
  session  session store at 300 sessions x 20 messages
  memory   KG search (500 facts) + episodic hybrid query (2,000 eps)
           — the same seeds as the Epic 4 scale test, for comparison
  codec    Event JSON encode/decode cost + size for a stream chunk
  plist    provider-list build on the real config shape

Usage: uv run python benchmarks/epic9_bench.py [--only cold,ttft]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _median_ms(fn, runs):
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


# ---------------------------------------------------------------------------
# cold — subprocess so import/init are measured honestly
# ---------------------------------------------------------------------------

def bench_cold_child():
    start = time.perf_counter()
    import aether_engine.app  # noqa: F401
    import_ms = (time.perf_counter() - start) * 1000

    from aether_engine.state import engine_state

    start = time.perf_counter()
    engine_state.initialize()
    init_ms = (time.perf_counter() - start) * 1000

    rss_kb = 0
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            rss_kb = int(line.split()[1])
    print(json.dumps({"import_ms": import_ms, "init_ms": init_ms, "rss_mb": rss_kb / 1024}))


def bench_cold():
    samples = []
    for _ in range(3):
        out = subprocess.run(
            [sys.executable, __file__, "--cold-child"],
            capture_output=True, text=True, cwd=REPO, timeout=300, check=False,
        )
        samples.append(json.loads(out.stdout.strip().splitlines()[-1]))
    return {
        "import (median of 3)": f"{statistics.median(s['import_ms'] for s in samples):.0f} ms",
        "engine_state.initialize (median of 3)": f"{statistics.median(s['init_ms'] for s in samples):.0f} ms",
        "RSS after init (median of 3)": f"{statistics.median(s['rss_mb'] for s in samples):.0f} MB",
    }


# ---------------------------------------------------------------------------
# ttft — the real WS handler, fake provider (Epic 1 pattern)
# ---------------------------------------------------------------------------

def bench_ttft():
    from unittest.mock import AsyncMock, patch

    from fastapi.testclient import TestClient

    import aether_engine.routes.websocket as ws_module
    from aether_common.contracts import Event, EventType
    from aether_engine.app import app
    from aether_engine.memory import session_store
    from aether_engine.providers.base import StreamChunk
    from aether_engine.state import engine_state

    class FakeProvider:
        def __init__(self):
            self.calls = []

        async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
            self.calls.append(node_name)
            if node_name == "intent_classifier":
                yield StreamChunk(text="CHAT", finish_reason="stop")
                return
            for text in ("Hi ", "there!"):
                yield StreamChunk(text=text)
            yield StreamChunk(finish_reason="stop")

    tmp = Path(tempfile.mkdtemp())
    engine_state.token_path = tmp / "engine.token"
    engine_state.audit_logger.log_path = tmp / "audit.log"
    engine_state.initialize_auth()
    session_store.DB_PATH = tmp / "memory.db"
    session_store._initialized = False

    first_ms, done_ms = [], []
    with (
        patch.object(ws_module, "SecretStore") as mock_store_cls,
        patch.object(ws_module, "_create_provider_instance", return_value=FakeProvider()),
        patch.object(ws_module, "get_pending_approvals", AsyncMock(return_value=[])),
        TestClient(app) as client,
    ):
        mock_store_cls.return_value.list_providers.return_value = ["google_gemini"]
        sid = session_store.create_session("bench")
        for _ in range(5):
            with client.websocket_connect("/ws/tasks", headers={"Authorization": f"Bearer {engine_state.auth_token}"}) as ws:
                ws.receive_text()  # HELLO
                start = time.perf_counter()
                ws.send_text(Event(
                    type=EventType.START_TASK,
                    payload={"prompt": "Hello, how are you today?", "session_id": sid},
                ).to_json())
                first = None
                while True:
                    ev = Event.from_json(ws.receive_text())
                    if first is None and ev.type != EventType.HELLO:
                        first = (time.perf_counter() - start) * 1000
                    if ev.type == EventType.TASK_COMPLETED:
                        break
                first_ms.append(first)
                done_ms.append((time.perf_counter() - start) * 1000)
    return {
        "first event (median of 5)": f"{statistics.median(first_ms):.1f} ms",
        "task completed (median of 5)": f"{statistics.median(done_ms):.1f} ms",
    }


# ---------------------------------------------------------------------------
# session — session store at scale
# ---------------------------------------------------------------------------

def bench_session():
    from aether_engine.memory import session_store

    tmp = Path(tempfile.mkdtemp())
    session_store.DB_PATH = tmp / "memory.db"
    session_store._initialized = False
    ids = []
    for i in range(300):
        sid = session_store.create_session(f"bench-{i}")
        ids.append(sid)
        for j in range(20):
            session_store.add_message(sid, "user" if j % 2 == 0 else "assistant", f"message {j} " * 8)
    return {
        "list_sessions, 300 sessions (median of 5)": f"{_median_ms(session_store.list_sessions, 5):.1f} ms",
        "get_session, 20 msgs (median of 5)": f"{_median_ms(lambda: session_store.get_session(ids[150]), 5):.2f} ms",
        "add_message (median of 5)": f"{_median_ms(lambda: session_store.add_message(ids[0], 'user', 'x'), 5):.2f} ms",
    }


# ---------------------------------------------------------------------------
# memory — Epic 4 seeds, Epic 4 sizes
# ---------------------------------------------------------------------------

def bench_memory():
    import sqlite3
    from unittest.mock import patch

    from aether_engine.memory import episodic
    from aether_engine.memory import knowledge_graph as kg

    tmp = Path(tempfile.mkdtemp())
    # episodic._get_conn binds its default path at def time, so the
    # redirect patches _get_conn itself (same pattern as the Epic 4
    # test fixture).
    orig_get_conn = episodic._get_conn
    mem_db, emb_db = tmp / "episodic.db", tmp / "embeddings.db"

    def fake_get_conn(db_path=None):
        if db_path is None or str(db_path) == str(episodic.DB_PATH):
            return orig_get_conn(mem_db)
        if str(db_path) == str(episodic.EMBEDDINGS_DB_PATH):
            return orig_get_conn(emb_db)
        return orig_get_conn(db_path)

    with (
        patch.object(kg, "DB_PATH", tmp / "kg.db"),
        patch.object(episodic, "_get_conn", fake_get_conn),
    ):
        kg.init_kg_db()
        now = int(time.time())
        conn = sqlite3.connect(tmp / "kg.db")
        conn.execute(
            "INSERT INTO entities (id, name, entity_type, first_seen, last_updated) VALUES ('e1', 'User', 'Person', ?, ?)",
            (now, now))
        conn.executemany(
            "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, confidence) VALUES (?, 'e1', ?, 'seed', ?, 0.8)",
            [(f"fact-{i}", f"User fact number {i} about topic {i % 50}", now - i) for i in range(500)],
        )
        conn.commit()
        conn.close()

        episodic.init_episodic_db()
        conn = sqlite3.connect(mem_db)
        conn.executemany(
            "INSERT INTO episodic_memory (task_id, timestamp, user_prompt, task_summary, tags, outcome) VALUES (?, ?, ?, ?, ?, ?)",
            [(f"task-{i}", now - i, f"task about subsystem {i % 80} number {i}", "summary", "task", "completed fine")
             for i in range(2000)],
        )
        conn.commit()
        conn.close()

        fact_ms = _median_ms(lambda: kg.search_facts("topic 42"), 5)

        async def _q():
            start = time.perf_counter()
            await episodic.query_episodes("subsystem 42", limit=5)
            return (time.perf_counter() - start) * 1000

        ep_samples = [asyncio.run(_q()) for _ in range(5)]
    return {
        "KG search, 500 facts (median of 5)": f"{fact_ms:.1f} ms",
        "episodic hybrid query, 2000 eps (median of 5)": f"{statistics.median(ep_samples):.1f} ms",
    }


# ---------------------------------------------------------------------------
# codec — WS event JSON cost
# ---------------------------------------------------------------------------

def bench_codec():
    from aether_common.contracts import Event, EventType

    ev = Event(
        type=EventType.TASK_PROGRESS,
        request_id="bench",
        payload={"task_id": "t1", "chunk": "The quick brown fox jumps over the lazy dog. " * 4, "state": "running"},
    )
    payload = ev.to_json()
    enc = _median_ms(lambda: ev.to_json(), 200) * 1000  # per-call ms -> µs
    dec = _median_ms(lambda: Event.from_json(payload), 200) * 1000
    return {
        "event size (typical stream chunk)": f"{len(payload)} bytes",
        "to_json per event (avg of 200)": f"{enc:.1f} µs",
        "from_json per event (avg of 200)": f"{dec:.1f} µs",
    }


# ---------------------------------------------------------------------------
# plist — provider list build
# ---------------------------------------------------------------------------

def bench_plist():
    from aether_engine.orchestration.core import _build_provider_list_async
    from aether_engine.secrets.storage import SecretStore

    store = SecretStore()

    async def _once():
        start = time.perf_counter()
        await _build_provider_list_async(store)
        return (time.perf_counter() - start) * 1000

    samples = [asyncio.run(_once()) for _ in range(5)]
    return {"provider list build (median of 5)": f"{statistics.median(samples):.1f} ms"}


SECTIONS = {
    "cold": bench_cold,
    "ttft": bench_ttft,
    "session": bench_session,
    "memory": bench_memory,
    "codec": bench_codec,
    "plist": bench_plist,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--cold-child", action="store_true")
    args = ap.parse_args()
    if args.cold_child:
        bench_cold_child()
        return
    wanted = [s for s in (args.only.split(",") if args.only else SECTIONS) if s]
    for name in wanted:
        print(f"\n## {name}")
        for k, v in SECTIONS[name]().items():
            print(f"- {k}: {v}")


if __name__ == "__main__":
    main()
