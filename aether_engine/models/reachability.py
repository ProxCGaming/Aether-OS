"""Model reachability verification and caching.

Performs fast, cheap, binary reachability checks using 1-token completion calls
to verify that models in a provider's catalog are actually callable with the user's
specific API key/tier before displaying them in UI dropdowns or capability jobs.
"""
import asyncio
import logging
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from aether_engine.providers.discovery import DiscoveryError

logger = logging.getLogger("aether_engine.models.reachability")

DEFAULT_REACHABILITY_DB = Path.home() / ".aether" / "reachability.db"
DEFAULT_CACHE_TTL_SECONDS = 86400  # 24 hours


class ReachableModels(list[str]):
    """Result of a reachability sweep: the reachable model ids, plus the sweep error.

    Subclasses ``list`` so existing callers keep working unchanged — empty
    means "nothing reachable", and truthiness/iteration behave like a plain
    list. ``error`` is a ``DiscoveryError`` (the discovery-v2 error shape:
    ``code`` + ``message``) and is set when the sweep could not run at all
    (no API key) or when it probed models but none was reachable. It is
    ``None`` when at least one model is reachable, or when there was
    nothing to probe (empty input).

    ``code`` is ``None`` here by the same convention as discovery-v2
    transport failures: a sweep has no single HTTP status — per-model
    failures vary and are recorded per model in the reachability DB — so
    the message carries the provider and the probed/reachable counts
    instead of a fabricated status code.
    """

    def __init__(
        self,
        models: Iterable[str] = (),
        error: DiscoveryError | None = None,
    ):
        super().__init__(models)
        self.error = error

    @property
    def ok(self) -> bool:
        """True when the sweep produced a usable result (mirrors DiscoveryResult.ok)."""
        return self.error is None


class ModelReachabilityChecker:
    """Probes model reachability via LiteLLM and caches results in SQLite."""

    def __init__(self, db_path: Path = DEFAULT_REACHABILITY_DB, cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS):
        self.db_path = Path(db_path)
        self.cache_ttl = cache_ttl
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS model_reachability (
                    provider TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    reachable INTEGER NOT NULL,
                    checked_at REAL NOT NULL,
                    latency_ms REAL,
                    error_detail TEXT,
                    PRIMARY KEY (provider, model_id)
                )
            """)
            conn.commit()

    def get_cached_reachability(self, provider: str, model_id: str) -> tuple[bool, float] | None:
        """Return (reachable, checked_at) if valid cache entry exists, else None."""
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT reachable, checked_at FROM model_reachability WHERE provider = ? AND model_id = ?",
                (provider, model_id),
            )
            row = cursor.fetchone()
            if row:
                reachable, checked_at = bool(row[0]), float(row[1])
                if (now - checked_at) < self.cache_ttl:
                    return reachable, checked_at
        return None

    def record_reachability(
        self,
        provider: str,
        model_id: str,
        reachable: bool,
        latency_ms: float | None = None,
        error_detail: str | None = None,
    ):
        """Persist reachability result to SQLite."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO model_reachability
                (provider, model_id, reachable, checked_at, latency_ms, error_detail)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                provider,
                model_id,
                1 if reachable else 0,
                time.time(),
                latency_ms,
                error_detail[:500] if error_detail else None,
            ))
            conn.commit()

    async def check_single_model(
        self,
        provider_name: str,
        model_id: str,
        api_key: str,
        base_url: str | None = None,
        force: bool = False,
        timeout: float = 4.0,
    ) -> bool:
        """Check if a single model responds to a 1-token probe request."""
        cache_key = f"{provider_name}@{base_url}" if base_url else provider_name
        if not force:
            cached = self.get_cached_reachability(cache_key, model_id)
            if cached is not None:
                return cached[0]

        from aether_engine.providers.litellm_provider import LiteLLMProvider

        start = time.time()
        try:
            prov = LiteLLMProvider(api_key=api_key, model=model_id, provider_name=provider_name, base_url=base_url)
            # Send a minimal 1-token completion probe
            async def _probe():
                async for chunk in prov.call_stream([{"role": "user", "content": "hi"}]):
                    return True
                return True

            await asyncio.wait_for(_probe(), timeout=timeout)
            latency_ms = (time.time() - start) * 1000.0
            self.record_reachability(cache_key, model_id, reachable=True, latency_ms=latency_ms)
            logger.info(f"Model reachable: {cache_key}/{model_id} ({latency_ms:.1f}ms)")
            return True
        except Exception as e:  # noqa: BLE001 — a failed probe records the model unreachable; probing never raises
            latency_ms = (time.time() - start) * 1000.0
            err_msg = str(e)
            self.record_reachability(cache_key, model_id, reachable=False, latency_ms=latency_ms, error_detail=err_msg)
            logger.warning(f"Model unreachable: {cache_key}/{model_id} — {err_msg}")
            return False

    async def filter_reachable_models(
        self,
        provider_name: str,
        model_ids: list[str],
        api_key: str,
        base_url: str | None = None,
        force: bool = False,
        max_concurrency: int = 5,
        timeout: float = 4.0,
    ) -> ReachableModels:
        """Sweep candidate models with bounded concurrency and return only reachable ones.

        Returns a ``ReachableModels`` list. When nothing is reachable the
        list is empty and ``.error`` carries a real ``DiscoveryError``
        (discovery-v2 shape) — the unfiltered input list is never returned
        as a silent success. Callers must treat an empty result as "no
        models available", not as "all models available".
        """
        from aether_engine.providers.discovery import DiscoveryError

        if not model_ids:
            return ReachableModels()
        if not api_key:
            err = DiscoveryError(
                code=None,
                message=f"No API key for provider '{provider_name}': cannot probe model reachability",
            )
            logger.warning(f"Reachability sweep for {provider_name}: {err.message}")
            return ReachableModels(error=err)

        semaphore = asyncio.Semaphore(max_concurrency)

        async def _probe(mid: str) -> tuple[str, bool]:
            async with semaphore:
                reachable = await self.check_single_model(
                    provider_name=provider_name,
                    model_id=mid,
                    api_key=api_key,
                    base_url=base_url,
                    force=force,
                    timeout=timeout,
                )
                return mid, reachable

        tasks = [_probe(m) for m in model_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        reachable_models = []
        for r in results:
            if isinstance(r, tuple):
                mid, is_ok = r
                if is_ok:
                    reachable_models.append(mid)

        logger.info(
            f"Reachability sweep complete for {provider_name}: "
            f"{len(reachable_models)}/{len(model_ids)} reachable."
        )
        if reachable_models:
            return ReachableModels(reachable_models)
        # Phase 10.3 B2: never mask total failure by returning the full
        # input list. Empty list + a real error; the caller must handle
        # the "nothing reachable" state explicitly.
        err = DiscoveryError(
            code=None,
            message=(
                f"No reachable models for provider '{provider_name}': "
                f"probed {len(model_ids)}, 0 reachable"
            ),
        )
        logger.warning(f"Reachability sweep for {provider_name}: {err.message}")
        return ReachableModels(error=err)


# Global reachability checker singleton
GLOBAL_REACHABILITY_CHECKER = ModelReachabilityChecker()
