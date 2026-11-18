"""Full Furnish SPEC-6D — memory & ledger perf baseline.

Seeds a SCRATCH profile (temp dir; never the owner's real data)
at the two spec scales — 1,000 and 5,000 facts, 500 episodes —
and times the reads the Batch 5 surfaces will perform, plus the
checkpoint-thread enumeration the task ledger depends on:

  profile     facade.user_profile()          (Batch 5 board 1)
  facts list  facade.list_facts(limit=100)   (boards 1/2 lists)
  entity      facade.get_entity_facts(name)  (board 4 inputs)
  episodes    facade.list_episodes(limit=50) (board 2 counts/list)
  ledger      checkpointer list() scan vs thread count (FF6-05)

Every figure is a median of 5 runs; payload is the JSON size of
the returned value. Later waves inherit these numbers as a soft
gate: no regression >20% on any row (spec §3, Wave 1).

Usage: .venv/bin/python benchmarks/memory_baseline_bench.py \
           [--out baseline.json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

RUNS = 5
ENTITIES = 40


def _median_ms(fn, runs=RUNS):
    samples = []
    result = None
    for _ in range(runs):
        start = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples), result


def _payload_bytes(value) -> int:
    return len(json.dumps(value, default=str).encode("utf-8"))


def _seed_scale(fact_count: int, episode_count: int) -> dict:
    """One scratch scale: seed, measure the four memory reads."""
    from aether_engine.memory import episodic
    from aether_engine.memory import knowledge_graph as kg
    from aether_engine.memory.facade import NativeMemoryBackend

    tmp = Path(tempfile.mkdtemp(prefix="aether-bench-"))
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
        conn.executemany(
            "INSERT INTO entities (id, name, entity_type, first_seen, last_updated)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (f"e{i}", f"Entity {i}", "Person" if i % 3 else "Topic", now, now)
                for i in range(ENTITIES)
            ],
        )
        conn.executemany(
            "INSERT INTO facts (id, entity_id, fact_text, source_task_id,"
            " timestamp, confidence) VALUES (?, ?, ?, 'seed', ?, 0.8)",
            [
                (
                    f"fact-{i}",
                    f"e{i % ENTITIES}",
                    f"Entity {i % ENTITIES} fact number {i} about topic {i % 50}"
                    f" with Entity {(i + 7) % ENTITIES} mentioned",
                    now - i,
                )
                for i in range(fact_count)
            ],
        )
        conn.commit()
        conn.close()

        episodic.init_episodic_db()
        conn = sqlite3.connect(mem_db)
        conn.executemany(
            "INSERT INTO episodic_memory (task_id, timestamp, user_prompt,"
            " task_summary, tags, outcome) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (f"task-{i}", now - i, f"task about subsystem {i % 80} number {i}",
                 "summary", "task", "completed fine")
                for i in range(episode_count)
            ],
        )
        conn.commit()
        conn.close()

        backend = NativeMemoryBackend()
        rows = {}
        ms, val = _median_ms(lambda: backend.user_profile())
        rows["profile"] = {"ms": round(ms, 2), "payload_bytes": _payload_bytes(val)}
        ms, val = _median_ms(lambda: backend.list_facts(limit=100))
        rows["facts_list_100"] = {"ms": round(ms, 2), "payload_bytes": _payload_bytes(val)}
        ms, val = _median_ms(lambda: backend.get_entity_facts("Entity 3"))
        rows["entity_facts"] = {"ms": round(ms, 2), "payload_bytes": _payload_bytes(val)}
        ms, val = _median_ms(lambda: backend.list_episodes(limit=50))
        rows["episodes_list_50"] = {"ms": round(ms, 2), "payload_bytes": _payload_bytes(val)}
        backend.close()
    return rows


def _seed_ledger(thread_counts=(50, 250)) -> dict:
    """Checkpoint-thread enumeration vs thread count (FF6-05).

    Uses langgraph's own SqliteSaver in a scratch DB — the same
    store the engine's checkpointer wraps. If the saver is not
    importable in this environment the section reports itself
    unavailable instead of failing the whole baseline.
    """
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except Exception as exc:  # pragma: no cover - environment dependent
        return {"unavailable": f"SqliteSaver import failed: {exc}"}

    rows = {}
    for count in thread_counts:
        tmp = Path(tempfile.mkdtemp(prefix="aether-ledger-"))
        with SqliteSaver.from_conn_string(str(tmp / "checkpoints.db")) as saver:
            for i in range(count):
                config = {"configurable": {"thread_id": f"t-{i}", "checkpoint_ns": ""}}
                checkpoint = {
                    "v": 1,
                    "id": f"cp-{i}",
                    "ts": "2026-01-01T00:00:00+00:00",
                    "channel_values": {},
                    "channel_versions": {},
                    "versions_seen": {},
                }
                saver.put(config, checkpoint, {}, {})
            ms, val = _median_ms(lambda: list(saver.list(None)))
            rows[f"threads_{count}"] = {"ms": round(ms, 2), "threads": len(val)}
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None,
                        help="also write the raw results JSON here")
    args = parser.parse_args()

    results = {
        "bench": "SPEC-6D memory baseline",
        "runs_per_row": RUNS,
        "scales": {},
        "ledger": _seed_ledger(),
    }
    for facts, episodes in ((1_000, 500), (5_000, 500)):
        results["scales"][f"facts_{facts}_episodes_{episodes}"] = _seed_scale(facts, episodes)

    print(f"SPEC-6D baseline — median of {RUNS} runs, scratch profile")
    for scale, rows in results["scales"].items():
        print(f"\n{scale}:")
        for name, row in rows.items():
            print(f"  {name:<18} {row['ms']:>9.2f} ms   payload {row['payload_bytes']:>8} B")
    print("\nledger (checkpoint-thread enumeration):")
    for name, row in results["ledger"].items():
        print(f"  {name:<18} {row}")
    if args.out:
        args.out.write_text(json.dumps(results, indent=2))
        print(f"\nraw JSON -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
