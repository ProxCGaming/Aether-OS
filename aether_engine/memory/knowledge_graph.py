import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from aether_engine.audit import AuditLogger
from aether_engine.config import UserConfig, load_config
from aether_engine.providers.base import BaseProvider

_audit_logger = AuditLogger()

logger = logging.getLogger("aether_engine.memory.knowledge_graph")

# Database path is ~/.aether/memory.db
DB_PATH = Path.home() / ".aether" / "memory.db"

# Phase 10.3 A3: DB file paths the one-time KG dedupe migration
# (dedupe_facts) has already run for in this process. A fresh tmp DB
# in tests gets its own key, so the migration still runs there on
# first init.
_DEDUPED_DB_PATHS = set()

def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn

def init_kg_db() -> None:
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                entity_type TEXT,
                first_seen INTEGER NOT NULL,
                last_updated INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                id TEXT PRIMARY KEY,
                entity_id TEXT NOT NULL,
                fact_text TEXT NOT NULL,
                source_task_id TEXT,
                timestamp INTEGER NOT NULL,
                confidence REAL,
                status TEXT NOT NULL DEFAULT 'active',
                superseded_by TEXT,
                FOREIGN KEY(entity_id) REFERENCES entities(id) ON DELETE CASCADE
            )
        """)
        # Epic 4, E4-02 migration: supersede support on pre-existing
        # databases. Idempotent column adds; existing rows default to
        # 'active', which is exactly their pre-Epic-4 meaning.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(facts)")}
        if "status" not in columns:
            conn.execute("ALTER TABLE facts ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        if "superseded_by" not in columns:
            conn.execute("ALTER TABLE facts ADD COLUMN superseded_by TEXT")
        conn.commit()
    # Phase 10.3 A3: collapse any duplicate facts left by the old
    # unconditional INSERT in extract_and_store_facts (once per
    # process per DB file).
    _ensure_kg_deduped()

# ---------------------------------------------------------------------------
# Phase 10.3 A3: KG fact dedupe + confidence-weighted ranking. Before
# this, extract_and_store_facts() did an unconditional INSERT (no dedupe
# on (entity, content) of any kind, no UNIQUE constraint on facts), and
# every retrieval ranked by timestamp DESC only, so a low-confidence
# LLM guess ranked equal to a high-confidence fact of the same age.
#
# Dedupe key: (entity name, fact text), compared case-insensitively
# after the graph's own normalisation — which is none: entity lookup is
# an exact-match on name and fact text is stored verbatim, so the key
# here is strip() + casefold() on both halves. The entity itself is
# NOT merged or renamed by dedupe (case-sensitive entity identity is
# critical/02 finding #11, a separate decision); only the duplicate
# fact rows are collapsed.
#
# Ranking: ORDER BY confidence DESC, timestamp DESC — higher-confidence
# facts rank above lower-confidence ones, all else equal; timestamp
# still breaks ties. Confidence NULL (LLM returned an explicit null,
# or a junk non-numeric value) counts as 0.0, so unknown confidence
# ranks below any stated confidence instead of first (SQLite sorts
# NULL, and TEXT, first under DESC).
# ---------------------------------------------------------------------------

def _normalise_kg_text(text: str | None) -> str:
    """Dedupe-key normalisation for KG entity names / fact text."""
    return (text or "").strip().casefold()


def _confidence_value(row: sqlite3.Row) -> float:
    """Numeric confidence of a fact row; 0.0 for NULL / junk values."""
    try:
        return float(row["confidence"])
    except (TypeError, ValueError):
        return 0.0


def _find_duplicate_fact_id(
    conn: sqlite3.Connection, entity_name: str, fact_text: str
) -> str | None:
    """Id of an existing fact with the same dedupe key, else None.

    Full scan of the facts table with the comparison in Python:
    SQLite's lower() / NOCASE are ASCII-only, while casefold() is
    Unicode-aware, and the table is bounded by A1 pruning (default
    max 5000 facts), so the scan stays cheap and correct.
    """
    target_entity = _normalise_kg_text(entity_name)
    target_fact = _normalise_kg_text(fact_text)
    rows = conn.execute(
        "SELECT f.id, e.name, f.fact_text FROM facts f "
        "JOIN entities e ON f.entity_id = e.id "
        "WHERE COALESCE(f.status, 'active') = 'active'"
    ).fetchall()
    for fact_id, name, text in rows:
        if (_normalise_kg_text(name) == target_entity
                and _normalise_kg_text(text) == target_fact):
            return fact_id
    return None


def dedupe_facts() -> dict[str, Any]:
    """One-time migration: collapse duplicate KG facts.

    For each (normalised entity name, normalised fact text) group with
    more than one row, the highest-confidence copy survives (ties break
    by earliest timestamp, then lowest id) and the rest are deleted.
    Nothing is silently dropped: the removed rows are logged to the
    audit log (KNOWLEDGE_GRAPH_DEDUPED) with their entity / text.
    Entities left with no facts are removed, mirroring delete_fact().
    Idempotent — a second run finds no duplicates and logs nothing.
    """
    init_kg_db()
    removed: list[dict[str, Any]] = []
    groups_deduped = 0
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT f.id, e.name, f.fact_text, f.confidence, f.timestamp "
            "FROM facts f JOIN entities e ON f.entity_id = e.id"
        ).fetchall()
        groups: dict[tuple, list[sqlite3.Row]] = {}
        for row in rows:
            key = (_normalise_kg_text(row["name"]),
                   _normalise_kg_text(row["fact_text"]))
            groups.setdefault(key, []).append(row)
        for members in groups.values():
            if len(members) < 2:
                continue
            groups_deduped += 1
            members.sort(key=lambda r: (-_confidence_value(r),
                                        r["timestamp"], r["id"]))
            for victim in members[1:]:
                conn.execute("DELETE FROM facts WHERE id = ?",
                             (victim["id"],))
                removed.append({
                    "fact_id": victim["id"],
                    "entity_name": victim["name"],
                    "fact_text": victim["fact_text"],
                })
        conn.execute(
            "DELETE FROM entities WHERE id NOT IN "
            "(SELECT DISTINCT entity_id FROM facts)"
        )
        conn.commit()
    result: dict[str, Any] = {
        "fact_groups_deduped": groups_deduped,
        "facts_removed": len(removed),
    }
    if removed:
        details = dict(result, removed=removed)
        _audit_logger.log_event("KNOWLEDGE_GRAPH_DEDUPED", details)
        logger.warning("KNOWLEDGE_GRAPH_DEDUPED %s", result)
    return result


def _ensure_kg_deduped() -> None:
    """Run the dedupe migration once per process per DB file.

    Called from init_kg_db() so existing deployments heal themselves
    on first use after the upgrade, with no new caller wiring.
    """
    key = str(DB_PATH)
    if key in _DEDUPED_DB_PATHS:
        return
    _DEDUPED_DB_PATHS.add(key)
    dedupe_facts()

async def extract_and_store_facts(
    provider: BaseProvider, 
    user_prompt: str, 
    assistant_response: str, 
    task_id: str
) -> None:
    init_kg_db()
    
    # Skip pure short greetings with no personal information or substance
    pure_greetings = {"hi", "hello", "hey", "hii", "heyy", "sup", "yo"}
    if user_prompt.strip().lower() in pure_greetings and len(assistant_response) < 40:
        return
    if len(user_prompt.strip()) < 3:
        return
        
    system_prompt = """
You are a Knowledge Extraction Engine.
Extract concrete facts from the conversation below about the user or the project.
Return a JSON array of objects, where each object has:
- "entity_name": The subject of the fact (e.g. "User", "Project X", "The API"). Max 3 words.
- "entity_type": e.g. "Person", "Project", "Concept", "Preference".
- "fact": A concise standalone statement (e.g. "User prefers Python over Java", "Project X uses SQLite").
- "confidence": Float between 0.0 and 1.0.

Only extract facts that are actually useful to remember long-term.
If no facts are present, return an empty array [].
Respond ONLY with raw JSON array, no markdown code blocks, no explanations.
"""
    conversation = f"User: {user_prompt}\nAssistant: {assistant_response}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": conversation}
    ]
    
    try:
        response_text = ""
        # The provider returns a stream
        async for chunk in provider.call_stream(messages=messages):
            if chunk.text:
                response_text += chunk.text
                
        response_text = response_text.strip()
        # Clean markdown code blocks if any
        response_text = response_text.removeprefix("```json")
        response_text = response_text.removesuffix("```")
        response_text = response_text.removeprefix("```")
            
        facts_list = json.loads(response_text.strip())
        
        if not isinstance(facts_list, list):
            return
            
        now = int(time.time())
        inserted_facts: list[dict] = []
        with _get_conn() as conn:
            for item in facts_list:
                entity_name = item.get("entity_name")
                if not entity_name: continue

                # Phase 10.3 A3: dedupe on (entity, content),
                # case-insensitively (see _normalise_kg_text). Checked
                # before the entity upsert, so a skipped duplicate does
                # not leave behind a fact-less entity row. A duplicate
                # is skipped, not re-inserted; the surviving copy is
                # left untouched (no confidence bump — that is
                # correction of a stored fact, not dedupe, and is out
                # of scope for this phase).
                fact_text = item.get("fact", "")
                if _find_duplicate_fact_id(conn, entity_name, fact_text) is not None:
                    logger.info(
                        "Skipping duplicate KG fact for entity %r: %r",
                        entity_name, fact_text,
                    )
                    _audit_logger.log_event("KNOWLEDGE_FACT_DUPLICATE_SKIPPED", {
                        "entity_name": entity_name,
                        "fact": fact_text,
                        "task_id": task_id,
                    })
                    continue

                # Upsert entity
                cur = conn.cursor()
                cur.execute("SELECT id FROM entities WHERE name = ?", (entity_name,))
                row = cur.fetchone()
                if row:
                    entity_id = row["id"]
                    cur.execute("UPDATE entities SET last_updated = ? WHERE id = ?", (now, entity_id))
                else:
                    entity_id = str(uuid.uuid4())
                    cur.execute(
                        "INSERT INTO entities (id, name, entity_type, first_seen, last_updated) VALUES (?, ?, ?, ?, ?)",
                        (entity_id, entity_name, item.get("entity_type", "Unknown"), now, now)
                    )

                # Insert fact
                fact_id = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, confidence) VALUES (?, ?, ?, ?, ?, ?)",
                    (fact_id, entity_id, fact_text, task_id, now, item.get("confidence", 1.0))
                )

                _audit_logger.log_event("KNOWLEDGE_FACT_STORED", {
                    "entity_name": entity_name,
                    "fact": fact_text,
                    "task_id": task_id
                })
                inserted_facts.append({
                    "fact_id": fact_id,
                    "entity_id": entity_id,
                    "entity_name": entity_name,
                    "fact_text": fact_text,
                })
            conn.commit()
        # Epic 4, E4-02: reconcile the new facts against what the
        # graph already believed about the same entities — a new fact
        # that contradicts an older active one supersedes it (the old
        # row is kept as history, never overwritten or deleted here).
        if inserted_facts:
            await _reconcile_superseded(provider, inserted_facts, task_id)
    except json.JSONDecodeError:
        logger.warning(f"Fact extraction returned invalid JSON. Raw output: {response_text[:100]}")
    except Exception as e:  # noqa: BLE001 — extraction is best-effort: a provider/DB failure is logged and must never fail the completed task
        logger.warning(f"Fact extraction failed: {e}")

async def _reconcile_superseded(provider, inserted_facts: list[dict], task_id: str) -> None:
    """Epic 4, E4-02: mark old facts superseded by contradictory new ones.

    For each entity that gained facts, the entity's OTHER active
    facts are shown to the provider alongside the new ones; the
    provider returns the (old, new) index pairs where the new fact
    directly replaces the old (a changed value for the same
    attribute — NOT merely a related or additional fact). Marked
    rows keep their text and gain status='superseded' +
    superseded_by=<new fact id>: history is preserved and default
    retrieval stops presenting them as current.

    Best-effort by design: any failure (provider error, bad JSON,
    out-of-range indices) leaves every fact active and is logged —
    storing the new facts already succeeded and must not be undone
    by a reconciliation problem.
    """
    by_entity: dict[str, list[dict]] = {}
    for item in inserted_facts:
        by_entity.setdefault(item["entity_id"], []).append(item)

    for entity_id, new_facts in by_entity.items():
        try:
            with _get_conn() as conn:
                new_ids = [f["fact_id"] for f in new_facts]
                placeholders = ",".join("?" for _ in new_ids)
                old_rows = conn.execute(
                    f"SELECT id, fact_text FROM facts WHERE entity_id = ? "
                    f"AND COALESCE(status, 'active') = 'active' AND id NOT IN ({placeholders}) "
                    f"ORDER BY timestamp",
                    (entity_id, *new_ids),
                ).fetchall()
            if not old_rows:
                continue
            old_facts = [{"id": r["id"], "fact_text": r["fact_text"]} for r in old_rows]
            listing_old = "\n".join(f"O{i}: {f['fact_text']}" for i, f in enumerate(old_facts))
            listing_new = "\n".join(f"N{i}: {f['fact_text']}" for i, f in enumerate(new_facts))
            prompt = (
                "A knowledge graph stores facts about one entity. NEW facts were just learned; "
                "some may directly REPLACE older facts (same attribute, changed value — e.g. "
                "moved city, changed job, new preference). Related-but-compatible facts do NOT "
                "replace anything.\n\n"
                f"OLD facts:\n{listing_old}\n\nNEW facts:\n{listing_new}\n\n"
                "Return ONLY a JSON array of objects {\"old\": <O index>, \"new\": <N index>} "
                "for each old fact a new fact directly replaces. Empty array [] if none."
            )
            response_text = ""
            async for chunk in provider.call_stream(messages=[
                {"role": "system", "content": "You reconcile contradicting facts. Respond ONLY with a raw JSON array."},
                {"role": "user", "content": prompt},
            ]):
                if chunk.text:
                    response_text += chunk.text
            cleaned = response_text.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.strip("`")
                cleaned = cleaned.removeprefix("json")
            pairs = json.loads(cleaned.strip())
            if not isinstance(pairs, list):
                continue
            marks: list[tuple[str, str]] = []
            for pair in pairs:
                if not isinstance(pair, dict):
                    continue
                old_idx, new_idx = pair.get("old"), pair.get("new")
                if isinstance(old_idx, int) and isinstance(new_idx, int) \
                        and 0 <= old_idx < len(old_facts) and 0 <= new_idx < len(new_facts):
                    marks.append((old_facts[old_idx]["id"], new_facts[new_idx]["fact_id"]))
            if not marks:
                continue
            with _get_conn() as conn:
                for old_id, new_id in marks:
                    conn.execute(
                        "UPDATE facts SET status = 'superseded', superseded_by = ? "
                        "WHERE id = ? AND COALESCE(status, 'active') = 'active'",
                        (new_id, old_id),
                    )
                conn.commit()
            for old_id, new_id in marks:
                _audit_logger.log_event("KNOWLEDGE_FACT_SUPERSEDED", {
                    "fact_id": old_id,
                    "superseded_by": new_id,
                    "entity_id": entity_id,
                    "task_id": task_id,
                })
        except Exception as e:  # noqa: BLE001 — reconciliation must never break extraction
            logger.warning(f"Supersede reconciliation failed for entity {entity_id}: {e}")


def get_entity_facts(entity_name: str, include_history: bool = False) -> list[dict[str, Any]]:
    """Facts for one entity. Epic 4, E4-02: superseded facts (the
    history) are excluded unless ``include_history`` — retrieval must
    never present a replaced fact as current."""
    init_kg_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        status_filter = "" if include_history else " AND COALESCE(f.status, 'active') = 'active'"
        cur.execute(f"""
            SELECT f.*
            FROM facts f
            JOIN entities e ON f.entity_id = e.id
            WHERE e.name = ?{status_filter}
            ORDER BY COALESCE(CAST(f.confidence AS REAL), 0.0) DESC, f.timestamp DESC
        """, (entity_name,))
        return [dict(row) for row in cur.fetchall()]

def get_all_facts(limit: int = 100, include_history: bool = False) -> list[dict[str, Any]]:
    init_kg_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        status_filter = "" if include_history else "WHERE COALESCE(f.status, 'active') = 'active'"
        cur.execute(f"""
            SELECT f.id as fact_id, e.id as entity_id, e.name as entity_name, e.entity_type, f.fact_text, f.timestamp, f.confidence, f.status, f.superseded_by
            FROM facts f
            JOIN entities e ON f.entity_id = e.id
            {status_filter}
            ORDER BY COALESCE(CAST(f.confidence AS REAL), 0.0) DESC, f.timestamp DESC LIMIT ?
        """, (limit,))
        return [dict(row) for row in cur.fetchall()]

def update_fact(fact_id: str, new_text: str) -> bool:
    """Epic 4, E4-03: edit one fact's text in place.

    Refuses (returns False) when the fact does not exist, the new
    text is empty, or another ACTIVE fact of the same entity already
    carries the normalised text (the edit would mint a duplicate).
    The fact keeps its id, entity, and status; its timestamp bumps
    so the edit is visible in recency orderings.
    """
    new_text = (new_text or "").strip()
    if not new_text:
        return False
    init_kg_db()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT f.entity_id, e.name FROM facts f JOIN entities e ON f.entity_id = e.id WHERE f.id = ?",
            (fact_id,),
        ).fetchone()
        if row is None:
            return False
        duplicate = _find_duplicate_fact_id(conn, row["name"], new_text)
        if duplicate is not None and duplicate != fact_id:
            logger.info("Refusing fact edit that would duplicate %s", duplicate)
            return False
        cur = conn.execute(
            "UPDATE facts SET fact_text = ?, timestamp = ? WHERE id = ?",
            (new_text, int(time.time()), fact_id),
        )
        conn.commit()
        changed = cur.rowcount > 0
    if changed:
        _audit_logger.log_event("KNOWLEDGE_FACT_UPDATED", {"fact_id": fact_id})
    return changed

def delete_fact(fact_id: str) -> bool:
    init_kg_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        cur.execute("DELETE FROM entities WHERE id NOT IN (SELECT DISTINCT entity_id FROM facts)")
        conn.commit()
    _audit_logger.log_event("KNOWLEDGE_FACT_DELETED", {"fact_id": fact_id})
    return True

def delete_entity(entity_name: str) -> bool:
    init_kg_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM entities WHERE name = ?", (entity_name,))
        conn.commit()
    _audit_logger.log_event("KNOWLEDGE_ENTITY_DELETED", {"entity_name": entity_name})
    return True

def clear_all_facts() -> bool:
    init_kg_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM facts")
        cur.execute("DELETE FROM entities")
        conn.commit()
    _audit_logger.log_event("KNOWLEDGE_GRAPH_CLEARED_ALL", {})
    return True

def search_facts(query: str, limit: int = 10, include_history: bool = False) -> list[dict[str, Any]]:
    init_kg_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        search_term = f"%{query.strip()}%"
        status_filter = "" if include_history else " AND COALESCE(f.status, 'active') = 'active'"
        cur.execute(f"""
            SELECT f.id as fact_id, e.name as entity_name, e.entity_type, f.fact_text, f.timestamp, f.confidence, f.status, f.superseded_by
            FROM facts f
            JOIN entities e ON f.entity_id = e.id
            WHERE (e.name LIKE ? OR f.fact_text LIKE ?){status_filter}
            ORDER BY COALESCE(CAST(f.confidence AS REAL), 0.0) DESC, f.timestamp DESC LIMIT ?
        """, (search_term, search_term, limit))
        return [dict(row) for row in cur.fetchall()]

# ---------------------------------------------------------------------------
# Phase 10.3 A1: retention / pruning. Before this, the knowledge graph had
# no pruning, decay, or TTL — entities + facts grew without bound. Bounds
# come from UserConfig (config.py is the single source of the numbers);
# explicit args here override them, and a bound <= 0 disables pruning on
# that axis.
#
# Safety guard — never delete the only record of a fact that is still
# referenced: a fact that is the ONLY fact of its entity is never pruned,
# because deleting it would erase the entity's sole record (nothing else
# points at a fact, and facts only point outward via entity_id /
# source_task_id, so the entity's last fact is the one live reference).
# Kept facts are counted as kept_referenced. Entities left with no facts
# after pruning are removed, mirroring delete_fact().
# ---------------------------------------------------------------------------

def prune_facts(retention_days: int | None = None,
                max_facts: int | None = None,
                *, config: UserConfig | None = None,
                now: int | None = None) -> dict[str, int]:
    """Prune KG facts by age, then by count (oldest first).

    Age: facts with ``timestamp`` older than ``retention_days`` are
    removed, unless they are their entity's only fact. Count: if more
    than ``max_facts`` remain, the oldest beyond the cap are removed,
    with the same guard. Returns counts and logs them to the audit log
    (KNOWLEDGE_GRAPH_PRUNED).
    """
    cfg = config if config is not None else load_config()
    if retention_days is None:
        retention_days = cfg.kg_fact_retention_days
    if max_facts is None:
        max_facts = cfg.kg_max_facts
    now = int(time.time()) if now is None else int(now)

    init_kg_db()
    pruned = 0
    kept_ids = set()

    with _get_conn() as conn:
        cur = conn.cursor()
        # Snapshot of entities with exactly one fact, taken before any
        # delete: those facts are protected for this whole run.
        sole_entity_ids = {
            r[0] for r in cur.execute(
                "SELECT entity_id FROM facts GROUP BY entity_id HAVING COUNT(*) = 1"
            )
        }

        def _delete_if_unreferenced(fact_id: str, entity_id: str) -> bool:
            nonlocal pruned
            if entity_id in sole_entity_ids:
                kept_ids.add(fact_id)
                return False
            cur.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            pruned += 1
            return True

        if retention_days and retention_days > 0:
            cutoff = now - int(retention_days) * 86400
            rows = cur.execute(
                "SELECT id, entity_id FROM facts WHERE timestamp < ?",
                (cutoff,),
            ).fetchall()
            for fact_id, entity_id in rows:
                _delete_if_unreferenced(fact_id, entity_id)

        if max_facts and max_facts > 0:
            total = cur.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            excess = total - int(max_facts)
            if excess > 0:
                rows = cur.execute(
                    "SELECT id, entity_id FROM facts ORDER BY timestamp ASC"
                ).fetchall()
                for fact_id, entity_id in rows:
                    if excess <= 0:
                        break
                    if _delete_if_unreferenced(fact_id, entity_id):
                        excess -= 1

        cur.execute("DELETE FROM entities WHERE id NOT IN (SELECT DISTINCT entity_id FROM facts)")
        pruned_entities = cur.rowcount
        conn.commit()

    result = {
        "pruned_facts": pruned,
        "kept_referenced": len(kept_ids),
        "pruned_entities": pruned_entities,
        "retention_days": retention_days,
        "max_facts": max_facts,
    }
    _audit_logger.log_event("KNOWLEDGE_GRAPH_PRUNED", result)
    logger.info("KNOWLEDGE_GRAPH_PRUNED %s", result)
    return result
