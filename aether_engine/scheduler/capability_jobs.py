"""APScheduler job management for periodic capability checks with real A+C classification.

Option A: Static benchmark data cache for known cloud models.
Option C: Live self-test suite against each configured model via LiteLLM.
"""
import asyncio
import json
import logging
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    HAS_APSCHEDULER = True
except ImportError:
    AsyncIOScheduler = None
    CronTrigger = None
    DateTrigger = None
    IntervalTrigger = None
    HAS_APSCHEDULER = False

logger = logging.getLogger("aether_engine.scheduler")

DEFAULT_DB_PATH = Path.home() / ".aether" / "capabilities.db"


# ---------------------------------------------------------------------------
# Option A: Static benchmark data for known cloud models
# ---------------------------------------------------------------------------
# Scores are normalized 0-100 from public benchmarks (MMLU, HumanEval, MATH, etc.)
# Sources: model cards, published evals as of mid-2025. Updated periodically.
BENCHMARK_CACHE: dict[str, dict[str, Any]] = {
    # Google Gemini
    "gemini-2.0-flash": {
        "mmlu": 84.0, "humaneval": 76.0, "math": 82.0,
        "capabilities": ["chat", "fast", "tools", "vision"],
        "tier": "fast", "cost_per_1m_input": 0.10, "cost_per_1m_output": 0.40,
    },
    "gemini-2.5-flash": {
        "mmlu": 85.0, "humaneval": 78.0, "math": 83.0,
        "capabilities": ["chat", "fast", "tools", "vision", "code"],
        "tier": "balanced", "cost_per_1m_input": 0.15, "cost_per_1m_output": 0.60,
    },
    "gemini-2.5-pro": {
        "mmlu": 90.0, "humaneval": 84.0, "math": 90.0,
        "capabilities": ["chat", "tools", "vision", "code", "reasoning"],
        "tier": "premium", "cost_per_1m_input": 1.25, "cost_per_1m_output": 10.0,
    },
    # OpenAI
    "gpt-4o-mini": {
        "mmlu": 82.0, "humaneval": 72.0, "math": 74.0,
        "capabilities": ["chat", "fast", "tools", "vision"],
        "tier": "fast", "cost_per_1m_input": 0.15, "cost_per_1m_output": 0.60,
    },
    "gpt-4o": {
        "mmlu": 88.0, "humaneval": 90.0, "math": 87.0,
        "capabilities": ["chat", "tools", "vision", "code", "reasoning"],
        "tier": "premium", "cost_per_1m_input": 2.50, "cost_per_1m_output": 10.0,
    },
    # Anthropic
    "claude-3-5-haiku": {
        "mmlu": 80.0, "humaneval": 75.0, "math": 73.0,
        "capabilities": ["chat", "fast", "tools", "vision"],
        "tier": "fast", "cost_per_1m_input": 0.25, "cost_per_1m_output": 1.25,
    },
    "claude-3-5-sonnet": {
        "mmlu": 89.0, "humaneval": 92.0, "math": 88.0,
        "capabilities": ["chat", "tools", "vision", "code", "reasoning"],
        "tier": "premium", "cost_per_1m_input": 3.00, "cost_per_1m_output": 15.0,
    },
    # DeepSeek
    "deepseek-chat": {
        "mmlu": 79.0, "humaneval": 80.0, "math": 75.0,
        "capabilities": ["chat", "fast", "code"],
        "tier": "fast", "cost_per_1m_input": 0.14, "cost_per_1m_output": 0.28,
    },
    "deepseek-reasoner": {
        "mmlu": 86.0, "humaneval": 85.0, "math": 91.0,
        "capabilities": ["chat", "reasoning", "code"],
        "tier": "premium", "cost_per_1m_input": 0.55, "cost_per_1m_output": 2.19,
    },
}


# ---------------------------------------------------------------------------
# Option C: Live self-test prompts
# ---------------------------------------------------------------------------
_SELF_TEST_PROMPTS = [
    {
        "name": "reasoning",
        "prompt": "What is 17 * 23? Answer with ONLY the number.",
        "expected_contains": "391",
        "category": "reasoning",
    },
    {
        "name": "code",
        "prompt": "Write a Python function `add(a, b)` that returns the sum. Answer with ONLY the code, no explanation.",
        "expected_contains": "def add",
        "category": "code",
    },
    {
        "name": "instruction_following",
        "prompt": "Reply with exactly the word 'PONG' and nothing else.",
        "expected_contains": "PONG",
        "category": "chat",
    },
]


class CapabilitySchedulerManager:
    """Manages APScheduler background jobs and SQLite persistence for capability checks."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.scheduler: AsyncIOScheduler | None = None
        # Defaults are assigned BEFORE _init_db() restores the persisted
        # config, so a saved schedule/method wins on reopen and the
        # defaults apply only when nothing was ever saved (E5-F10).
        self.current_schedule = "weekly"  # default: weekly
        self.current_method = "Both"       # default: Option A + Option C
        # Set when the active schedule string cannot be honoured, so the
        # refusal is visible in the job state (get_status_summary).
        self.schedule_error: str | None = None
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS capability_check_runs (
                    id TEXT PRIMARY KEY,
                    method TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL NOT NULL,
                    models_tested INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    cost_estimate REAL NOT NULL,
                    error_log TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scheduler_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS model_capabilities (
                    model_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    benchmark_data TEXT,
                    selftest_results TEXT,
                    capabilities TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    last_tested_at REAL,
                    is_reachable INTEGER DEFAULT 1
                )
            """)
            conn.commit()

        # Restore saved config
        saved_sched = self._get_config("schedule")
        if saved_sched:
            self.current_schedule = saved_sched
        saved_method = self._get_config("method")
        if saved_method:
            self.current_method = saved_method

    def _get_config(self, key: str) -> str | None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM scheduler_config WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else None

    def _set_config(self, key: str, value: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO scheduler_config (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()

    def start(self):
        if not HAS_APSCHEDULER:
            logger.info("APScheduler not installed; capability background scheduling disabled.")
            return

        if not self.scheduler:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            try:
                self.scheduler = AsyncIOScheduler(event_loop=loop)
                self.scheduler.start()
                self._update_job_schedule(self.current_schedule)
                self._register_memory_prune_job()
                self._register_memory_consolidation_job()
                logger.info("CapabilitySchedulerManager started.")
            except Exception as e:  # noqa: BLE001 — scheduler start is deferred to the loop's first tick if no loop is running yet
                logger.warning(f"AsyncIOScheduler start deferred (no running event loop yet): {e}")

    def stop(self):
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None
            logger.info("CapabilitySchedulerManager stopped.")

    def _last_run_started_at(self) -> float | None:
        """Epoch of the most recent recorded run, or None if never run."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(started_at) FROM capability_check_runs"
            ).fetchone()
            return row[0] if row else None

    def _build_trigger(self, interval: str):
        """Build the APScheduler trigger for a schedule string.

        Named intervals are anchored at the last recorded run so their
        occurrences stay aligned to the schedule itself across restarts
        (last_run + k * period) instead of restarting a full period from
        every startup. Anything else must be a standard 5-field cron
        expression and registers a real CronTrigger. An unparseable
        string raises ValueError — callers refuse it loudly rather than
        silently degrading it to some default interval (E5-F10).
        """
        last_ts = self._last_run_started_at()
        anchor = (
            datetime.fromtimestamp(last_ts, tz=UTC)
            if last_ts is not None
            else None
        )
        if interval == "daily":
            return IntervalTrigger(days=1, start_date=anchor)
        if interval == "weekly":
            return IntervalTrigger(weeks=1, start_date=anchor)
        if interval == "monthly":
            return IntervalTrigger(weeks=4, start_date=anchor)
        return CronTrigger.from_crontab(interval)

    def _recover_missed_run(self, trigger):
        """Startup misfire recovery (ADR 0007, E5-F10).

        Rule: if at least one scheduled occurrence fell due after the
        last recorded run while the app was closed, execute exactly ONE
        catch-up run at startup — never a storm of backdated runs, no
        matter how many periods were missed. A job whose next occurrence
        is still in the future recovers nothing. The periodic job's own
        next fire is computed from the schedule's occurrences (see
        _build_trigger), so recovery never pushes it a period away.
        """
        last_ts = self._last_run_started_at()
        if last_ts is None:
            return
        now = datetime.now(UTC)
        last_dt = datetime.fromtimestamp(last_ts, tz=UTC)
        try:
            missed_at = trigger.get_next_fire_time(last_dt, now)
        except Exception:  # recovery must never block startup
            logger.exception("Capability check misfire check failed")
            return
        if missed_at is None or missed_at > now:
            return

        job = self.scheduler.get_job("capability_check_periodic")
        if job is not None and job.next_run_time is not None and job.next_run_time <= now:
            # The periodic job itself is already due; APScheduler's
            # misfire handling (grace time None) fires it once — that
            # firing IS the catch-up, so don't schedule a second one.
            return

        self.scheduler.add_job(
            self._execute_check_job,
            trigger=DateTrigger(run_date=now),
            id="capability_check_catchup",
            misfire_grace_time=None,
            replace_existing=True,
        )
        logger.info(
            "Capability check run was due at %s while the app was closed; "
            "scheduled one startup catch-up run.",
            missed_at.isoformat(),
        )

    def _update_job_schedule(self, interval: str):
        if not self.scheduler or not self.scheduler.running:
            return

        # Remove existing jobs if any (a re-schedule also cancels a
        # catch-up that has not fired yet; _recover_missed_run below
        # re-evaluates and re-adds one if a run is still due).
        for job_id in ("capability_check_periodic", "capability_check_catchup"):
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)

        if interval == "none":
            self.schedule_error = None
            return

        try:
            trigger = self._build_trigger(interval)
        except ValueError as e:
            # Refuse loudly: no job runs under a false schedule, and the
            # error is part of the job state for the settings surface.
            self.schedule_error = f"Unrecognised schedule '{interval}': {e}"
            logger.error(
                f"Refusing capability check schedule '{interval}': {e}. "
                "No periodic job registered."
            )
            return
        self.schedule_error = None

        # misfire_grace_time=None ensures missed jobs run immediately on startup
        self.scheduler.add_job(
            self._execute_check_job,
            trigger=trigger,
            id="capability_check_periodic",
            misfire_grace_time=None,
            replace_existing=True,
        )
        logger.info(f"Updated capability check schedule to '{interval}' with misfire recovery.")
        self._recover_missed_run(trigger)

    def _register_memory_prune_job(self):
        """Phase 10.3 A1: weekly retention pruning for all memory tiers.

        This manager is the engine's only background scheduler, so the
        prune job lives here next to the capability-check job — same
        pattern, its own fixed weekly trigger, independent of the
        user-set capability-check schedule. Deliberately NOT run at
        startup: a startup prune would fire inside EngineState.initialize(),
        which tests also call, and would prune the live ~/.aether DBs.
        A weekly interval trigger only fires on schedule, never during a
        test run. Manual trigger: run_memory_prune_now().
        """
        if not HAS_APSCHEDULER or not self.scheduler or not self.scheduler.running:
            return
        self.scheduler.add_job(
            self._run_memory_prune_job,
            trigger=IntervalTrigger(weeks=1),
            id="memory_prune_weekly",
            replace_existing=True,
        )
        logger.info("Registered weekly memory pruning job.")

    def _run_memory_prune_job(self) -> dict[str, Any]:
        """Execute one memory-pruning run (sync job; APScheduler runs it
        in the default executor). Returns per-tier counts."""
        from aether_engine.config import load_config
        from aether_engine.memory.pruning import prune_all

        try:
            results = prune_all(config=load_config())
            logger.info(f"Memory pruning complete: {results}")
            return results
        except Exception as e:
            logger.exception("Memory pruning failed")
            return {"error": str(e)}

    def run_memory_prune_now(self) -> dict[str, Any]:
        """Manual trigger for memory pruning (mirrors run_now for
        capability checks)."""
        return self._run_memory_prune_job()

    def _register_memory_consolidation_job(self):
        """Epic 4, E4-10 (addendum omp #33): weekly consolidation of
        the episodic/KG stores into the curated memory document.

        Registered next to the prune job, same pattern and cadence —
        and deliberately NOT run at startup, for the same reason as
        pruning: a startup run would fire inside
        EngineState.initialize(), which tests also call, and would
        rewrite the live ~/.aether document during test runs. A
        weekly interval trigger only fires on schedule. Manual
        trigger: run_memory_consolidation_now().
        """
        if not HAS_APSCHEDULER or not self.scheduler or not self.scheduler.running:
            return
        self.scheduler.add_job(
            self._run_memory_consolidation_job,
            trigger=IntervalTrigger(weeks=1),
            id="memory_consolidation_weekly",
            replace_existing=True,
        )
        logger.info("Registered weekly memory consolidation job.")

    def _run_memory_consolidation_job(self) -> dict[str, Any]:
        """Execute one consolidation pass (sync job; APScheduler runs
        it in the default executor). Returns the pass result."""
        from aether_engine.config import load_config
        from aether_engine.memory.consolidation import consolidate_memory_document

        try:
            result = consolidate_memory_document(config=load_config())
            logger.info(f"Memory consolidation complete: {result}")
            return result
        except Exception as e:
            logger.exception("Memory consolidation failed")
            return {"error": str(e)}

    def run_memory_consolidation_now(self) -> dict[str, Any]:
        """Manual trigger for memory consolidation (mirrors
        run_memory_prune_now for capability housekeeping)."""
        return self._run_memory_consolidation_job()

    async def _execute_check_job(self, method: str | None = None) -> dict[str, Any]:
        """Execute a capability check run using Option A (benchmarks), C (self-tests), or Both."""
        run_method = method or self.current_method
        run_id = str(uuid.uuid4())
        started_at = time.time()
        models_tested = 0
        errors: list[str] = []

        logger.info(f"Running capability check job '{run_id}' using method '{run_method}'...")

        try:
            if run_method in ("Option A", "Both"):
                count = self._run_option_a()
                models_tested += count

            if run_method in ("Option C", "Both"):
                count = await self._run_option_c()
                models_tested += count

            status_str = "SUCCESS"
        except Exception as e:
            logger.exception("Capability check failed")
            errors.append(str(e))
            status_str = "FAILED"

        finished_at = time.time()
        # Estimate cost: ~5 tokens in + 10 tokens out per test, 3 tests per model
        cost_estimate = models_tested * 3 * 0.002  # rough sub-cent estimate

        record = {
            "id": run_id,
            "method": run_method,
            "started_at": started_at,
            "finished_at": finished_at,
            "models_tested": models_tested,
            "status": status_str,
            "cost_estimate": round(cost_estimate, 4),
            "error_log": "; ".join(errors) if errors else None,
        }

        self._record_run(record)
        return record

    def _run_option_a(self) -> int:
        """Option A: Load static benchmark cache into model_capabilities table."""
        from aether_engine.capability.benchmark_cache import BENCHMARK_CACHE
        from aether_engine.capability.classifier import classify_model_capabilities

        count = 0
        with sqlite3.connect(self.db_path) as conn:
            for model_id, bench in BENCHMARK_CACHE.items():
                classification = classify_model_capabilities(model_id, benchmark_data=bench)
                caps = json.dumps(classification["capabilities"])
                tier = classification["tier"]
                bench_json = json.dumps({
                    k: v for k, v in bench.items()
                    if k not in ("capabilities", "tier")
                })
                conn.execute("""
                    INSERT OR REPLACE INTO model_capabilities
                    (model_id, provider, benchmark_data, capabilities, tier, last_tested_at, is_reachable)
                    VALUES (?, ?, ?, ?, ?, ?, COALESCE(
                        (SELECT is_reachable FROM model_capabilities WHERE model_id = ?), 1
                    ))
                """, (
                    model_id,
                    _infer_provider(model_id),
                    bench_json,
                    caps,
                    tier,
                    time.time(),
                    model_id,
                ))
                count += 1
            conn.commit()
        logger.info(f"Option A: Loaded benchmark data for {count} models.")
        return count

    async def _run_option_c(self) -> int:
        """Option C: Run live self-test suite against reachable models via LiteLLM."""
        from aether_engine.capability.classifier import classify_model_capabilities
        from aether_engine.capability.self_test import run_model_self_test
        from aether_engine.config import CLOUD_PROVIDERS, load_config
        from aether_engine.models.reachability import GLOBAL_REACHABILITY_CHECKER
        from aether_engine.providers.discovery import fetch_available_models
        from aether_engine.secrets.storage import SecretStore

        secret_store = SecretStore()
        stored_providers = set(secret_store.list_providers())

        if not stored_providers:
            logger.info("Option C: No configured providers with API keys — skipping self-tests.")
            return 0

        # Build list of (provider, model, key) pairs to test — strictly filter for reachable models
        test_tuples: list[tuple] = []
        cfg = load_config()

        for p in CLOUD_PROVIDERS:
            if p.name in stored_providers:
                try:
                    key = secret_store.load_provider(p.name)
                    if not key:
                        continue

                    # 1. Dynamically discover models from provider API
                    disc = await fetch_available_models(
                        provider_name=p.name,
                        api_key=key,
                        filter_reachability=True,
                        force_reachability=False,
                    )
                    models_to_test = disc.models

                    # 2. Fallback to persisted provider_models from config
                    if not models_to_test:
                        stored_models = cfg.provider_models.get(p.name, [])
                        if stored_models:
                            models_to_test = await GLOBAL_REACHABILITY_CHECKER.filter_reachable_models(
                                provider_name=p.name,
                                model_ids=stored_models,
                                api_key=key,
                                force=False,
                            )

                    # 3. Fallback to model_capabilities database for known models
                    if not models_to_test:
                        with sqlite3.connect(self.db_path) as conn:
                            db_models = [
                                r[0] for r in conn.execute(
                                    "SELECT model_id FROM model_capabilities WHERE provider = ?",
                                    (p.name,)
                                ).fetchall()
                            ]
                            if db_models:
                                models_to_test = await GLOBAL_REACHABILITY_CHECKER.filter_reachable_models(
                                    provider_name=p.name,
                                    model_ids=db_models,
                                    api_key=key,
                                    force=False,
                                )

                    for model_id in models_to_test:
                        test_tuples.append((p.name, model_id, key))
                except Exception as e:  # noqa: BLE001 — per-provider isolation in the capability sweep
                    logger.warning(f"Option C: Error preparing tests for {p.name}: {e}")

        count = 0
        for provider_name, model_id, api_key in test_tuples:
            try:
                st_result = await run_model_self_test(
                    provider_name=provider_name,
                    model_id=model_id,
                    api_key=api_key,
                )
                is_reachable = st_result.get("pass_rate", 0) > 0 or not st_result.get("details", {}).get("reasoning", {}).get("error")

                # Merge with existing benchmark data if available
                with sqlite3.connect(self.db_path) as conn:
                    existing = conn.execute(
                        "SELECT benchmark_data FROM model_capabilities WHERE model_id = ?",
                        (model_id,)
                    ).fetchone()
                    existing_bench = json.loads(existing[0]) if existing and existing[0] else None

                    classification = classify_model_capabilities(
                        model_id,
                        benchmark_data=existing_bench,
                        selftest_data=st_result,
                    )

                    conn.execute("""
                        INSERT OR REPLACE INTO model_capabilities
                        (model_id, provider, benchmark_data, selftest_results, capabilities, tier, last_tested_at, is_reachable)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        model_id,
                        provider_name,
                        json.dumps(existing_bench) if existing_bench else None,
                        json.dumps(st_result),
                        json.dumps(classification["capabilities"]),
                        classification["tier"],
                        time.time(),
                        1 if is_reachable else 0,
                    ))
                    conn.commit()

                count += 1
                logger.info(f"Option C: Tested {provider_name}/{model_id} — pass_rate={st_result.get('pass_rate')}")
            except Exception as e:  # noqa: BLE001 — per-model isolation in the capability sweep
                logger.error(f"Option C: Test failed for {provider_name}/{model_id}: {e}")

        return count

    def _record_run(self, record: dict[str, Any]):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO capability_check_runs (id, method, started_at, finished_at, models_tested, status, cost_estimate, error_log)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"],
                    record["method"],
                    record["started_at"],
                    record["finished_at"],
                    record["models_tested"],
                    record["status"],
                    record["cost_estimate"],
                    record.get("error_log"),
                ),
            )
            conn.commit()

    async def run_now(self, method: str | None = None) -> dict[str, Any]:
        """Manual trigger ('Run Now' button)."""
        return await self._execute_check_job(method=method)

    def set_schedule(self, interval: str, method: str):
        self.current_schedule = interval
        self.current_method = method
        self._set_config("schedule", interval)
        self._set_config("method", method)
        self._update_job_schedule(interval)

    def get_history(self, limit: int = 10) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM capability_check_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_status_summary(self) -> dict[str, Any]:
        history = self.get_history(limit=1)
        last_run = history[0] if history else None
        next_run_ts = None
        if self.scheduler and self.scheduler.running:
            job = self.scheduler.get_job("capability_check_periodic")
            if job and job.next_run_time:
                next_run_ts = job.next_run_time.timestamp()

        return {
            "schedule": self.current_schedule,
            "method": self.current_method,
            "schedule_error": self.schedule_error,
            "last_run": last_run,
            "next_run_ts": next_run_ts,
        }

    def get_model_capabilities(self, model_id: str) -> dict[str, Any] | None:
        """Retrieve cached capability data for a specific model."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM model_capabilities WHERE model_id = ?", (model_id,))
            row = cursor.fetchone()
            if row:
                result = dict(row)
                if result.get("benchmark_data"):
                    result["benchmark_data"] = json.loads(result["benchmark_data"])
                if result.get("selftest_results"):
                    result["selftest_results"] = json.loads(result["selftest_results"])
                if result.get("capabilities"):
                    result["capabilities"] = json.loads(result["capabilities"])
                return result
            return None


def _infer_provider(model_id: str) -> str:
    """Infer provider name from model ID string."""
    mid = model_id.lower()
    if mid.startswith("gemini"):
        return "google_gemini"
    elif mid.startswith(("gpt", "o1", "o3", "text-embedding")):
        return "openai"
    elif mid.startswith("claude"):
        return "anthropic"
    elif mid.startswith("deepseek"):
        return "deepseek"
    elif "/" in mid or mid.startswith(("qwen", "meta-llama", "mistral")):
        return "openrouter"
    return "unknown"
