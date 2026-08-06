import pytest
from unittest.mock import MagicMock
from aether_engine.routing.capability_router import CapabilityRouter, infer_capability_profile
from aether_engine.routing.registry import ModelRegistry, ModelEntry

def test_intent_classification():
    # Coding intent
    intent_code = infer_capability_profile("Write a Python function to compute fibonacci numbers.")
    assert intent_code.intent == "code"
    assert "code" in intent_code.required_tags

    # Reasoning / Math intent
    intent_reasoning = infer_capability_profile("Solve this step-by-step logic puzzle and calculate the probability.")
    assert intent_reasoning.intent == "reasoning"
    assert "reasoning" in intent_reasoning.required_tags

    # Vision intent
    intent_vision = infer_capability_profile("Describe what is shown in this screenshot.", has_images=True)
    assert intent_vision.intent == "vision"
    assert "vision" in intent_vision.required_tags

    # Simple chat intent
    intent_chat = infer_capability_profile("Hello! How are you today?")
    assert intent_chat.intent == "chat"
    assert "chat" in intent_chat.required_tags

def test_router_model_selection():
    registry = ModelRegistry([])
    registry.register_model(ModelEntry(provider="google_gemini", id="gemini-1.5-flash", label="Gemini 1.5 Flash", capabilities=["chat", "fast", "vision"]))
    registry.register_model(ModelEntry(provider="google_gemini", id="gemini-1.5-pro", label="Gemini 1.5 Pro", capabilities=["chat", "code", "reasoning", "vision"]))
    registry.register_model(ModelEntry(provider="openrouter", id="deepseek/deepseek-r1", label="DeepSeek R1", capabilities=["code", "reasoning"]))

    router = CapabilityRouter(registry=registry)

    health_mgr = MagicMock()
    health_mgr.is_provider_available.side_effect = lambda p: p == "google_gemini"

    # Coding task with reasoning should select gemini-1.5-pro because openrouter is not available
    decision = router.route_task(
        prompt="Solve this complex algorithmic proof in Rust step by step",
        configured_providers=["google_gemini", "openrouter"],
        health_manager=health_mgr,
        user_default_provider="google_gemini",
        user_default_model="gemini-1.5-flash",
    )
    assert decision["provider"] == "google_gemini"
    assert decision["model"] == "gemini-1.5-pro"

    # Fast chat query should select flash
    chat_decision = router.route_task(
        prompt="hi",
        configured_providers=["google_gemini"],
        health_manager=health_mgr,
        user_default_provider="google_gemini",
        user_default_model="gemini-1.5-flash",
    )
    assert chat_decision["provider"] == "google_gemini"
    assert chat_decision["model"] in ("gemini-1.5-flash", "gemini-1.5-pro")


