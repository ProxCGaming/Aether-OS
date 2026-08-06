"""Tests for task requirements inference and intelligent routing policy."""
import pytest

from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.policy import (
    RoutingPolicy,
    infer_task_requirements,
)
from aether_engine.routing.registry import ModelRegistry, ModelEntry


@pytest.fixture
def health_mgr():
    return ProviderHealthManager()


@pytest.fixture
def mock_registry():
    models = [
        ModelEntry(provider="google_gemini", id="gemini-2.0-flash", label="Gemini 2.0 Flash", capabilities=["chat", "fast", "tools", "vision"], priority=1),
        ModelEntry(provider="google_gemini", id="gemini-2.5-flash", label="Gemini 2.5 Flash", capabilities=["chat", "fast", "tools", "vision", "code"], priority=2),
        ModelEntry(provider="google_gemini", id="gemini-2.5-pro", label="Gemini 2.5 Pro", capabilities=["chat", "tools", "vision", "code", "reasoning"], priority=3),
        ModelEntry(provider="openai", id="gpt-4o-mini", label="GPT-4o Mini", capabilities=["chat", "fast", "tools", "vision"], priority=1),
        ModelEntry(provider="openai", id="gpt-4o", label="GPT-4o", capabilities=["chat", "tools", "vision", "code", "reasoning"], priority=2),
    ]
    return ModelRegistry(models)


class TestRoutingPolicy:
    def test_infer_task_requirements_general(self):
        reqs = infer_task_requirements("Hello, what is the weather like today?")
        assert not reqs.wants_code
        assert not reqs.wants_reasoning
        assert not reqs.has_images
        assert reqs.low_latency

    def test_infer_task_requirements_code(self):
        reqs = infer_task_requirements("Write a python function def calculate_fibonacci(n):")
        assert reqs.wants_code
        assert not reqs.wants_reasoning

    def test_infer_task_requirements_reasoning(self):
        reqs = infer_task_requirements("Think step by step and solve this complex logic riddle.")
        assert reqs.wants_reasoning
        assert not reqs.low_latency

    def test_infer_task_requirements_with_images(self):
        reqs = infer_task_requirements("What is shown in this picture?", images=["sample.png"])
        assert reqs.has_images

    def test_select_route_default_chat(self, mock_registry, health_mgr):
        policy = RoutingPolicy(mock_registry)
        decision = policy.select_route(
            prompt="Hello there!",
            configured_providers=["google_gemini", "openai"],
            health_manager=health_mgr,
            user_default_provider="google_gemini",
            user_default_model="gemini-2.0-flash",
        )
        assert decision.provider == "google_gemini"
        assert decision.model == "gemini-2.0-flash"
        assert "Gemini 2.0 Flash" in decision.reason

    def test_select_route_code_task(self, mock_registry, health_mgr):
        policy = RoutingPolicy(mock_registry)
        decision = policy.select_route(
            prompt="Write a python script to parse json logs",
            configured_providers=["google_gemini"],
            health_manager=health_mgr,
            user_default_provider="google_gemini",
            user_default_model="gemini-2.0-flash",
        )
        assert decision.provider == "google_gemini"
        assert decision.model == "gemini-2.5-flash"
        assert "code" in decision.capabilities

    def test_select_route_reasoning_task(self, mock_registry, health_mgr):
        policy = RoutingPolicy(mock_registry)
        decision = policy.select_route(
            prompt="Prove step by step that the square root of 2 is irrational",
            configured_providers=["google_gemini"],
            health_manager=health_mgr,
            user_default_provider="google_gemini",
            user_default_model="gemini-2.0-flash",
        )
        assert decision.provider == "google_gemini"
        assert decision.model == "gemini-2.5-pro"
        assert "reasoning" in decision.capabilities

    def test_select_route_fallback_when_default_offline(self, mock_registry, health_mgr):
        policy = RoutingPolicy(mock_registry)
        health_mgr.record_failure("google_gemini", "Network error", is_retriable=False)
        assert not health_mgr.is_provider_available("google_gemini")

        decision = policy.select_route(
            prompt="Hello!",
            configured_providers=["google_gemini", "openai"],
            health_manager=health_mgr,
            user_default_provider="google_gemini",
            user_default_model="gemini-2.0-flash",
        )
        assert decision.provider == "openai"
        assert decision.model == "gpt-4o-mini"
        assert "fallback from google_gemini" in decision.reason

    def test_select_route_explicit_user_model(self, mock_registry, health_mgr):
        policy = RoutingPolicy(mock_registry)
        decision = policy.select_route(
            prompt="Write hello world",
            configured_providers=["google_gemini"],
            health_manager=health_mgr,
            requested_model="gemini-2.5-pro",
        )
        assert decision.model == "gemini-2.5-pro"
        assert "User selected" in decision.reason
