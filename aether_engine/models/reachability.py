"""Model reachability verification and caching.

Performs fast, cheap, binary reachability checks using 1-token completion calls
to verify that models in a provider's catalog are actually callable with the user's
specific API key/tier before displaying them in UI dropdowns or capability jobs.
"""
import asyncio
import logging
from pathlib import Path
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("aether_engine.models.reachability")

DEFAULT_REACHABILITY_DB = Path.home() / ".aether" / "reachability.db"
DEFAULT_CACHE_TTL_SECONDS = 86400  # 24 hours


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

    def get_cached_reachability(self, provider: str, model_id: str) -> Optional[Tuple[bool, float]]:
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
        latency_ms: Optional[float] = None,
        error_detail: Optional[str] = None,
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
        base_url: Optional[str] = None,
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
        except Exception as e:
            latency_ms = (time.time() - start) * 1000.0
            err_msg = str(e)
            self.record_reachability(cache_key, model_id, reachable=False, latency_ms=latency_ms, error_detail=err_msg)
            logger.warning(f"Model unreachable: {cache_key}/{model_id} — {err_msg}")
            return False

    async def filter_reachable_models(
        self,
        provider_name: str,
        model_ids: List[str],
        api_key: str,
        base_url: Optional[str] = None,
        force: bool = False,
        max_concurrency: int = 5,
        timeout: float = 4.0,
    ) -> List[str]:
        """Sweep candidate models with bounded concurrency and return only reachable ones."""
        if not model_ids or not api_key:
            return model_ids

        semaphore = asyncio.Semaphore(max_concurrency)

        async def _probe(mid: str) -> Tuple[str, bool]:
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
        return reachable_models if reachable_models else model_ids


# Global reachability checker singleton
GLOBAL_REACHABILITY_CHECKER = ModelReachabilityChecker()
