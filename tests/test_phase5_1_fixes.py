"""Phase 5.1 acceptance tests — verify all six fixes.

Each test validates one of the confirmed bugs is actually fixed.
"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestFix1CheckpointDurability(unittest.TestCase):
    """Fix 1: Checkpointer must use a persistent file, not :memory:."""

    def test_executor_uses_persistent_db_path(self):
        """Verify the executor source code no longer contains ':memory:' for the checkpointer."""
        executor_path = Path(__file__).resolve().parent.parent / "aether_engine" / "langgraph" / "executor.py"
        source = executor_path.read_text(encoding="utf-8")
        self.assertNotIn('":memory:"', source, "executor.py still uses in-memory checkpointer!")
        self.assertIn("checkpoints.db", source, "executor.py should reference checkpoints.db")

    def test_default_checkpoint_path(self):
        """Verify the default checkpoint path is ~/.aether/checkpoints.db."""
        from aether_engine.langgraph.checkpointer import DEFAULT_CHECKPOINT_DB
        expected = Path.home() / ".aether" / "checkpoints.db"
        self.assertEqual(DEFAULT_CHECKPOINT_DB, expected)


class TestFix2SandboxEnforcement(unittest.TestCase):
    """Fix 2: LangGraph tool execution must use pre-flight validation and sandbox worker."""

    def test_execute_tool_imports_validation(self):
        """Verify execute_tool.py imports validate_tool_request."""
        source_path = Path(__file__).resolve().parent.parent / "aether_engine" / "langgraph" / "nodes" / "execute_tool.py"
        source = source_path.read_text(encoding="utf-8")
        self.assertIn("validate_tool_request", source)

    def test_execute_tool_imports_sandbox(self):
        """Verify execute_tool.py imports execute_tool_in_worker."""
        source_path = Path(__file__).resolve().parent.parent / "aether_engine" / "langgraph" / "nodes" / "execute_tool.py"
        source = source_path.read_text(encoding="utf-8")
        self.assertIn("execute_tool_in_worker", source)

    def test_preflight_rejects_outside_workspace(self):
        """Pre-flight validation rejects write_file to a path outside workspace roots."""
        from aether_engine.validation.pre_flight import validate_tool_request
        result = validate_tool_request(
            "write_file",
            {"path": "C:\\Windows\\System32\\evil.txt"},
            workspace_roots=["C:\\Users\\TestUser\\AetherWorkspace"],
        )
        self.assertFalse(result.is_valid, "write_file to outside workspace should be rejected")

    def test_preflight_allows_inside_workspace(self):
        """Pre-flight validation allows write_file to a path inside workspace roots."""
        from aether_engine.validation.pre_flight import validate_tool_request
        # Use temp dir as workspace root
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.txt")
            result = validate_tool_request(
                "write_file",
                {"path": target},
                workspace_roots=[tmpdir],
            )
            self.assertTrue(result.is_valid, f"write_file inside workspace should pass, got: {result.error}")

    def test_risky_tools_set_matches_graph(self):
        """Verify execute_tool.py's RISKY_TOOLS matches graph.py's RISKY_TOOLS."""
        from aether_engine.langgraph.graph import RISKY_TOOLS as graph_risky
        from aether_engine.langgraph.nodes.execute_tool import RISKY_TOOLS as node_risky
        self.assertEqual(graph_risky, node_risky)

    def test_sandbox_module_exists(self):
        """Verify the shared sandbox module exists and is importable."""
        from aether_engine.workers.sandbox import execute_tool_in_worker
        self.assertTrue(callable(execute_tool_in_worker))


class TestFix3FailClosedApproval(unittest.IsolatedAsyncioTestCase):
    """Fix 3: Missing approval_handler must reject, not auto-approve."""

    async def test_missing_handler_rejects(self):
        """When approval_handler is None in config, the tool call must be rejected."""
        from aether_engine.langgraph.nodes.pause import dummy_pause_node

        state = {
            "pending_tool_call": {
                "name": "write_file",
                "args": {"path": "/tmp/test.txt", "content": "evil"},
                "call_id": "call_1",
            }
        }
        config = {"configurable": {}}  # No approval_handler!

        result = await dummy_pause_node(state, config)

        # Should reject: clear pending_tool_call and return rejection message
        self.assertIsNone(result.get("pending_tool_call"))
        self.assertTrue(len(result.get("messages", [])) > 0)
        msg = result["messages"][0]
        self.assertEqual(msg["role"], "tool")
        self.assertIn("rejected", msg["content"].lower())

    async def test_explicit_handler_rejection_matches_missing_handler_format(self):
        """The rejection format for missing handler must match explicit user rejection."""
        from aether_engine.langgraph.nodes.pause import dummy_pause_node

        state = {
            "pending_tool_call": {
                "name": "execute_shell",
                "args": {"command": "rm -rf /"},
                "call_id": "call_2",
            }
        }

        # Missing handler
        config_missing = {"configurable": {}}
        result_missing = await dummy_pause_node(state, config_missing)

        # Explicit handler that rejects
        async def rejecting_handler(tool_name, args, req_id):
            return False

        # Reset state (pending_tool_call was cleared)
        state["pending_tool_call"] = {
            "name": "execute_shell",
            "args": {"command": "rm -rf /"},
            "call_id": "call_2",
        }
        config_explicit = {"configurable": {"approval_handler": rejecting_handler}}
        result_explicit = await dummy_pause_node(state, config_explicit)

        # Both should have the same message content
        self.assertEqual(
            result_missing["messages"][0]["content"],
            result_explicit["messages"][0]["content"],
        )


class TestFix4PerNodeFallback(unittest.TestCase):
    """Fix 4: LiteLLMProvider must support per-node fallback models."""

    def test_provider_accepts_fallback_models(self):
        """LiteLLMProvider constructor accepts fallback_models parameter."""
        from aether_engine.providers.litellm_provider import LiteLLMProvider
        provider = LiteLLMProvider(
            api_key="test-key",
            model="gemini-2.5-flash",
            provider_name="google_gemini",
            fallback_models=["openai/gpt-4o-mini"],
        )
        self.assertEqual(provider.fallback_models, ["openai/gpt-4o-mini"])

    def test_provider_default_no_fallbacks(self):
        """Without fallback_models, the list should be empty."""
        from aether_engine.providers.litellm_provider import LiteLLMProvider
        provider = LiteLLMProvider(
            api_key="test-key",
            model="gpt-4o-mini",
            provider_name="openai",
        )
        self.assertEqual(provider.fallback_models, [])

    def test_call_stream_builds_models_to_try_list(self):
        """Verify call_stream source constructs models_to_try from primary + fallbacks."""
        source_path = (
            Path(__file__).resolve().parent.parent
            / "aether_engine" / "providers" / "litellm_provider.py"
        )
        source = source_path.read_text(encoding="utf-8")
        self.assertIn("models_to_try", source)
        self.assertIn("NODE_FALLBACK_TRIGGERED", source)


class TestFix5ExportKeysAudit(unittest.TestCase):
    """Fix 5: export_keys.py must have audit logging."""

    def test_export_keys_has_audit_import(self):
        """Verify export_keys.py imports AuditLogger."""
        source_path = Path(__file__).resolve().parent.parent / "scripts" / "export_keys.py"
        source = source_path.read_text(encoding="utf-8")
        self.assertIn("AuditLogger", source)
        self.assertIn("SECRETS_EXPORTED", source)


class TestFix6DeadCodeCleanup(unittest.TestCase):
    """Fix 6: Dead code must be removed."""

    def test_gemini_py_deleted(self):
        """gemini.py should no longer exist."""
        gemini_path = Path(__file__).resolve().parent.parent / "aether_engine" / "providers" / "gemini.py"
        self.assertFalse(gemini_path.exists(), f"Dead code {gemini_path} still exists!")

    def test_simple_py_deleted(self):
        """orchestration/simple.py should no longer exist."""
        simple_path = Path(__file__).resolve().parent.parent / "aether_engine" / "orchestration" / "simple.py"
        self.assertFalse(simple_path.exists(), f"Dead code {simple_path} still exists!")

    def test_fallback_test_no_dead_import(self):
        """test_fallback_rules.py must not import from the dead simple module."""
        test_path = Path(__file__).resolve().parent / "test_fallback_rules.py"
        source = test_path.read_text(encoding="utf-8")
        # Check there's no actual import line from the dead module (comments mentioning it are fine)
        self.assertNotIn("from aether_engine.orchestration.simple", source)
        self.assertNotIn("import aether_engine.orchestration.simple", source)


if __name__ == "__main__":
    unittest.main()
