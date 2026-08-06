"""Option A: Curated public benchmark cache and model tier mappings.

Stores published benchmark scores (MMLU, HumanEval, MATH), speed ratings,
and standard capability tags for common LLM model architectures.
"""
from typing import Any, Dict, List, Optional

BENCHMARK_CACHE: Dict[str, Dict[str, Any]] = {
    # Google Gemini
    "gemini-2.0-flash": {
        "mmlu": 84.5,
        "humaneval": 82.0,
        "math": 70.2,
        "tier": "fast",
        "capabilities": ["chat", "fast", "tools", "vision"],
        "cost_input_1m": 0.10,
        "cost_output_1m": 0.40,
    },
    "gemini-2.0-flash-lite": {
        "mmlu": 79.8,
        "humaneval": 75.0,
        "math": 64.0,
        "tier": "fast",
        "capabilities": ["chat", "fast", "tools"],
        "cost_input_1m": 0.075,
        "cost_output_1m": 0.30,
    },
    "gemini-1.5-pro": {
        "mmlu": 85.9,
        "humaneval": 84.1,
        "math": 67.7,
        "tier": "premium",
        "capabilities": ["chat", "code", "reasoning", "tools", "vision"],
        "cost_input_1m": 1.25,
        "cost_output_1m": 5.00,
    },
    "gemini-1.5-flash": {
        "mmlu": 78.9,
        "humaneval": 74.3,
        "math": 55.5,
        "tier": "fast",
        "capabilities": ["chat", "fast", "tools", "vision"],
        "cost_input_1m": 0.075,
        "cost_output_1m": 0.30,
    },
    # OpenAI
    "gpt-4o": {
        "mmlu": 88.7,
        "humaneval": 90.2,
        "math": 76.6,
        "tier": "premium",
        "capabilities": ["chat", "code", "reasoning", "tools", "vision"],
        "cost_input_1m": 2.50,
        "cost_output_1m": 10.00,
    },
    "gpt-4o-mini": {
        "mmlu": 82.0,
        "humaneval": 87.0,
        "math": 70.2,
        "tier": "fast",
        "capabilities": ["chat", "code", "fast", "tools", "vision"],
        "cost_input_1m": 0.15,
        "cost_output_1m": 0.60,
    },
    "o1": {
        "mmlu": 91.8,
        "humaneval": 92.4,
        "math": 96.4,
        "tier": "premium",
        "capabilities": ["reasoning", "code", "chat"],
        "cost_input_1m": 15.00,
        "cost_output_1m": 60.00,
    },
    "o3-mini": {
        "mmlu": 86.9,
        "humaneval": 92.0,
        "math": 87.3,
        "tier": "balanced",
        "capabilities": ["reasoning", "code", "fast", "tools"],
        "cost_input_1m": 1.10,
        "cost_output_1m": 4.40,
    },
    # Anthropic
    "claude-3-7-sonnet-latest": {
        "mmlu": 89.2,
        "humaneval": 92.0,
        "math": 78.4,
        "tier": "premium",
        "capabilities": ["chat", "code", "reasoning", "tools", "vision"],
        "cost_input_1m": 3.00,
        "cost_output_1m": 15.00,
    },
    "claude-3-5-sonnet-latest": {
        "mmlu": 88.3,
        "humaneval": 92.0,
        "math": 71.1,
        "tier": "premium",
        "capabilities": ["chat", "code", "reasoning", "tools", "vision"],
        "cost_input_1m": 3.00,
        "cost_output_1m": 15.00,
    },
    "claude-3-5-haiku-latest": {
        "mmlu": 75.2,
        "humaneval": 75.9,
        "math": 52.8,
        "tier": "fast",
        "capabilities": ["chat", "fast", "tools"],
        "cost_input_1m": 0.80,
        "cost_output_1m": 4.00,
    },
    # DeepSeek
    "deepseek-chat": {
        "mmlu": 88.5,
        "humaneval": 89.0,
        "math": 75.5,
        "tier": "balanced",
        "capabilities": ["chat", "code", "tools"],
        "cost_input_1m": 0.14,
        "cost_output_1m": 0.28,
    },
    "deepseek-reasoner": {
        "mmlu": 90.8,
        "humaneval": 90.2,
        "math": 90.0,
        "tier": "premium",
        "capabilities": ["reasoning", "code", "chat"],
        "cost_input_1m": 0.55,
        "cost_output_1m": 2.19,
    },
    # Local Ollama Architectures (Static Estimations)
    "llama3.2:1b": {
        "mmlu": 49.3,
        "humaneval": 30.0,
        "math": 25.0,
        "tier": "fast",
        "capabilities": ["chat", "fast", "local"],
        "cost_input_1m": 0.0,
        "cost_output_1m": 0.0,
    },
    "llama3.2:3b": {
        "mmlu": 63.4,
        "humaneval": 45.0,
        "math": 38.0,
        "tier": "fast",
        "capabilities": ["chat", "fast", "local"],
        "cost_input_1m": 0.0,
        "cost_output_1m": 0.0,
    },
    "qwen2.5-coder:1.5b": {
        "mmlu": 55.0,
        "humaneval": 60.0,
        "math": 40.0,
        "tier": "fast",
        "capabilities": ["code", "fast", "local"],
        "cost_input_1m": 0.0,
        "cost_output_1m": 0.0,
    },
    "qwen2.5-coder:7b": {
        "mmlu": 72.0,
        "humaneval": 84.0,
        "math": 65.0,
        "tier": "balanced",
        "capabilities": ["code", "tools", "local"],
        "cost_input_1m": 0.0,
        "cost_output_1m": 0.0,
    },
    "deepseek-r1:1.5b": {
        "mmlu": 58.0,
        "humaneval": 50.0,
        "math": 60.0,
        "tier": "fast",
        "capabilities": ["reasoning", "fast", "local"],
        "cost_input_1m": 0.0,
        "cost_output_1m": 0.0,
    },
    "deepseek-r1:7b": {
        "mmlu": 75.0,
        "humaneval": 70.0,
        "math": 82.0,
        "tier": "balanced",
        "capabilities": ["reasoning", "code", "local"],
        "cost_input_1m": 0.0,
        "cost_output_1m": 0.0,
    },
}


def get_benchmark_for_model(model_id: str) -> Optional[Dict[str, Any]]:
    """Look up benchmark entry by exact match or normalized prefix match."""
    if model_id in BENCHMARK_CACHE:
        return dict(BENCHMARK_CACHE[model_id])

    # Strip provider prefixes e.g. "openrouter/openai/gpt-4o" -> "gpt-4o"
    clean_id = model_id.split("/")[-1].lower()
    if clean_id in BENCHMARK_CACHE:
        return dict(BENCHMARK_CACHE[clean_id])

    # Partial prefix match
    for k, v in BENCHMARK_CACHE.items():
        if clean_id.startswith(k) or k in clean_id:
            return dict(v)

    return None
