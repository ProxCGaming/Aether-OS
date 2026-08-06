"""Classification and capability synthesis engine.

Merges Option A (published benchmark data) and Option C (live self-test evals)
into authoritative capability tags and performance tier assignments.
"""
from typing import Any, Dict, List, Optional, Set

from aether_engine.capability.benchmark_cache import get_benchmark_for_model


def classify_model_capabilities(
    model_id: str,
    benchmark_data: Optional[Dict[str, Any]] = None,
    selftest_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Synthesize model capabilities and tier from benchmark and self-test evidence.

    Returns a dict with:
      - capabilities: List[str]
      - tier: str ("fast", "balanced", "premium")
      - confidence: float (0.0 to 1.0)
      - synthesized_summary: str
    """
    bench = benchmark_data or get_benchmark_for_model(model_id) or {}
    st = selftest_data or {}

    caps: Set[str] = {"chat"}  # All LLMs support chat
    tier = bench.get("tier", "balanced")
    confidence_sources = 0

    # 1. Process Option A benchmark data if available
    if bench:
        confidence_sources += 1
        bench_caps = bench.get("capabilities", [])
        caps.update(bench_caps)

        humaneval = bench.get("humaneval", 0.0)
        math_score = bench.get("math", 0.0)
        mmlu = bench.get("mmlu", 0.0)

        if humaneval >= 70.0:
            caps.add("code")
        if math_score >= 65.0 or mmlu >= 85.0:
            caps.add("reasoning")
        if bench.get("tier") == "fast":
            caps.add("fast")

    # 2. Process Option C self-test results if available
    if st and st.get("category_scores"):
        confidence_sources += 1
        cat_scores = st.get("category_scores", {})
        pass_rate = st.get("pass_rate", 0.0)
        avg_lat = st.get("avg_latency_ms", 9999.0)

        if cat_scores.get("code", 0.0) >= 0.75:
            caps.add("code")
        elif cat_scores.get("code", 0.0) < 0.25 and not bench:
            caps.discard("code")

        if cat_scores.get("reasoning", 0.0) >= 0.75:
            caps.add("reasoning")
        elif cat_scores.get("reasoning", 0.0) < 0.25 and not bench:
            caps.discard("reasoning")

        if avg_lat <= 2000.0 or pass_rate >= 0.75 and avg_lat <= 3000.0:
            caps.add("fast")

        # Refine tier based on live empirical performance
        if pass_rate >= 0.9 and (not bench or bench.get("tier") == "premium"):
            tier = "premium"
        elif avg_lat < 1500.0 and pass_rate >= 0.5:
            tier = "fast"
        elif pass_rate >= 0.5:
            tier = "balanced"

    # Compute classification confidence
    confidence = 0.95 if confidence_sources >= 2 else (0.75 if confidence_sources == 1 else 0.40)

    # Convert sorted list for deterministic serialization
    capabilities_list = sorted(list(caps))

    return {
        "model_id": model_id,
        "capabilities": capabilities_list,
        "tier": tier,
        "confidence": confidence,
        "has_benchmark": bool(bench),
        "has_selftest": bool(st),
    }
