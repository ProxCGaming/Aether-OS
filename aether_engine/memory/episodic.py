import asyncio
import logging
import math
import pickle
import sqlite3
import time
from pathlib import Path
from typing import Any

from aether_engine.audit import AuditLogger
from aether_engine.config import UserConfig, load_config

_audit_logger = AuditLogger()
logger = logging.getLogger(__name__)

# Embedding model + timeout (Phase 10.1 B2).
# Timeout is 30s, not the 10s DISCOVERY_TIMEOUT used for provider
# validation: a cold Ollama start (model load + first embed) routinely
# exceeds 10s, while provider pings are expected to answer promptly.
# No better value was found in code; 30s is the prompt-required value.
#
# Epic 8, E8-3.2: ONE embedder serves episodic memory AND the
# workspace prose index (rag/prose_indexer.py) — grill Q17,
# local-first via Ollama. The configured name is the bare Ollama
# model (UserConfig.embedding_model, whose default is
# DEFAULT_EMBEDDER); resolve_embedder() below is the single
# resolution path both consumers share. EMBEDDING_MODEL keeps the
# LiteLLM form of the default for the callers/tests that reference
# the module constant (embedding_stats, failure logging).
DEFAULT_EMBEDDER = "qwen3-embedding:0.6b"
EMBEDDING_MODEL = "ollama/qwen3-embedding:0.6b"
EMBEDDING_TIMEOUT_SECONDS = 30.0


def resolve_embedder(config: UserConfig | None = None) -> str:
    """The configured embedder's bare Ollama model name (E8-3.2).

    Resolution order matches _resolve_similarity_threshold: an
    explicit config wins, otherwise the persisted config is loaded.
    This is THE embedder for both episodic memory and the prose
    index — there is deliberately no second resolution path.
    """
    cfg = config if config is not None else load_config()
    return str(getattr(cfg, "embedding_model", None) or DEFAULT_EMBEDDER)


def litellm_embedding_model(config: UserConfig | None = None,
                            *, embedder: str | None = None) -> str:
    """LiteLLM model string for the embedder: a bare local name gets
    the ``ollama/`` provider prefix; an already-prefixed name (a
    future cloud embedder, opt-in only per Q17) passes through."""
    name = embedder or resolve_embedder(config)
    return name if "/" in name else f"ollama/{name}"

# Phase 10.3 A2: episodic semantic-similarity threshold (cosine).
# nomic-embed-text (the embedder at the time) returns L2-normalised
# embeddings, so cosine is the native similarity; the unified
# qwen3-embedding:0.6b (E8-3.2) is likewise used through cosine.
# Unrelated short texts score well below 0.3 while
# genuinely related task summaries score materially higher, so 0.3 is a
# conservative floor: it drops noise without cutting weak-but-real
# matches. Same 0.3 default as litellm's own semantic tool filter
# (litellm/proxy/_experimental/mcp_server/semantic_tool_filter.py).
# The authoritative value lives in UserConfig
# (episodic_similarity_threshold); this constant is only the fallback
# when no config is available.
EPISODIC_SIMILARITY_THRESHOLD = 0.3

# Phase 10.3 A2: how many unembedded episodes one query_episodes() call
# backfills (oldest first). Bounded so a query cannot stall on a long
# embed queue when Ollama is slow; leftovers retry on the next query.
DEFAULT_QUERY_BACKFILL_LIMIT = 5

# Phase 10.3 A2: FTS5 tables for episodic_memory, keyed by the DB file
# path, that the one-time dedupe migration has already run for in this
# process. A fresh tmp DB in tests gets its own key, so the migration
# still runs there on first init.
# Epic 9: litellm imported lazily via module __getattr__ (PEP 562) —
# the real import costs ~4s and is only needed when an embedding
# actually happens. First ``litellm`` reference loads and caches the
# real module in globals; tests patching ``episodic.litellm`` still
# land on the real module object; inside this module, call sites
# bind ``litellm = _load_litellm()`` locally as their first step.


def _load_litellm():
    import litellm as real

    real.suppress_debug_info = True
    real.set_verbose = False
    globals()["litellm"] = real
    return real


def __getattr__(name: str):
    if name == "litellm":
        return _load_litellm()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

_DEDUPED_DB_PATHS = set()

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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS episodic_thinking (
                task_id TEXT PRIMARY KEY,
                thinking_log TEXT
            )
        """)
        conn.commit()
    # Phase 10.3 A2: collapse any duplicate FTS rows left by the old
    # plain-INSERT store_episode (once per process per DB file).
    _ensure_episodic_deduped()

def init_embeddings_db() -> None:
    with _get_conn(EMBEDDINGS_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS episodic_embeddings (
                task_id TEXT PRIMARY KEY,
                embedding BLOB
            )
        """)
        conn.commit()

# Epic 9: embedding-endpoint circuit breaker. When the embedding
# endpoint is unreachable, each attempt costs LiteLLM retry/backoff
# (~1s, up to the 30s wait_for bound if the endpoint blackholes), and
# a single query could pay it 1 + backfill_limit times — measured:
# a 5.0s first query over 2,000 unembedded episodes with Ollama down.
# After a connection-class failure, attempts pause for the cooldown
# and callers degrade to lexical retrieval immediately; the failure
# itself is still logged distinctly (below), and any success resets
# the breaker. Arbitrary errors (bad request, auth) do NOT trip it.
_EMBED_BREAKER_COOLDOWN_S = 60.0
_embed_unavailable_until: float = 0.0


def _embed_breaker_open() -> bool:
    return time.monotonic() < _embed_unavailable_until


def embedding_breaker_open() -> bool:
    """Public view of the shared embedding circuit breaker (Epic 9).

    Epic 8, E8-3.2: the workspace prose indexer shares this breaker
    with episodic memory — one endpoint, one cooldown. Indexing
    checks it before attempting embeds and queues instead of
    hammering a dead endpoint; search degrades to lexical-only.
    """
    return _embed_breaker_open()


def _trip_embed_breaker(error: Exception) -> None:
    global _embed_unavailable_until
    if isinstance(error, (ConnectionError, TimeoutError)) or type(error).__name__ in (
        "APIConnectionError",
        "ConnectError",
    ):
        _embed_unavailable_until = time.monotonic() + _EMBED_BREAKER_COOLDOWN_S


async def _embed_text(text: str, task_id: str | None = None,
                    context: str = "store",
                    *, config: UserConfig | None = None) -> list[float] | None:
    """Embed ``text`` via Ollama. Returns ``None`` on failure — but never
    silently: every failure is logged distinctly (model, error, episode id)
    to the audit log and the module logger. No retry/backfill here
    (Phase 10.3); callers must treat ``None`` as "not embedded".

    Epic 8, E8-3.2: the model is the unified configured embedder
    (resolve_embedder) — the same one the prose index embeds with.
    Episodic's own call sites deliberately do NOT thread their
    ``config`` argument in: they resolve the ambient persisted
    config (the same source their config was loaded from), and the
    strict-signature test doubles that replace this function
    (tests/test_phase10_3_episodic_recall.py) keep working. The
    ``config`` kwarg serves the public embed_text wrapper, whose
    callers (the prose indexer) may carry an explicit override.
    """
    global _embed_unavailable_until
    if _embed_breaker_open():
        # Degraded mode (Epic 9): the endpoint recently proved
        # unreachable; skip the doomed attempt. Not audit-logged per
        # episode — the tripping failure already was.
        logger.debug("Embedding skipped: endpoint in circuit-breaker cooldown")
        return None
    model = litellm_embedding_model(config)
    litellm = _load_litellm()  # Epic 9: deferred import, bound locally
    try:
        response = await asyncio.wait_for(
            litellm.aembedding(
                model=model,
                input=[text],
                api_base="http://127.0.0.1:11434",
                # Single attempt, per this function's contract ("no
                # retry here" — the system's retry IS the backfill).
                # LiteLLM's default retries cost ~3.9s of backoff
                # against a dead endpoint before the breaker can trip.
                num_retries=0,
            ),
            timeout=EMBEDDING_TIMEOUT_SECONDS
        )
        _embed_unavailable_until = 0.0
        return response.data[0]["embedding"]
    except TimeoutError as e:
        _trip_embed_breaker(e)
        _log_embedding_failure(e, task_id, context, model=model)
        return None
    except Exception as e:  # noqa: BLE001 — embedding is best-effort: any failure means "not embedded", never a raised error
        _trip_embed_breaker(e)
        _log_embedding_failure(e, task_id, context, model=model)
        return None


async def embed_text(text: str, *, config: UserConfig | None = None,
                     context: str = "external") -> list[float] | None:
    """Public embedding entry point (Epic 8, E8-3.2).

    The workspace prose indexer and the ``workspace_search`` tool
    embed through here so episodic memory and the prose index share
    the unified model resolution AND the circuit breaker — one
    endpoint, one failure domain, one cooldown.
    """
    return await _embed_text(text, context=context, config=config)


def _log_embedding_failure(error: Exception, task_id: str | None,
                           context: str, *, model: str | None = None) -> None:
    """Log one embedding failure distinctly: model, error, episode id.

    Uses the module-level audit logger (already imported at the top of
    this module — no new import, no circular-import risk) via the
    existing generic ``log_event``; no new audit event type was needed.
    ``model`` is the resolved embedder the failed attempt used
    (E8-3.2); it defaults to the module default's LiteLLM form.
    """
    model = model or EMBEDDING_MODEL
    details = {
        "task_id": task_id,
        "context": context,
        "model": model,
        "error": str(error) or repr(error),
        "error_type": type(error).__name__,
    }
    _audit_logger.log_event("EPISODIC_EMBEDDING_FAILED", details)
    logger.warning(
        "EPISODIC_EMBEDDING_FAILED task_id=%s model=%s error_type=%s error=%s",
        task_id, model, type(error).__name__, error,
        exc_info=True,  # noqa: LOG014 — this helper is only ever called from except handlers; exc_info captures the active exception
    )
    # Epic 4, E4-04: the most recent failure is part of embedding
    # health, surfaced by embedding_stats() in Diagnostics.
    global _LAST_EMBEDDING_FAILURE
    _LAST_EMBEDDING_FAILURE = {
        "task_id": task_id,
        "model": model,
        "error_type": type(error).__name__,
        "error": str(error) or repr(error),
        "at": int(time.time()),
    }


_LAST_EMBEDDING_FAILURE: dict[str, Any] | None = None


def embedding_stats() -> dict[str, Any]:
    """Epic 4, E4-04: embedding health for Diagnostics — how many
    episodes exist, how many carry an embedding, how many are
    pending backfill, and the most recent failure this process saw.
    """
    init_episodic_db()
    init_embeddings_db()
    with _get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM episodic_memory").fetchone()[0]
    with _get_conn(EMBEDDINGS_DB_PATH) as conn:
        embedded = conn.execute(
            "SELECT COUNT(*) FROM episodic_embeddings WHERE embedding IS NOT NULL"
        ).fetchone()[0]
    return {
        "episodes": int(total),
        "embedded": int(embedded),
        "pending": int(total) - int(embedded),
        # E8-3.2: the resolved unified embedder's LiteLLM form —
        # identical to EMBEDDING_MODEL under the default config.
        "model": litellm_embedding_model(),
        "last_failure": _LAST_EMBEDDING_FAILURE,
    }

def _cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    dot = sum(a * b for a, b in zip(vec1, vec2))
    norm_a = math.sqrt(sum(a * a for a in vec1))
    norm_b = math.sqrt(sum(b * b for b in vec2))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

async def store_episode(task_id: str, user_prompt: str, task_summary: str, tags: list[str], outcome: str, thinking_log: str | None = None) -> None:
    init_episodic_db()
    init_embeddings_db()
    now = int(time.time())
    tags_str = ",".join(tags)
    with _get_conn() as conn:
        # Phase 10.3 A2: FTS5 cannot declare UNIQUE / PRIMARY KEY on
        # task_id (SQLite rejects it at CREATE VIRTUAL TABLE time), so
        # uniqueness is enforced here instead: delete any existing row
        # for this task_id in the same transaction before inserting, so
        # a re-store (resume / retry) replaces the old row instead of
        # leaving a duplicate that would double-count in BM25/RRF.
        conn.execute(
            "DELETE FROM episodic_memory WHERE task_id = ?",
            (task_id,)
        )
        conn.execute(
            """INSERT INTO episodic_memory (task_id, timestamp, user_prompt, task_summary, tags, outcome)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, now, user_prompt, task_summary, tags_str, outcome)
        )
        if thinking_log:
            conn.execute(
                """INSERT OR REPLACE INTO episodic_thinking (task_id, thinking_log)
                   VALUES (?, ?)""",
                (task_id, thinking_log)
            )
        conn.commit()
    
    # Generate and store embedding
    embedding = await _embed_text(task_summary, task_id=task_id)
    if embedding:
        with _get_conn(EMBEDDINGS_DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO episodic_embeddings (task_id, embedding) VALUES (?, ?)",
                (task_id, pickle.dumps(embedding))
            )
            conn.commit()
    else:
        # Embedding failed (already logged distinctly by _embed_text).
        # Make "unembedded" explicit: drop any stale row for this task so
        # absence from episodic_embeddings is a truthful marker, and the
        # EPISODIC_MEMORY_STORED audit below records embedded=False.
        with _get_conn(EMBEDDINGS_DB_PATH) as conn:
            conn.execute(
                "DELETE FROM episodic_embeddings WHERE task_id = ?",
                (task_id,)
            )
            conn.commit()

    _audit_logger.log_event("EPISODIC_MEMORY_STORED", {
        "task_id": task_id,
        "tags": tags,
        "embedded": embedding is not None
    })

async def query_episodes(query: str, limit: int = 5,
                       *, similarity_threshold: float | None = None,
                       backfill_limit: int = DEFAULT_QUERY_BACKFILL_LIMIT,
                       config: UserConfig | None = None) -> list[dict[str, Any]]:
    """Hybrid BM25 + semantic retrieval, merged with RRF.

    Phase 10.3 A2: the semantic leg is thresholded — an episode whose
    cosine similarity with the query is below ``similarity_threshold``
    never enters RRF, so irrelevant episodes can no longer dilute the
    results purely by rank. The threshold gates the semantic leg only:
    a lexical (BM25) match still returns on its own merit. Resolution
    order: explicit arg, then ``config.episodic_similarity_threshold``,
    then the module default. Before the semantic scan, up to
    ``backfill_limit`` episodes stored unembedded (Phase 10.1 marker:
    absent from ``episodic_embeddings``) are backfilled oldest-first,
    so an episode stored while Ollama was down becomes retrievable on
    the next query; pass ``backfill_limit=0`` to skip that.
    """
    threshold = _resolve_similarity_threshold(similarity_threshold, config)
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
                """SELECT e.*, t.thinking_log, e.rank 
                   FROM (
                       SELECT *, rank 
                       FROM episodic_memory 
                       WHERE episodic_memory MATCH ? 
                       ORDER BY rank LIMIT 100
                   ) e
                   LEFT JOIN episodic_thinking t ON e.task_id = t.task_id""",
                (clean_query,)
            )
            bm25_rows = cur.fetchall()
        except sqlite3.OperationalError:
            bm25_rows = []

        # Phase 10.3 A2: backfill episodes stored unembedded (Phase 10.1
        # marker: absent from episodic_embeddings) before the semantic
        # scan, so they can participate in this query. Bounded per call;
        # failures stay unembedded and retry on the next query.
        if backfill_limit and backfill_limit > 0:
            await backfill_episodic_embeddings(limit=backfill_limit, config=config)

        # 2. Semantic Search
        query_embedding = await _embed_text(query, context="query")
        semantic_scores = {}
        if query_embedding:
            with _get_conn(EMBEDDINGS_DB_PATH) as e_conn:
                e_cur = e_conn.cursor()
                e_cur.execute("SELECT task_id, embedding FROM episodic_embeddings")
                for row in e_cur.fetchall():
                    emb = pickle.loads(row["embedding"])
                    sim = _cosine_similarity(query_embedding, emb)
                    # Phase 10.3 A2: below-threshold episodes never enter
                    # RRF — without this, every episode scored ~0 still
                    # ranked and diluted the relevant hits.
                    if sim >= threshold:
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
                cur.execute("""
                    SELECT e.*, t.thinking_log, 0 as rank 
                    FROM episodic_memory e
                    LEFT JOIN episodic_thinking t ON e.task_id = t.task_id
                    WHERE e.task_id = ?
                """, (t_id,))
                fetched = cur.fetchone()
                if fetched:
                    rrf_scores[t_id] = {"row": dict(fetched), "score": 1.0 / (k + idx + 1)}

        # Sort combined results
        final_results = sorted(rrf_scores.values(), key=lambda x: x["score"], reverse=True)
        results = [res["row"] for res in final_results[:limit]]
        
        _audit_logger.log_event("EPISODIC_MEMORY_RETRIEVED", {
            "query": query,
            "results_count": len(results),
            "hybrid": query_embedding is not None,
            "similarity_threshold": threshold,
        })
        return results


def _resolve_similarity_threshold(similarity_threshold: float | None,
                                  config: UserConfig | None) -> float:
    """Explicit arg wins, then UserConfig, then the module default."""
    if similarity_threshold is not None:
        return float(similarity_threshold)
    cfg = config if config is not None else load_config()
    return float(getattr(cfg, "episodic_similarity_threshold",
                         EPISODIC_SIMILARITY_THRESHOLD))


def dedupe_episodic_rows() -> dict[str, Any]:
    """One-time migration: collapse duplicate episodic_memory rows.

    The old ``store_episode`` used a plain INSERT into the FTS5 table,
    which cannot declare a UNIQUE constraint, so re-stores left duplicate
    rows. For each duplicated ``task_id`` the newest row (latest
    timestamp, then highest rowid) survives and the rest are deleted.
    Nothing is silently dropped: the removed rows are logged to the audit
    log (EPISODIC_MEMORY_DEDUPED) with the affected task_ids and counts.
    Idempotent — a second run finds no duplicates and logs nothing.
    """
    init_episodic_db()
    with _get_conn() as conn:
        dup_ids = [
            r["task_id"] for r in conn.execute(
                "SELECT task_id FROM episodic_memory"
                " GROUP BY task_id HAVING COUNT(*) > 1"
            ).fetchall()
        ]
        removed = 0
        for task_id in dup_ids:
            rows = conn.execute(
                "SELECT rowid FROM episodic_memory WHERE task_id = ?"
                " ORDER BY timestamp DESC, rowid DESC",
                (task_id,),
            ).fetchall()
            for r in rows[1:]:
                conn.execute(
                    "DELETE FROM episodic_memory WHERE rowid = ?",
                    (r["rowid"],),
                )
                removed += 1
        conn.commit()
    result: dict[str, Any] = {
        "task_ids_deduped": len(dup_ids),
        "rows_removed": removed,
    }
    if removed:
        details = {"task_ids": dup_ids, "rows_removed": removed}
        _audit_logger.log_event("EPISODIC_MEMORY_DEDUPED", details)
        logger.warning("EPISODIC_MEMORY_DEDUPED %s", details)
    return result


def _ensure_episodic_deduped() -> None:
    """Run the dedupe migration once per process per DB file.

    Called from ``init_episodic_db`` so existing deployments heal
    themselves on first use after the upgrade, with no new caller wiring
    (the scheduler is Phase 10.3 A1's, out of scope for A2).
    """
    key = str(DB_PATH)
    if key in _DEDUPED_DB_PATHS:
        return
    _DEDUPED_DB_PATHS.add(key)
    dedupe_episodic_rows()


async def backfill_episodic_embeddings(limit: int | None = None,
                                       *, config: UserConfig | None = None
                                       ) -> dict[str, Any]:
    """Embed episodes stored unembedded and write their embeddings.

    The Phase 10.1 marker for "stored unembedded" is absence from
    ``episodic_embeddings`` (a failed store also deletes any stale row,
    so absence is truthful). Candidates are the oldest unembedded
    episodes first; ``limit`` caps how many are attempted in one call
    (``None`` = all). Each episode is embedded over its ``task_summary``
    — the same text ``store_episode`` embeds. A failed embed is already
    logged distinctly by ``_embed_text``; the episode simply stays
    unembedded and is retried on the next backfill. Episodes with an
    empty summary are skipped (nothing to embed). Returns counts and
    logs them (EPISODIC_EMBEDDINGS_BACKFILLED).
    """
    init_episodic_db()
    init_embeddings_db()
    with _get_conn(EMBEDDINGS_DB_PATH) as e_conn:
        embedded_ids = {
            r[0] for r in e_conn.execute("SELECT task_id FROM episodic_embeddings")
        }
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT task_id, task_summary FROM episodic_memory"
            " ORDER BY timestamp ASC"
        ).fetchall()
    unembedded = [r for r in rows if r["task_id"] not in embedded_ids]
    missing = [
        r for r in unembedded if (r["task_summary"] or "").strip()
    ]
    skipped_empty = len(unembedded) - len(missing)
    if limit is not None:
        missing = missing[:limit]

    backfilled = 0
    failed_ids: list[str] = []
    for r in missing:
        embedding = await _embed_text(
            r["task_summary"], task_id=r["task_id"], context="backfill"
        )
        if embedding:
            with _get_conn(EMBEDDINGS_DB_PATH) as e_conn:
                e_conn.execute(
                    "INSERT OR REPLACE INTO episodic_embeddings (task_id, embedding)"
                    " VALUES (?, ?)",
                    (r["task_id"], pickle.dumps(embedding)),
                )
                e_conn.commit()
            backfilled += 1
        else:
            failed_ids.append(r["task_id"])

    result: dict[str, Any] = {
        "backfilled": backfilled,
        "failed": len(failed_ids),
        "failed_task_ids": failed_ids,
        "skipped_empty": skipped_empty,
        "checked": len(rows),
    }
    _audit_logger.log_event("EPISODIC_EMBEDDINGS_BACKFILLED", result)
    logger.info("EPISODIC_EMBEDDINGS_BACKFILLED %s", result)
    return result

def get_all_episodes(limit: int = 50) -> list[dict[str, Any]]:
    init_episodic_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT e.*, t.thinking_log 
            FROM episodic_memory e
            LEFT JOIN episodic_thinking t ON e.task_id = t.task_id
            ORDER BY e.timestamp DESC LIMIT ?
        """, (limit,))
        return [dict(row) for row in cur.fetchall()]

def delete_episode(task_id: str) -> bool:
    init_episodic_db()
    init_embeddings_db()
    with _get_conn() as conn:
        conn.execute("DELETE FROM episodic_memory WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM episodic_thinking WHERE task_id = ?", (task_id,))
        conn.commit()
    with _get_conn(EMBEDDINGS_DB_PATH) as e_conn:
        e_conn.execute("DELETE FROM episodic_embeddings WHERE task_id = ?", (task_id,))
        e_conn.commit()
    _audit_logger.log_event("EPISODIC_MEMORY_DELETED", {"task_id": task_id})
    return True

def clear_all_episodes() -> bool:
    init_episodic_db()
    init_embeddings_db()
    with _get_conn() as conn:
        conn.execute("DELETE FROM episodic_memory")
        conn.execute("DELETE FROM episodic_thinking")
        conn.commit()
    with _get_conn(EMBEDDINGS_DB_PATH) as e_conn:
        e_conn.execute("DELETE FROM episodic_embeddings")
        e_conn.commit()
    _audit_logger.log_event("EPISODIC_MEMORY_CLEARED_ALL", {})
    return True

# ---------------------------------------------------------------------------
# Phase 10.3 A1: retention / pruning. Before this, episodic memory had no
# pruning, decay, or TTL — episodic_memory + episodic_embeddings grew
# without bound. Bounds come from UserConfig (config.py is the single
# source of the numbers); explicit args here override them, and a bound
# <= 0 disables pruning on that axis.
# ---------------------------------------------------------------------------

def _delete_episode_rows(task_ids: list[str]) -> dict[str, int]:
    """Delete the FTS, thinking, and embedding rows for ``task_ids``.

    An episode is three rows in two DBs; they are always deleted together
    so pruning never leaves an orphan embedding/thinking row behind.
    Returns row counts per table.
    """
    counts = {"episodes": 0, "thinking_rows": 0, "embedding_rows": 0}
    if not task_ids:
        return counts
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.executemany("DELETE FROM episodic_memory WHERE task_id = ?", [(t,) for t in task_ids])
        counts["episodes"] = cur.rowcount
        cur.executemany("DELETE FROM episodic_thinking WHERE task_id = ?", [(t,) for t in task_ids])
        counts["thinking_rows"] = cur.rowcount
        conn.commit()
    with _get_conn(EMBEDDINGS_DB_PATH) as e_conn:
        e_cur = e_conn.cursor()
        e_cur.executemany("DELETE FROM episodic_embeddings WHERE task_id = ?", [(t,) for t in task_ids])
        counts["embedding_rows"] = e_cur.rowcount
        e_conn.commit()
    return counts

def prune_episodes(retention_days: int | None = None,
                   max_episodes: int | None = None,
                   *, config: UserConfig | None = None,
                   now: int | None = None) -> dict[str, int]:
    """Prune episodic memory by age, then by count (oldest first).

    Age: episodes with ``timestamp`` older than ``retention_days`` are
    removed. Count: if more than ``max_episodes`` remain, the oldest
    beyond the cap are removed. Each pruned episode loses its FTS row,
    thinking row, and embedding row together (see ``_delete_episode_rows``).
    Nothing else references an episode, so no keep-guard applies here —
    the guard lives in the knowledge graph, where facts reference entities.
    Returns counts and logs them to the audit log (EPISODIC_MEMORY_PRUNED).
    """
    cfg = config if config is not None else load_config()
    if retention_days is None:
        retention_days = cfg.episodic_retention_days
    if max_episodes is None:
        max_episodes = cfg.episodic_max_episodes
    now = int(time.time()) if now is None else int(now)

    init_episodic_db()
    init_embeddings_db()

    pruned_ids: list[str] = []
    if retention_days and retention_days > 0:
        cutoff = now - int(retention_days) * 86400
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT task_id FROM episodic_memory WHERE timestamp < ?",
                (cutoff,),
            ).fetchall()
        pruned_ids.extend(r["task_id"] for r in rows)

    if max_episodes and max_episodes > 0:
        with _get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM episodic_memory").fetchone()[0]
            # Episodes already selected for age pruning are gone once the
            # deletes below run, so subtract them from the count.
            excess = total - len(pruned_ids) - int(max_episodes)
            if excess > 0:
                # Age-selected ids are the oldest rows, so fetching
                # excess + len(pruned_ids) oldest rows guarantees at least
                # `excess` not-yet-selected candidates.
                rows = conn.execute(
                    "SELECT task_id FROM episodic_memory ORDER BY timestamp ASC LIMIT ?",
                    (excess + len(pruned_ids),),
                ).fetchall()
                seen = set(pruned_ids)
                added = 0
                for r in rows:
                    if added >= excess:
                        break
                    if r["task_id"] not in seen:
                        seen.add(r["task_id"])
                        pruned_ids.append(r["task_id"])
                        added += 1

    counts = _delete_episode_rows(pruned_ids)
    result = {
        "pruned_episodes": counts["episodes"],
        "pruned_thinking_rows": counts["thinking_rows"],
        "pruned_embedding_rows": counts["embedding_rows"],
        "retention_days": retention_days,
        "max_episodes": max_episodes,
    }
    _audit_logger.log_event("EPISODIC_MEMORY_PRUNED", result)
    logger.info("EPISODIC_MEMORY_PRUNED %s", result)
    return result
