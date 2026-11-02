import asyncio
import logging
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from aether_engine.audit import AuditLogger
from aether_engine.config import UserConfig, load_config
from aether_engine.embedding_codec import decode_embedding, encode_embedding

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

# E4-F11: application schema versions for this module's stores.
# 1 = the FTS5 episodic_memory + episodic_thinking schema (no column
# migrations to date). The embeddings store: 1 = the original
# episodic_embeddings table; 2 = N02's model / dimension identity
# columns. Bump with every migration; _record_schema_version keeps
# the recorded marker in step.
EPISODIC_SCHEMA_VERSION = 1
EMBEDDINGS_SCHEMA_VERSION = 2

_SCHEMA_META_DDL = """
    CREATE TABLE IF NOT EXISTS aether_schema_meta (
        store TEXT PRIMARY KEY,
        version INTEGER NOT NULL
    )
"""


def _record_schema_version(conn: sqlite3.Connection, store: str, version: int) -> None:
    """Record ``store``'s schema version after its migrations ran.

    The per-store row in ``aether_schema_meta`` is the marker a
    future migration gates on (memory.db is shared by several
    stores, so one file-level number cannot speak for each of them);
    ``PRAGMA user_version`` mirrors the highest store version in the
    file as the file-level marker.
    """
    conn.execute(_SCHEMA_META_DDL)
    conn.execute(
        "INSERT INTO aether_schema_meta (store, version) VALUES (?, ?) "
        "ON CONFLICT(store) DO UPDATE SET version = excluded.version",
        (store, version),
    )
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > current:
        conn.execute(f"PRAGMA user_version = {int(version)}")


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
        _record_schema_version(conn, "episodic", EPISODIC_SCHEMA_VERSION)
        conn.commit()
    # Phase 10.3 A2: collapse any duplicate FTS rows left by the old
    # plain-INSERT store_episode (once per process per DB file).
    _ensure_episodic_deduped()
    # E4-F04: drop embedding rows whose episode is gone (split-delete
    # residue) once per process per DB pair.
    _ensure_episodic_reconciled()

def init_embeddings_db() -> None:
    with _get_conn(EMBEDDINGS_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS episodic_embeddings (
                task_id TEXT PRIMARY KEY,
                embedding BLOB,
                model TEXT,
                dimension INTEGER
            )
        """)
        # Finding N02: rows written before model identity existed
        # gain the columns with NULL identity — they are of UNKNOWN
        # model and stay ineligible for recall (and backfill
        # candidates) until rebuilt; they are never silently treated
        # as current-model vectors.
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(episodic_embeddings)")
        }
        if "model" not in columns:
            conn.execute(
                "ALTER TABLE episodic_embeddings ADD COLUMN model TEXT")
        if "dimension" not in columns:
            conn.execute(
                "ALTER TABLE episodic_embeddings ADD COLUMN dimension INTEGER")
        _record_schema_version(conn, "episodic_embeddings", EMBEDDINGS_SCHEMA_VERSION)
        conn.commit()

def _store_embedding_row(task_id: str, embedding: list[float]) -> None:
    """Write one embedding row stamped with its model identity (N02).

    The stamp is the ambient resolved embedder — the same resolution
    ``_embed_text`` just used, since episodic's call sites deliberately
    do not thread a config (see its docstring). Recall only ever
    scores rows whose stamp matches the active embedder and whose
    dimension matches the query vector exactly.
    """
    with _get_conn(EMBEDDINGS_DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO episodic_embeddings"
            " (task_id, embedding, model, dimension) VALUES (?, ?, ?, ?)",
            (task_id, encode_embedding(embedding),
             resolve_embedder(), len(embedding)),
        )
        conn.commit()

def _episodic_db_file() -> str:
    """The file the episodic store's own connections actually serve.

    Resolved through ``_get_conn`` rather than read off the
    ``DB_PATH`` constant: embedders of this module (and several test
    suites) redirect connections, not constants, and the E4-F03
    fence must guard the store the writers are really using.
    """
    with _get_conn() as conn:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main" and row[2]:
                return str(row[2])
    return str(DB_PATH)

def _embeddings_db_file() -> str:
    """The file the embeddings store's own connections actually
    serve — the mirror of :func:`_episodic_db_file`, for the
    cross-database delete/reconcile paths (E4-F04)."""
    with _get_conn(EMBEDDINGS_DB_PATH) as conn:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main" and row[2]:
                return str(row[2])
    return str(EMBEDDINGS_DB_PATH)

def _delete_embedding_row(task_id: str) -> None:
    """Drop any embedding row for ``task_id`` (mark it unembedded)."""
    with _get_conn(EMBEDDINGS_DB_PATH) as conn:
        conn.execute(
            "DELETE FROM episodic_embeddings WHERE task_id = ?",
            (task_id,),
        )
        conn.commit()

def _store_embedding_row_if_current(task_id: str, embedding: list[float],
                                    expected_summary: str) -> bool:
    """Fenced embedding write (E4-F03): store ``embedding`` for
    ``task_id`` only if the episode still exists AND its stored
    ``task_summary`` is still exactly ``expected_summary`` — the text
    that was embedded.

    Embedding is awaited, so between reading a summary and writing
    its vector the episode can be revised (a newer store has already
    written its own vector — this one is stale) or deleted (this one
    would resurrect as an orphan row). The existence + currency
    check and the write are a SINGLE SQL statement — the embeddings
    connection ATTACHes the episodic store, so the ``WHERE EXISTS``
    guard and the ``INSERT`` commit atomically with respect to every
    other writer, coroutine or thread; there is no check-then-write
    window. The currency signal is the summary text itself: a vector
    is a pure function of (summary, embedder), so an unchanged
    summary makes this vector correct whatever else about the row
    changed, and a changed summary makes it stale whatever else
    stayed the same. (The schema carries no revision counter, and
    the store timestamp has one-second resolution — two revisions
    inside one second would be indistinguishable by it.)

    Callers must have initialised both stores (both existing callers
    do). Returns True when the row was written.
    """
    with _get_conn(EMBEDDINGS_DB_PATH) as conn:
        conn.execute("ATTACH DATABASE ? AS episodic_mem",
                     (_episodic_db_file(),))
        cur = conn.execute(
            "INSERT OR REPLACE INTO episodic_embeddings"
            " (task_id, embedding, model, dimension)"
            " SELECT ?, ?, ?, ? WHERE EXISTS ("
            " SELECT 1 FROM episodic_mem.episodic_memory"
            " WHERE task_id = ? AND task_summary IS ?)",
            (task_id, encode_embedding(embedding),
             resolve_embedder(), len(embedding),
             task_id, expected_summary),
        )
        conn.commit()
        return cur.rowcount > 0


def reset_episodic_embeddings_for_embedder(embedder: str) -> dict[str, Any]:
    """Invalidate episodic vectors not produced by ``embedder`` (N02).

    The embedder-switch counterpart of the prose store's
    ``reset_chunks_for_embedder``: vectors are incomparable across
    models, so a switch deletes every row whose stamp is not the new
    embedder (NULL stamps — pre-identity rows of unknown model —
    included). Deletion is the invalidation; absence from
    ``episodic_embeddings`` is exactly what the existing backfill
    treats as "needs embedding", so the episodes are rebuilt under
    the new model on the next query/startup backfill instead of
    staying recallable in the interim.
    """
    init_embeddings_db()
    embedder = str(embedder or "").strip()
    if not embedder:
        return {"embedder": embedder, "removed": 0}
    with _get_conn(EMBEDDINGS_DB_PATH) as conn:
        removed = conn.execute(
            "DELETE FROM episodic_embeddings WHERE model IS NOT ?",
            (embedder,),
        ).rowcount
        conn.commit()
    result: dict[str, Any] = {"embedder": embedder, "removed": int(removed)}
    if removed:
        _audit_logger.log_event("EPISODIC_EMBEDDINGS_INVALIDATED", result)
        logger.info("EPISODIC_EMBEDDINGS_INVALIDATED %s", result)
    return result

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


# E9-F01: the class NAMES that mark a connection/timeout-class
# failure for the breaker, matched against the raised type and its
# bases. The real provider stacks wrap their timeouts in classes
# that inherit neither builtin: litellm.Timeout inherits
# openai.APITimeoutError (itself an APIConnectionError), and
# httpx.ReadTimeout / ConnectTimeout inherit httpx.TimeoutException
# — the original exact-name check saw none of those, so real
# timeouts never tripped the breaker and a dead endpoint was retried
# on every call. Name matching keeps litellm / openai / httpx out of
# this module's import graph (Epic 9 lazy-import rule). The set is
# deliberately explicit — arbitrary errors still never trip it.
_BREAKER_ERROR_NAMES = frozenset({
    "APIConnectionError",
    "APITimeoutError",
    "ConnectError",
    "ConnectTimeout",
    "PoolTimeout",
    "ReadTimeout",
    "Timeout",
    "TimeoutException",
    "WriteTimeout",
})


def _trip_embed_breaker(error: Exception) -> None:
    global _embed_unavailable_until
    if isinstance(error, (ConnectionError, TimeoutError)) or any(
        cls.__name__ in _BREAKER_ERROR_NAMES for cls in type(error).__mro__
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
        _record_embedding_usage(response, model)
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


def _record_embedding_usage(response: Any, model: str) -> None:
    """Record one completed embedding call in the usage ledger.

    Finding M19: embeddings call LiteLLM directly, bypassing the
    provider recording seam — but they are real model calls (the
    local Ollama endpoint this module posts to, $0.00) and PRD A8
    counts every call, local included. Observational only: a
    recording failure must never break embedding, so it is swallowed
    to a debug log (``record_provider_call`` itself never raises;
    this guards the import and usage extraction).
    """
    try:
        from aether_engine.cost.ledger import record_provider_call

        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", None)
        if input_tokens is None:
            input_tokens = getattr(usage, "total_tokens", None)
        record_provider_call(
            model_name=model,
            pricing_model=model,
            provider_name="ollama",
            input_tokens=input_tokens,
            output_tokens=0,
            agent_name="embedding",
        )
    except Exception:  # usage recording is observational only.
        logger.debug("Embedding usage recording failed", exc_info=True)


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
        # N02: only rows stamped with the ACTIVE embedder count as
        # embedded — a stale-model row is pending rebuild, and
        # Diagnostics must not claim recall is healthy over vectors
        # the current model cannot legitimately score.
        embedded = conn.execute(
            "SELECT COUNT(*) FROM episodic_embeddings"
            " WHERE embedding IS NOT NULL AND model = ?",
            (resolve_embedder(),),
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
    # N02: a dimension mismatch is a data error (vectors from
    # different models, or a corrupt row) — never an invitation to
    # zip()-truncate the longer vector into a plausible score.
    if len(vec1) != len(vec2):
        return 0.0
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
    try:
        embedding = await _embed_text(task_summary, task_id=task_id)
    except asyncio.CancelledError:
        # E4-F03: a cancelled embed must leave the same truthful state
        # as a failed one. Without this, the previous revision's
        # vector stays attached to the new text — the episode remains
        # semantically recallable for the OLD summary and reads as
        # embedded to embedding_stats/backfill, so it is never
        # rebuilt. Drop the stale row exactly as the failure branch
        # below does, then let the cancellation propagate.
        _delete_embedding_row(task_id)
        raise
    if embedding:
        # E4-F03 fence: a revision or delete may have landed while
        # the embed was awaited; write only if this episode is still
        # the revision that was embedded.
        _store_embedding_row_if_current(task_id, embedding, task_summary)
    else:
        # Embedding failed (already logged distinctly by _embed_text).
        # Make "unembedded" explicit: drop any stale row for this task so
        # absence from episodic_embeddings is a truthful marker, and the
        # EPISODIC_MEMORY_STORED audit below records embedded=False.
        _delete_embedding_row(task_id)

    _audit_logger.log_event("EPISODIC_MEMORY_STORED", {
        "task_id": task_id,
        "tags": tags,
        "embedded": embedding is not None
    })

_FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# FTS5 operator words: as bare tokens they are query syntax, not
# search terms. Dropping them keeps the expression a plain keyword
# search (implicit AND — dropping AND is exactly its operator
# meaning; OR/NOT/NEAR lose their widening/negating power rather
# than hijacking or breaking the query).
_FTS_OPERATOR_WORDS = frozenset({"AND", "OR", "NOT", "NEAR"})


def _fts_match_expression(query: str) -> str:
    """Build a safe FTS5 MATCH expression from a raw user query.

    E4-F10: FTS5 treats characters like ``?``, ``*``, ``(``, ``:``
    and the bare words AND/OR/NOT as query syntax, so a raw user
    query containing them makes MATCH raise OperationalError — and
    the caller used to swallow that by discarding the entire lexical
    leg, leaving embeddings-down recall with nothing. The old
    sanitizer stripped only quotes and colons. Reducing the query
    to plain word tokens (minus the operator words), each quoted
    as a literal phrase, makes punctuation degrade to "these words"
    (implicit AND, as before) instead of a dead search. Returns ""
    when the query has no searchable tokens — the caller treats
    that as no-match.
    """
    tokens = [
        token for token in _FTS_TOKEN_RE.findall(query)
        if token.upper() not in _FTS_OPERATOR_WORDS
    ]
    return " ".join(f'"{token}"' for token in tokens)


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
        
        # E4-F10: never let raw user text reach the FTS5 parser —
        # build the MATCH expression from word tokens instead.
        clean_query = _fts_match_expression(query)

        if not clean_query:
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
                # N02: only vectors produced by the ACTIVE embedder
                # at exactly the query vector's dimension are
                # eligible. Stale-model and unknown-model (NULL)
                # rows never reach the scorer — eligibility is
                # decided here, at recall time, so it holds even if
                # a switch happened outside the switch handler.
                e_cur.execute(
                    "SELECT task_id, embedding FROM episodic_embeddings"
                    " WHERE model = ? AND dimension = ?",
                    (resolve_embedder(), len(query_embedding)),
                )
                for row in e_cur.fetchall():
                    # S1-M7: rows without the tagged safe format
                    # (legacy pickle rows included) are never
                    # deserialised — the embedding reads as missing
                    # and the row is skipped; it is recomputable via
                    # backfill under the new format.
                    emb = decode_embedding(row["embedding"])
                    if emb is None:
                        continue
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
    so absence is truthful); since N02 the marker is absence of a row
    stamped with the ACTIVE embedder, so stale-model and pre-identity
    rows are rebuild candidates too. Candidates are the oldest unembedded
    episodes first; ``limit`` caps how many are attempted in one call
    (``None`` = all). Each episode is embedded over its ``task_summary``
    — the same text ``store_episode`` embeds. A failed embed is already
    logged distinctly by ``_embed_text``; the episode simply stays
    unembedded and is retried on the next backfill. Episodes with an
    empty summary are skipped (nothing to embed). Writes are fenced
    (E4-F03): an episode revised or deleted while its embed was in
    flight is counted under ``fenced`` and its stale vector is never
    written. Returns counts and logs them
    (EPISODIC_EMBEDDINGS_BACKFILLED).
    """
    init_episodic_db()
    init_embeddings_db()
    with _get_conn(EMBEDDINGS_DB_PATH) as e_conn:
        # N02: "embedded" means embedded under the ACTIVE embedder.
        # Rows stamped with another model — or with no stamp at all
        # (pre-identity rows) — are rebuild candidates, exactly like
        # episodes with no row.
        embedded_ids = {
            r[0] for r in e_conn.execute(
                "SELECT task_id FROM episodic_embeddings WHERE model = ?",
                (resolve_embedder(),),
            )
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
    fenced = 0
    failed_ids: list[str] = []
    for r in missing:
        embedding = await _embed_text(
            r["task_summary"], task_id=r["task_id"], context="backfill"
        )
        if embedding:
            # E4-F03 fence: the snapshot above is only a read; the
            # episode may have been revised (its own store wrote a
            # newer vector) or deleted while this embed was awaited.
            # Write only if the episode still exists with exactly
            # the snapshotted summary — otherwise this vector is
            # already stale, or an orphan for a deleted episode.
            if _store_embedding_row_if_current(
                    r["task_id"], embedding, r["task_summary"]):
                backfilled += 1
            else:
                fenced += 1
        else:
            failed_ids.append(r["task_id"])

    result: dict[str, Any] = {
        "backfilled": backfilled,
        "fenced": fenced,
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

# ---------------------------------------------------------------------------
# E4-F04: episode deletion spans two databases (episodes + thinking in
# memory.db, vectors in embeddings.db) and must land — or not land —
# as one unit. The old code committed memory.db first and embeddings.db
# second as independent transactions: a failure at the second commit
# left the episode gone and its vector orphaned (stats 0 / 1 / -1),
# with no way to roll the first commit back. All three delete paths
# (single, clear-all, prune) now funnel through
# ``_delete_episode_data``, which runs every delete on ONE connection
# with the embeddings file ATTACHed — the same cross-database
# mechanism the E4-F03 fence uses — under a single transaction with a
# single commit. A failure at any statement reaches the connection's
# rollback and undoes BOTH sides.
#
# Crash honesty: with rollback-journal files that single commit is
# also atomic across the two files (SQLite's master-journal commit).
# memory.db, however, is shared with the knowledge-graph and session
# stores, which put it in WAL mode, and SQLite does not guarantee
# cross-file atomicity for a transaction spanning a WAL database if
# the process dies DURING the commit. A kill before the commit is
# still fully safe (uncommitted work is never visible, in any journal
# mode). The only residue a mid-commit WAL split could leave is an
# orphan vector — never a missing one, which is the store's ordinary
# "unembedded" state that backfill rebuilds — and
# ``_ensure_episodic_reconciled`` removes exactly those orphans at
# init, which also heals splits the pre-fix two-commit code may have
# left on existing disks.
# ---------------------------------------------------------------------------

def _delete_episode_data(task_ids: list[str] | None) -> dict[str, int]:
    """Delete episode rows from BOTH stores in one transaction.

    ``task_ids=None`` deletes every episode (clear-all). Returns row
    counts per table. See the E4-F04 note above for the atomicity
    argument and its one bounded residual.
    """
    counts = {"episodes": 0, "thinking_rows": 0, "embedding_rows": 0}
    if task_ids is not None and not task_ids:
        return counts
    emb_file = _embeddings_db_file()
    attached = emb_file != _episodic_db_file()
    emb = "episodic_emb." if attached else ""
    with _get_conn() as conn:
        if attached:
            conn.execute("ATTACH DATABASE ? AS episodic_emb", (emb_file,))
        cur = conn.cursor()
        if task_ids is None:
            counts["episodes"] = cur.execute(
                "DELETE FROM episodic_memory").rowcount
            counts["thinking_rows"] = cur.execute(
                "DELETE FROM episodic_thinking").rowcount
            counts["embedding_rows"] = cur.execute(
                f"DELETE FROM {emb}episodic_embeddings").rowcount
        else:
            params = [(t,) for t in task_ids]
            counts["episodes"] = cur.executemany(
                "DELETE FROM episodic_memory WHERE task_id = ?",
                params).rowcount
            counts["thinking_rows"] = cur.executemany(
                "DELETE FROM episodic_thinking WHERE task_id = ?",
                params).rowcount
            counts["embedding_rows"] = cur.executemany(
                f"DELETE FROM {emb}episodic_embeddings WHERE task_id = ?",
                params).rowcount
        conn.commit()
    return counts


def delete_episode(task_id: str) -> bool:
    init_episodic_db()
    init_embeddings_db()
    _delete_episode_data([task_id])
    _audit_logger.log_event("EPISODIC_MEMORY_DELETED", {"task_id": task_id})
    return True

def clear_all_episodes() -> bool:
    init_episodic_db()
    init_embeddings_db()
    _delete_episode_data(None)
    _audit_logger.log_event("EPISODIC_MEMORY_CLEARED_ALL", {})
    return True

# (mem_file, emb_file) pairs the orphan reconciliation has already
# run for in this process — the same once-per-process-per-DB guard
# shape as _DEDUPED_DB_PATHS.
_RECONCILED_DB_PAIRS: set = set()


def _sweep_orphan_embeddings() -> list[str]:
    """Delete embedding rows whose episode no longer exists.

    An embedding row is legitimate only alongside its episode row:
    every writer commits the episode first (store) or fences on its
    existence (backfill, E4-F03), so a vector without an episode is
    residue — of the pre-fix split delete (E4-F04), or of the one
    bounded WAL crash window the delete note above describes. The
    delete touches only the embeddings store, so it is atomic on its
    own; the episode set it judges against is read through an ATTACH
    in the same statement. Returns the removed task_ids.
    """
    mem_file = _episodic_db_file()
    with _get_conn(EMBEDDINGS_DB_PATH) as conn:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "episodic_embeddings" not in tables:
            return []
        if mem_file == _embeddings_db_file():
            episode_tables = tables
            mem_schema = ""
        else:
            conn.execute("ATTACH DATABASE ? AS episodic_mem", (mem_file,))
            episode_tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM episodic_mem.sqlite_master"
                    " WHERE type = 'table'")
            }
            mem_schema = "episodic_mem."
        if "episodic_memory" not in episode_tables:
            return []
        orphans = [
            row[0] for row in conn.execute(
                "SELECT task_id FROM episodic_embeddings"
                f" WHERE task_id NOT IN (SELECT task_id FROM {mem_schema}episodic_memory)")
        ]
        if orphans:
            conn.execute(
                "DELETE FROM episodic_embeddings"
                f" WHERE task_id NOT IN (SELECT task_id FROM {mem_schema}episodic_memory)")
            conn.commit()
        return orphans


def reconcile_episodic_orphans() -> dict[str, Any]:
    """Remove embedding rows left orphaned by a split delete.

    The repair half of E4-F04; runs automatically once per process
    per DB pair at init (see ``_ensure_episodic_reconciled``) and is
    safe to call again — a second run finds nothing. Removed rows
    are logged to the audit log (EPISODIC_ORPHAN_EMBEDDINGS_REMOVED),
    never silently dropped. Note the counts here cover this call's
    own sweep: on a fresh process the init above runs the guarded
    sweep first (logging its batch under the same event), so this
    call then reports only what was orphaned since.
    """
    init_episodic_db()
    init_embeddings_db()
    removed = _sweep_orphan_embeddings()
    result: dict[str, Any] = {"removed": len(removed), "task_ids": removed}
    if removed:
        _audit_logger.log_event("EPISODIC_ORPHAN_EMBEDDINGS_REMOVED", result)
        logger.warning("EPISODIC_ORPHAN_EMBEDDINGS_REMOVED %s", result)
    return result


def _ensure_episodic_reconciled() -> None:
    """Run the orphan sweep once per process per DB pair, from
    ``init_episodic_db`` — existing deployments heal themselves on
    first use, the same pattern as the dedupe migration."""
    key = (_episodic_db_file(), _embeddings_db_file())
    if key in _RECONCILED_DB_PAIRS:
        return
    _RECONCILED_DB_PAIRS.add(key)
    removed = _sweep_orphan_embeddings()
    if removed:
        details = {"removed": len(removed), "task_ids": removed}
        _audit_logger.log_event("EPISODIC_ORPHAN_EMBEDDINGS_REMOVED", details)
        logger.warning("EPISODIC_ORPHAN_EMBEDDINGS_REMOVED %s", details)

# ---------------------------------------------------------------------------
# Phase 10.3 A1: retention / pruning. Before this, episodic memory had no
# pruning, decay, or TTL — episodic_memory + episodic_embeddings grew
# without bound. Bounds come from UserConfig (config.py is the single
# source of the numbers); explicit args here override them, and a bound
# <= 0 disables pruning on that axis.
# ---------------------------------------------------------------------------

def _delete_episode_rows(task_ids: list[str]) -> dict[str, int]:
    """Delete the FTS, thinking, and embedding rows for ``task_ids``.

    An episode is three rows in two DBs; they are deleted together in
    ONE transaction (``_delete_episode_data``, E4-F04) so pruning
    never leaves an orphan embedding/thinking row behind — and a
    failure midway leaves all of them in place. Returns row counts
    per table.
    """
    return _delete_episode_data(task_ids)

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
