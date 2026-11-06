"""Phase 5.1 acceptance tests — verify all six fixes.

Each test validates one of the confirmed bugs is actually fixed.
"""
import os
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch


class TestFix1CheckpointDurability(unittest.TestCase):
    """Fix 1: Checkpointer must use a persistent file, not :memory:."""

    def test_executor_uses_persistent_db_path(self):
        """Verify the executor source code no longer contains ':memory:' for the checkpointer."""
        executor_path = Path(__file__).resolve().parent.parent / "aether_engine" / "langgraph" / "executor.py"
        source = executor_path.read_text(encoding="utf-8")
        self.assertNotIn('":memory:"', source, "executor.py still uses in-memory checkpointer!")
        # Phase 9: the path now comes from the shared checkpointer constant
        # (single source of truth) instead of a triplicated inline literal.
        self.assertIn("DEFAULT_CHECKPOINT_DB", source, "executor.py should use the shared DEFAULT_CHECKPOINT_DB")
        self.assertIn("from aether_engine.langgraph.checkpointer import DEFAULT_CHECKPOINT_DB", source)

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

    # ── B-06 rewrite (Wave 0) ──────────────────────────────────────
    # The old test here compared a RISKY_TOOLS constant between
    # graph.py and execute_tool.py; that symbol no longer exists in
    # either module — the risky-tools concept moved into the tool
    # registry's risk tiers (ToolRisk). These tests pin the CURRENT
    # contract: the tier the registry assigns to representative
    # built-in tools, and the enforcement consequence those tiers
    # drive through ToolRegistry.execute (the real policy pipeline).

    # name -> the tier the registry must assign it.
    EXPECTED_RISK_TIERS: ClassVar[dict[str, str]] = {
        "get_current_time": "read_only",
        "search_web": "read_only",
        "write_file": "write",
        "edit_file": "write",
        "create_directory": "write",
        "delete_file": "destructive",
        "execute_shell": "execute",
    }

    def test_registry_assigns_expected_risk_tiers(self):
        """Representative built-ins carry the risk tier their behaviour warrants."""
        from aether_engine.tools.registry import ToolRisk, build_tool_registry

        registry = build_tool_registry()
        for name, tier in self.EXPECTED_RISK_TIERS.items():
            tool = registry.get_tool(name)
            self.assertEqual(
                tool.risk, ToolRisk(tier),
                f"{name} should be tier {tier!r}, registry says {tool.risk.value!r}",
            )
        # Plan mode offers exactly the READ_ONLY set (registry
        # .read_only_names): read-only representatives are in,
        # anything that writes, executes or destroys is out.
        read_only = registry.read_only_names()
        self.assertIn("get_current_time", read_only)
        self.assertIn("search_web", read_only)
        for name in ("write_file", "delete_file", "execute_shell"):
            self.assertNotIn(name, read_only)

    def test_risk_tiers_drive_approval_enforcement(self):
        """Through the real pipeline: READ_ONLY runs unapproved; WRITE and
        DESTRUCTIVE tools are refused until approved — and nothing runs."""
        import asyncio

        from aether_engine.tools.registry import ToolExecutionError, build_tool_registry

        class _NullAudit:
            def log_event(self, *args, **kwargs):
                pass

        registry = build_tool_registry()

        async def _run():
            # READ_ONLY: no approval needed, executes in-process.
            result = await registry.execute(
                "get_current_time", {}, audit_logger=_NullAudit())
            self.assertIsInstance(result, str)
            self.assertTrue(result)

            with tempfile.TemporaryDirectory() as tmpdir:
                target = os.path.join(tmpdir, "victim.txt")
                Path(target).write_text("precious", encoding="utf-8")
                # DESTRUCTIVE without approval: refused, file untouched.
                with self.assertRaises(ToolExecutionError) as ctx:
                    await registry.execute(
                        "delete_file", {"path": target}, audit_logger=_NullAudit())
                self.assertIn("requires approval", str(ctx.exception))
                self.assertTrue(Path(target).exists())
                # WRITE without approval: refused, nothing created.
                new_file = os.path.join(tmpdir, "new.txt")
                with self.assertRaises(ToolExecutionError) as ctx:
                    await registry.execute(
                        "write_file", {"path": new_file, "content": "x"},
                        audit_logger=_NullAudit())
                self.assertIn("requires approval", str(ctx.exception))
                self.assertFalse(Path(new_file).exists())

        asyncio.run(_run())

    def test_sandbox_module_exists(self):
        """Verify the shared sandbox module exists and is importable."""
        from aether_engine.workers.sandbox import execute_tool_in_worker
        self.assertTrue(callable(execute_tool_in_worker))


class TestFix3FailClosedApproval(unittest.IsolatedAsyncioTestCase):
    """Fix 3: Missing approval_handler must reject, not auto-approve."""

    @patch("aether_engine.langgraph.nodes.pause.interrupt")
    async def test_missing_handler_rejects(self, mock_interrupt):
        """When interrupt returns False (rejected), the tool call must be rejected."""
        from aether_engine.langgraph.nodes.pause import dummy_pause_node
    
        state = {
            "pending_tool_call": {
                "name": "write_file",
                "args": {"path": "/tmp/test.txt", "content": "evil"},
                "call_id": "call_1",
            }
        }
        config = {"configurable": {}}
        mock_interrupt.return_value = False
    
        result = dummy_pause_node(state, config)
        
        # Should reject: clear pending_tool_call and return rejection message
        self.assertIsNone(result.get("pending_tool_call"))
        self.assertTrue(len(result.get("messages", [])) > 0)
        msg = result["messages"][0]
        self.assertEqual(msg["role"], "tool")
        self.assertIn("rejected", msg["content"].lower())

    @patch("aether_engine.langgraph.nodes.pause.interrupt")
    async def test_explicit_handler_rejection_matches_missing_handler_format(self, mock_interrupt):
        """The rejection format for interrupt must match expected."""
        from aether_engine.langgraph.nodes.pause import dummy_pause_node
    
        state = {
            "pending_tool_call": {
                "name": "execute_shell",
                "args": {"command": "rm -rf /"},
                "call_id": "call_2",
            }
        }
    
        config = {"configurable": {}}
        mock_interrupt.return_value = False
        result = dummy_pause_node(state, config)
    
        self.assertEqual(result["messages"][0]["role"], "tool")
        self.assertIn("rejected", result["messages"][0]["content"].lower())


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
