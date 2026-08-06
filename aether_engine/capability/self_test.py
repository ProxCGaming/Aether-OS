"""Option C: Live self-test suite runner for reachable models via LiteLLM."""
import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from aether_engine.capability.test_suite import SELF_TEST_SUITE, grade_test
from aether_engine.providers.litellm_provider import LiteLLMProvider

logger = logging.getLogger("aether_engine.capability.self_test")


async def run_model_self_test(
    provider_name: str,
    model_id: str,
    api_key: str,
    timeout_per_test: float = 8.0,
) -> Dict[str, Any]:
    """Execute the self-test eval suite against a single reachable model."""
    test_results = {}
    latencies = []
    category_scores: Dict[str, List[bool]] = {}

    prov = LiteLLMProvider(api_key=api_key, model=model_id, provider_name=provider_name)

    for test in SELF_TEST_SUITE:
        start = time.time()
        response_text = ""
        passed = False
        error_msg = None

        try:
            async def _run():
                text = ""
                async for chunk in prov.call_stream([{"role": "user", "content": test.prompt}]):
                    if chunk.text:
                        text += chunk.text
                return text

            response_text = await asyncio.wait_for(_run(), timeout=timeout_per_test)
            latency_ms = (time.time() - start) * 1000.0
            latencies.append(latency_ms)

            graded = grade_test(test, response_text)
            passed = graded["passed"]
            test_results[test.name] = {
                "passed": passed,
                "latency_ms": round(latency_ms, 2),
                "response_preview": graded["response_preview"],
            }
        except Exception as e:
            latency_ms = (time.time() - start) * 1000.0
            error_msg = str(e)
            test_results[test.name] = {
                "passed": False,
                "latency_ms": round(latency_ms, 2),
                "error": error_msg[:200],
            }

        if test.category not in category_scores:
            category_scores[test.category] = []
        category_scores[test.category].append(passed)

    total_tests = len(SELF_TEST_SUITE)
    total_passed = sum(1 for res in test_results.values() if res.get("passed"))
    pass_rate = (total_passed / total_tests) if total_tests > 0 else 0.0
    avg_latency = (sum(latencies) / len(latencies)) if latencies else 0.0

    category_summary = {
        cat: (sum(1 for p in bools) / len(bools)) if bools else 0.0
        for cat, bools in category_scores.items()
    }

    return {
        "model_id": model_id,
        "provider": provider_name,
        "total_tests": total_tests,
        "passed_tests": total_passed,
        "pass_rate": round(pass_rate, 3),
        "avg_latency_ms": round(avg_latency, 2),
        "category_scores": category_summary,
        "details": test_results,
        "tested_at": time.time(),
    }


async def run_suite_on_reachable_models(
    model_pairs: List[Tuple[str, str, str]],  # (provider_name, model_id, api_key)
    max_concurrency: int = 3,
) -> Dict[str, Dict[str, Any]]:
    """Run Option C self-tests across multiple reachable models with concurrency control."""
    semaphore = asyncio.Semaphore(max_concurrency)
    results: Dict[str, Dict[str, Any]] = {}

    async def _test_one(prov: str, mid: str, key: str):
        async with semaphore:
            logger.info(f"Option C: Testing {prov}/{mid}...")
            res = await run_model_self_test(prov, mid, key)
            results[mid] = res

    tasks = [_test_one(p, m, k) for p, m, k in model_pairs]
    await asyncio.gather(*tasks, return_exceptions=True)
    return results
