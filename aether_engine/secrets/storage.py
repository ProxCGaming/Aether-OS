import contextlib
import sqlite3
from pathlib import Path
from typing import List, Optional, Union

from aether_engine.secrets.dpapi import (
    BaseProtector,
    SecretDecryptionError,
    WindowsDPAPIProtector,
)


class ProviderNotFoundError(Exception):
    """Raised when a requested provider key does not exist in SecretStore."""
    pass


class SecretStore:
    """Secure encrypted storage for API keys and secrets using DPAPI and SQLite."""

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        protector: Optional[BaseProtector] = None,
    ):
        if db_path is None:
            db_path = Path.home() / ".aether" / "secrets.db"
        self.db_path = Path(db_path)
        self.protector = protector if protector is not None else WindowsDPAPIProtector()
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS providers (
                        name TEXT PRIMARY KEY,
                        api_key BLOB NOT NULL
                    )
                    """
                )

    def list_providers(self) -> List[str]:
        if not self.db_path.exists():
            return []
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM providers ORDER BY name ASC")
            return [row[0] for row in cursor.fetchall()]

    def save_provider(self, provider: str, api_key: str) -> None:
        self._init_db()
        encrypted_bytes = self.protector.protect(api_key.encode("utf-8"))
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO providers (name, api_key) VALUES (?, ?)
                    ON CONFLICT(name) DO UPDATE SET api_key = excluded.api_key
                    """,
                    (provider, encrypted_bytes),
                )

    def load_provider(self, provider: str) -> str:
        if not self.db_path.exists():
            raise ProviderNotFoundError(f"Provider '{provider}' not found.")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT api_key FROM providers WHERE name = ?", (provider,))
            row = cursor.fetchone()
            if not row:
                raise ProviderNotFoundError(f"Provider '{provider}' not found.")
            encrypted_bytes = row[0]
            decrypted_bytes = self.protector.unprotect(encrypted_bytes)
            return decrypted_bytes.decode("utf-8")

    def delete_provider(self, provider: str) -> bool:
        if not self.db_path.exists():
            return False
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                cursor = conn.execute("DELETE FROM providers WHERE name = ?", (provider,))
                return cursor.rowcount > 0
