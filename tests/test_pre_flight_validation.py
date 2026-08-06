import os
import tempfile
import unittest
from pathlib import Path

from aether_engine.validation.pre_flight import validate_tool_request


class TestPreFlightValidation(unittest.TestCase):
    def test_delete_file_validation_requires_existing_target(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "missing.txt"
            result = validate_tool_request("delete_file", {"path": str(target)})

        self.assertFalse(result.is_valid)
        self.assertIn("does not exist", result.error)

    def test_write_file_validation_requires_existing_parent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "child" / "notes.txt"
            result = validate_tool_request("write_file", {"path": str(target)})

        self.assertFalse(result.is_valid)
        self.assertIn("parent directory", result.error)

    def test_execute_shell_validation_requires_executable_in_path(self):
        result = validate_tool_request("execute_shell", {"command": "echo hi"})
        self.assertTrue(result.is_valid)

        invalid = validate_tool_request("execute_shell", {"command": "totally_missing_tool --help"})
        self.assertFalse(invalid.is_valid)
        self.assertIn("not found in PATH", invalid.error)


if __name__ == "__main__":
    unittest.main()
