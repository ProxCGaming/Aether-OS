"""Tests for E8-4.1: Snip flow (screen region -> message attachment).

Tests cover:
1. Message attachment payload, persistence in session_messages, and reload survival.
2. Vision model routing via configured auxiliary vision model (cloud or local Ollama).
3. Local model execution guarantees zero external network calls leave the machine.
4. WebSocket START_TASK flow with image attachments.
"""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aether_engine.config import UserConfig
from aether_engine.memory import session_store
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.policy import (
    RoutingPolicy,
    infer_task_requirements,
)
from aether_engine.routing.registry import GLOBAL_MODEL_REGISTRY


@pytest.fixture
def temp_db(monkeypatch):
    """Use a temporary database for session_store to isolate tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_memory.db"
        monkeypatch.setattr(session_store, "DB_PATH", db_path)
        monkeypatch.setattr(session_store, "_initialized", None)
        session_store.init_db()
        yield db_path


class TestAttachmentPersistence:
    """Verifies attachment payload storage and reload survival."""

    def test_add_message_with_attachments_and_reload(self, temp_db):
        sess_id = session_store.create_session("Test Vision Session")
        snip_data = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        attachments = [
            {
                "name": "snip.png",
                "mime_type": "image/png",
                "data": snip_data,
                "width": 100,
                "height": 80,
            }
        ]

        msg_id = session_store.add_message(
            session_id=sess_id,
            role="user",
            content="What is shown in this snip?",
            thoughts=None,
            message_id="msg-vision-1",
            attachments=attachments,
        )
        assert msg_id == "msg-vision-1"

        # First read
        sess = session_store.get_session(sess_id)
        assert sess is not None
        assert len(sess["messages"]) == 1
        saved_msg = sess["messages"][0]
        assert saved_msg["content"] == "What is shown in this snip?"
        assert saved_msg.get("attachments") == attachments

        # Reload simulation: reset module-level DB connection state and re-read
        session_store._initialized = None
        reloaded_sess = session_store.get_session(sess_id)
        assert reloaded_sess is not None
        reloaded_msg = reloaded_sess["messages"][0]
        assert reloaded_msg.get("attachments") == attachments
        assert reloaded_msg["attachments"][0]["name"] == "snip.png"
        assert reloaded_msg["attachments"][0]["data"] == snip_data

    def test_add_message_without_attachments_defaults_empty(self, temp_db):
        sess_id = session_store.create_session("No Attachments")
        session_store.add_message(
            session_id=sess_id,
            role="user",
            content="Hello world",
        )
        sess = session_store.get_session(sess_id)
        assert sess is not None
        assert sess["messages"][0].get("attachments") == []


class TestVisionRoutingPolicy:
    """Verifies vision model routing with cloud and local Ollama auxiliary configurations."""

    @pytest.fixture
    def health_manager(self):
        hm = ProviderHealthManager()
        hm.record_success("google_gemini", 10.0)
        hm.record_success("anthropic", 15.0)
        hm.record_success("ollama", 5.0)
        return hm

    def test_infer_task_requirements_detects_images(self):
        reqs_no_img = infer_task_requirements("What is this?")
        assert reqs_no_img.has_images is False

        reqs_with_img = infer_task_requirements(
            "What is this?",
            images=[{"name": "snip.png", "data": "data:image/png;base64,..."}],
        )
        assert reqs_with_img.has_images is True

    def test_select_route_with_cloud_vision_override(self, health_manager):
        policy = RoutingPolicy(GLOBAL_MODEL_REGISTRY)
        attachments = [{"name": "snip.png", "data": "data:image/png;base64,..."}]

        decision = policy.select_route(
            prompt="Explain this error in the snip",
            configured_providers=["google_gemini", "anthropic"],
            health_manager=health_manager,
            user_default_provider="google_gemini",
            user_default_model="gemini-3.5-flash",
            auxiliary_models={"vision": "google_gemini:::gemini-3.5-flash"},
            images=attachments,
        )

        assert decision.provider == "google_gemini"
        assert decision.model == "gemini-3.5-flash"
        assert "vision" in decision.reason.lower() or "auxiliary override" in decision.reason.lower()
        assert "vision" in decision.capabilities

    def test_select_route_with_local_ollama_vision_override(self, health_manager):
        policy = RoutingPolicy(GLOBAL_MODEL_REGISTRY)
        attachments = [{"name": "snip.png", "data": "data:image/png;base64,..."}]

        # Local Ollama model configured in aux-vision
        decision = policy.select_route(
            prompt="Read the text from this screenshot",
            configured_providers=["google_gemini"],
            health_manager=health_manager,
            user_default_provider="google_gemini",
            user_default_model="gemini-3.5-flash",
            auxiliary_models={"vision": "ollama:::llava"},
            images=attachments,
        )

        assert decision.provider == "ollama"
        assert decision.model == "llava"
        assert "vision" in decision.capabilities

    def test_select_route_with_auto_vision_falls_back_to_default_vision_capable_model(
        self, health_manager
    ):
        policy = RoutingPolicy(GLOBAL_MODEL_REGISTRY)
        attachments = [{"name": "snip.png", "data": "data:image/png;base64,..."}]

        decision = policy.select_route(
            prompt="Summarize this diagram",
            configured_providers=["google_gemini"],
            health_manager=health_manager,
            user_default_provider="google_gemini",
            user_default_model="gemini-3.5-flash",
            auxiliary_models={"vision": "auto"},
            images=attachments,
        )

        assert decision.provider == "google_gemini"
        assert decision.model == "gemini-3.5-flash"
        assert "vision" in decision.capabilities


class TestLocalVisionNoExternalNetworkCalls:
    """Verifies that with a local vision model, verifiably zero network calls leave the machine."""

    @pytest.mark.asyncio
    async def test_local_model_makes_zero_outbound_network_calls(self):
        from aether_engine.orchestration.core import _create_provider_instance
        from aether_engine.secrets.storage import SecretStore

        # Create mock secret store without any cloud API keys
        with tempfile.TemporaryDirectory() as tmpdir:
            sec_db = Path(tmpdir) / "secrets.db"
            secret_store = SecretStore(db_path=sec_db)

            # Creating provider instance for Ollama should not fail
            provider = _create_provider_instance("ollama", "llava", secret_store)
            assert provider.provider_name == "ollama"
            assert provider.model == "llava"

            # Trap socket connections: allow only loopback addresses
            outbound_attempts = []

            def guarded_connect(sock, address):
                host = address[0] if isinstance(address, tuple) else address
                # Local loopback hosts
                if host in ("127.0.0.1", "localhost", "::1"):
                    # Mock connection refusing/mocked for local server
                    raise ConnectionRefusedError(f"Mocked local daemon connection: {address}")
                outbound_attempts.append(address)
                raise AssertionError(
                    f"VIOLATION: Outbound network call detected to non-local address: {address}"
                )

            with (
                patch("socket.socket.connect", side_effect=guarded_connect),
                patch("litellm.acompletion") as mock_acompletion,
            ):
                async def fake_stream(*args, **kwargs):
                    chunk = MagicMock()
                    chunk.choices = [MagicMock(delta=MagicMock(content="Analysis from local vision model", reasoning_content=None))]
                    yield chunk

                mock_acompletion.return_value = fake_stream()

                messages = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Describe this"},
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,ABC"}},
                        ],
                    }
                ]
                chunks = []
                async for chunk in provider.call_stream(messages, node_name="vision_task"):
                    chunks.append(chunk)

                assert len(chunks) == 1
                assert chunks[0].text == "Analysis from local vision model"
                # Zero outbound non-loopback calls
                assert outbound_attempts == []


class TestWebSocketSnipTaskFlow:
    """Verifies START_TASK handling of attachments and persistence over WebSocket."""

    def test_start_task_attachments_parsing_and_persistence(self, temp_db):
        from aether_engine.routing.policy import GLOBAL_ROUTING_POLICY

        user_cfg = UserConfig(
            default_provider="google_gemini",
            default_model="gemini-3.5-flash",
            auxiliary_models={"vision": "ollama:::llava"},
        )

        # Given a START_TASK event payload with image attachments
        snip_attachment = {
            "name": "snip.png",
            "mime_type": "image/png",
            "data": "data:image/png;base64,iVBORw0KGgo...",
        }
        payload = {
            "prompt": "What does this button do?",
            "session_id": "test-session-snip",
            "message_id": "client-msg-12345",
            "attachments": [snip_attachment],
        }

        # 1. Routing must pick the aux vision model
        decision = GLOBAL_ROUTING_POLICY.select_route(
            prompt=payload["prompt"],
            configured_providers=["google_gemini"],
            health_manager=ProviderHealthManager(),
            user_default_provider=user_cfg.default_provider,
            user_default_model=user_cfg.default_model,
            auxiliary_models=user_cfg.auxiliary_models,
            images=payload.get("attachments"),
        )
        assert decision.provider == "ollama"
        assert decision.model == "llava"

        # 2. Persistence in session_store with attachments
        sess_id = session_store.create_session("Snip Session")
        session_store.add_message(
            session_id=sess_id,
            role="user",
            content=payload["prompt"],
            message_id=payload["message_id"],
            attachments=payload["attachments"],
        )
        saved = session_store.get_session(sess_id)
        assert saved["messages"][0]["attachments"] == [snip_attachment]
