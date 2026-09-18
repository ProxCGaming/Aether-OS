import contextlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from aether_engine.secrets.dpapi import (
    BaseProtector,
    SecretDecryptionError,
    WindowsDPAPIProtector,
)
from aether_engine.secrets.storage import (
    ProviderNotFoundError,
    SecretStore,
)


class DummyProtector(BaseProtector):
    def protect(self, data: bytes) -> bytes:
        return b"ENC:" + data

    def unprotect(self, data: bytes) -> bytes:
        if not data.startswith(b"ENC:"):
            raise SecretDecryptionError("Invalid header")
        return data[4:]


class TestSecretStore(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_secrets.db"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_crud_with_dummy_protector(self):
        store = SecretStore(db_path=self.db_path, protector=DummyProtector())
        self.assertEqual(store.list_providers(), [])

        # Save
        store.save_provider("test_provider", "sk-123456789")
        self.assertEqual(store.list_providers(), ["test_provider"])

        # Load
        loaded = store.load_provider("test_provider")
        self.assertEqual(loaded, "sk-123456789")

        # Update
        store.save_provider("test_provider", "sk-new-key-987")
        self.assertEqual(store.load_provider("test_provider"), "sk-new-key-987")

        # Save second provider
        store.save_provider("another_provider", "another-key")
        self.assertEqual(store.list_providers(), ["another_provider", "test_provider"])

        # Delete
        self.assertTrue(store.delete_provider("test_provider"))
        self.assertFalse(store.delete_provider("test_provider"))
        self.assertEqual(store.list_providers(), ["another_provider"])

    def test_provider_not_found_raises(self):
        store = SecretStore(db_path=self.db_path, protector=DummyProtector())
        with self.assertRaises(ProviderNotFoundError):
            store.load_provider("non_existent")

    def test_decryption_error_raises(self):
        store = SecretStore(db_path=self.db_path, protector=DummyProtector())
        store.save_provider("google_gemini", "my-key")

        # Corrupt DB entry
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE providers SET api_key = ? WHERE name = ?", (b"CORRUPTED", "google_gemini"))
            conn.commit()

        with self.assertRaises(SecretDecryptionError):
            store.load_provider("google_gemini")

    def test_legacy_schema_is_migrated_on_init(self):
        # Simulate a DB created by an older build with extra NOT NULL columns.
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE providers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    api_key BLOB NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO providers (name, api_key, created_at, updated_at) VALUES (?, ?, ?, ?)",
                ("openrouter", b"ENC:old-key", 1.0, 1.0),
            )
            conn.commit()

        store = SecretStore(db_path=self.db_path, protector=DummyProtector())
        # Saving on the legacy table used to raise
        # "IntegrityError: NOT NULL constraint failed: providers.created_at".
        store.save_provider("custom_new_x", "sk-new")
        self.assertEqual(store.load_provider("custom_new_x"), "sk-new")
        self.assertIn("openrouter", store.list_providers())

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(providers)")}
        self.assertEqual(cols, {"name", "api_key"})

    def test_save_rejects_empty_provider_name(self):
        store = SecretStore(db_path=self.db_path, protector=DummyProtector())
        with self.assertRaises(ValueError):
            store.save_provider("", "sk-123")
        with self.assertRaises(ValueError):
            store.save_provider(None, "sk-123")

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_windows_dpapi_roundtrip(self):
        store = SecretStore(db_path=self.db_path, protector=WindowsDPAPIProtector())
        key = "AIzaSyTestGeminiKeyLiveDPAPI_999"
        store.save_provider("google_gemini", key)

        loaded = store.load_provider("google_gemini")
        self.assertEqual(loaded, key)


if __name__ == "__main__":
    unittest.main()
