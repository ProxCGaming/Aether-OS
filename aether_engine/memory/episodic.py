import sqlite3
import time
import uuid
import math
import pickle
import logging
from typing import List, Dict, Any, Optional
from pathlib import Path
import litellm
from aether_engine.audit import AuditLogger

_audit_logger = AuditLogger()

# Database paths
DB_PATH = Path.home() / ".aether" / "memory.db"
EMBEDDINGS_DB_PATH = Path.home() / ".aether" / "embeddings.db"

def _get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_episodic_db() -> None:
    with _get_conn() as conn:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS episodic_memory USING fts5(
                task_id UNINDEXED,
                timestamp UNINDEXED,
                user_prompt,
                task_summary,
                tags,
                outcome,
                tokenize = 'porter unicode61'
            )
        """)
        conn.commit()

def init_embeddings_db() -> None:
    with _get_conn(EMBEDDINGS_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS episodic_embeddings (
                task_id TEXT PRIMARY KEY,
                embedding BLOB
            )
        """)
        conn.commit()

async def _embed_text(text: str) -> Optional[List[float]]:
    try:
        response = await litellm.aembedding(
            model="ollama/nomic-embed-text",
            input=[text],
            api_base="http://127.0.0.1:11434"
        )
        return response.data[0]["embedding"]
    except Exception as e:
        logging.warning(f"Embedding failed (fallback to BM25): {e}")
        return None

def _cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    dot = sum(a * b for a, b in zip(vec1, vec2))
    norm_a = math.sqrt(sum(a * a for a in vec1))
    norm_b = math.sqrt(sum(b * b for b in vec2))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

async def store_episode(task_id: str, user_prompt: str, task_summary: str, tags: List[str], outcome: str) -> None:
    init_episodic_db()
    init_embeddings_db()
    now = int(time.time())
    tags_str = ",".join(tags)
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO episodic_memory (task_id, timestamp, user_prompt, task_summary, tags, outcome)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, now, user_prompt, task_summary, tags_str, outcome)
        )
        conn.commit()
    
    # Generate and store embedding
    embedding = await _embed_text(task_summary)
    if embedding:
        with _get_conn(EMBEDDINGS_DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO episodic_embeddings (task_id, embedding) VALUES (?, ?)",
                (task_id, pickle.dumps(embedding))
            )
            conn.commit()

    _audit_logger.log_event("EPISODIC_MEMORY_STORED", {
        "task_id": task_id,
        "tags": tags,
        "embedded": embedding is not None
    })

async def query_episodes(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    init_episodic_db()
    init_embeddings_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        
        # Sanitize query by removing quotes and special FTS characters
        clean_query = query.replace('"', ' ').replace("'", " ").replace(":", " ")
        
        if not clean_query.strip():
            return []
            
        try:
            # We use OR matching for terms or wrap in quotes for phrase match.
            # Here we wrap the whole thing in quotes for exact phrase, or we can just let it match terms.
            # Let's just let it match terms by providing the clean string (which acts as AND).
            # If we want it to be more lenient, we can split and join with OR. Let's just use the clean string.
            cur.execute(
                """SELECT *, rank 
                   FROM episodic_memory 
                   WHERE episodic_memory MATCH ? 
                   ORDER BY rank LIMIT 100""",
                (clean_query,)
            )
            bm25_rows = cur.fetchall()
        except sqlite3.OperationalError:
            bm25_rows = []

        # 2. Semantic Search
        query_embedding = await _embed_text(query)
        semantic_scores = {}
        if query_embedding:
            with _get_conn(EMBEDDINGS_DB_PATH) as e_conn:
                e_cur = e_conn.cursor()
                e_cur.execute("SELECT task_id, embedding FROM episodic_embeddings")
                for row in e_cur.fetchall():
                    emb = pickle.loads(row["embedding"])
                    sim = _cosine_similarity(query_embedding, emb)
                    semantic_scores[row["task_id"]] = sim

        # 3. RRF Merge
        k = 60
        rrf_scores = {}
        
        # Rank BM25 results
        bm25_ranked = sorted(bm25_rows, key=lambda x: x["rank"])
        for idx, row in enumerate(bm25_ranked):
            t_id = row["task_id"]
            if t_id not in rrf_scores:
                rrf_scores[t_id] = {"row": dict(row), "score": 0.0}
            rrf_scores[t_id]["score"] += 1.0 / (k + idx + 1)
            
        # Rank Semantic results
        semantic_ranked = sorted(semantic_scores.items(), key=lambda x: x[1], reverse=True)
        for idx, (t_id, sim) in enumerate(semantic_ranked):
            if t_id in rrf_scores:
                rrf_scores[t_id]["score"] += 1.0 / (k + idx + 1)
            else:
                # Need to fetch the row from episodic_memory if it matched semantic but not BM25
                cur.execute("SELECT *, 0 as rank FROM episodic_memory WHERE task_id = ?", (t_id,))
                fetched = cur.fetchone()
                if fetched:
                    rrf_scores[t_id] = {"row": dict(fetched), "score": 1.0 / (k + idx + 1)}

        # Sort combined results
        final_results = sorted(rrf_scores.values(), key=lambda x: x["score"], reverse=True)
        results = [res["row"] for res in final_results[:limit]]
        
        _audit_logger.log_event("EPISODIC_MEMORY_RETRIEVED", {
            "query": query,
            "results_count": len(results),
            "hybrid": query_embedding is not None
        })
        return results

def get_all_episodes(limit: int = 50) -> List[Dict[str, Any]]:
    init_episodic_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM episodic_memory ORDER BY timestamp DESC LIMIT ?", (limit,))
        return [dict(row) for row in cur.fetchall()]
