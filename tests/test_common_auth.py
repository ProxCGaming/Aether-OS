import unittest
import tempfile
import os
from pathlib import Path
from aether_common.auth import (
    generate_token,
    write_token,
    read_token,
    verify_token,
)

class TestCommonAuth(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.token_file = Path(self.test_dir.name) / "sub" / "engine.token"

    def tearDown(self):
        self.test_dir.cleanup()

    def test_generate_token(self):
        token1 = generate_token()
        token2 = generate_token()
        self.assertTrue(len(token1) >= 32)
        self.assertNotEqual(token1, token2)

    def test_write_and_read_token(self):
        token = generate_token()
        written_path = write_token(token, self.token_file)
        self.assertTrue(written_path.exists())

        read_val = read_token(self.token_file)
        self.assertEqual(read_val, token)

    def test_read_missing_token_raises(self):
        non_existent = Path(self.test_dir.name) / "does_not_exist.token"
        with self.assertRaises(FileNotFoundError):
            read_token(non_existent)

    def test_read_empty_token_raises(self):
        empty_file = Path(self.test_dir.name) / "empty.token"
        empty_file.write_text("   \n", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_token(empty_file)

    def test_verify_token(self):
        token = "secret-token-xyz-12345"
        self.assertTrue(verify_token(token, token))
        self.assertTrue(verify_token(f" {token} \n", token))
        self.assertFalse(verify_token("wrong-token", token))
        self.assertFalse(verify_token("", token))
        self.assertFalse(verify_token(token, ""))
        self.assertFalse(verify_token("", ""))

if __name__ == "__main__":
    unittest.main()
