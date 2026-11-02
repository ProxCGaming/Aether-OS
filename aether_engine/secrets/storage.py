import contextlib
import sqlite3
from pathlib import Path

from aether_engine.audit import AuditLogger
from aether_engine.secrets.dpapi import (
    BaseProtector,
    WindowsDPAPIProtector,
)


class ProviderNotFoundError(Exception):
    """Raised when a requested provider key does not exist in SecretStore."""


class SecretStore:
    """Secure encrypted storage for API keys and secrets using DPAPI and SQLite."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        protector: BaseProtector | None = None,
        audit_logger: AuditLogger | None = None,
    ):
        if db_path is None:
            db_path = Path.home() / ".aether" / "secrets.db"
        self.db_path = Path(db_path)
        self.protector = protector if protector is not None else WindowsDPAPIProtector()
        # Phase 10.2, C2: key loads/deletes are audited (critical/07 M4).
        # No logger supplied -> default AuditLogger (same pattern as
        # ToolRegistry.execute). Audit is best-effort here: a broken audit
        # log must not break provider auth/startup (cf. critical/07 L4) —
        # only scripts/export_keys.py fails closed on audit, deliberately.
        self._audit_logger = audit_logger
        self._init_db()

    def _audit(self, action: str, provider: str) -> None:
        try:
            logger = self._audit_logger or AuditLogger()
            self._audit_logger = logger
            # Provider name only — never a key value (audit.py redacts
            # sensitive keys as a second line of defence).
            logger.log_event(action, {"provider": provider})
        except Exception:  # noqa: BLE001, S110 — audit logging must never break secret storage
            pass

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                    CREATE TABLE IF NOT EXISTS providers (
                        name TEXT PRIMARY KEY,
                        api_key BLOB NOT NULL
                    )
                    """
            )
            self._migrate_providers_schema(conn)

    def _migrate_providers_schema(self, conn: sqlite3.Connection) -> None:
        """Normalize legacy 'providers' tables to the current (name, api_key) schema.

        Older builds created this table with extra NOT NULL columns such as
        created_at/updated_at. Because ``CREATE TABLE IF NOT EXISTS`` never alters
        an existing table, saves on those databases failed with
        ``IntegrityError: NOT NULL constraint failed: providers.created_at``.
        This preserves the stored keys while rebuilding the table.
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(providers)")}
        if not cols or cols == {"name", "api_key"}:
            return
        if not {"name", "api_key"}.issubset(cols):
            return

        conn.execute("ALTER TABLE providers RENAME TO providers__legacy")
        conn.execute(
            """
            CREATE TABLE providers (
                name TEXT PRIMARY KEY,
                api_key BLOB NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO providers (name, api_key) "
            "SELECT name, api_key FROM providers__legacy"
        )
        conn.execute("DROP TABLE providers__legacy")

    def list_providers(self) -> list[str]:
        if not self.db_path.exists():
            return []
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM providers ORDER BY name ASC")
            return [row[0] for row in cursor.fetchall()]

    def save_provider(self, provider: str, api_key: str) -> None:
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("Provider name must be a non-empty string.")
        if not isinstance(api_key, str):
            raise TypeError("API key must be a string.")
        self._init_db()
        encrypted_bytes = self.protector.protect(api_key.encode("utf-8"))
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
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
            decrypted = decrypted_bytes.decode("utf-8")
            # Phase 10.2, C2: a successful decryption materialises the
            # plaintext key — audit it (provider name only). This is the
            # audit for the websocket PROVIDER_REVEAL_KEY_REQUEST path,
            # which calls load_provider and previously logged nothing
            # (critical/07 M4); it also covers export_keys.py, which
            # reveals every stored key. Failed loads (not found /
            # undecryptable) are not audited: no key was revealed.
            self._audit("SECRET_LOADED", provider)
            return decrypted

    def delete_provider(self, provider: str) -> bool:
        if not self.db_path.exists():
            return False
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
            cursor = conn.execute("DELETE FROM providers WHERE name = ?", (provider,))
            deleted = cursor.rowcount > 0
        # Phase 10.2, C2: deleting a stored key is a destructive action —
        # audit it, but only when a row was actually removed (critical/07
        # M4). Provider name only, never a key value.
        if deleted:
            self._audit("SECRET_DELETED", provider)
        return deleted
