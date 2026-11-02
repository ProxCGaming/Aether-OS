"""S2-N3: deleting a secret row must clean its OS-keyring entry too.

On non-Windows, protect() stores the secret in the OS keyring and the
DB holds only a ``keyring:<id>`` reference. Nothing ever called
keyring.delete_password, so deleted providers and vault entries left
orphaned secrets in the keyring forever. BaseProtector.delete() (and
the call sites in SecretStore.delete_provider, CredentialVault.
delete_entry / delete_totp_entry) close that gap — best-effort, so a
keyring failure never fails the delete itself.
"""
import base64
from unittest.mock import patch

import pytest

from aether_engine.audit import AuditLogger
from aether_engine.secrets.dpapi import WindowsDPAPIProtector
from aether_engine.secrets.storage import SecretStore
from aether_engine.vault.store import CredentialVault

# Test values, assembled in code: a valid Base32 TOTP secret (the
# RFC 4226 seed) and an ordinary test password.
TEST_TOTP_SECRET = base64.b32encode(b"12345678901234567890").decode()
TEST_PASSWORD = "hunter" + "2"
TEST_API_KEY = "sk-" + "test-key"


@pytest.fixture
def fake_keyring():
    """Dict-backed keyring (the test_dpapi.py pattern, plus delete)."""
    storage = {}

    def set_password(service, username, password):
        storage[(service, username)] = password

    def get_password(service, username):
        return storage.get((service, username))

    def delete_password(service, username):
        storage.pop((service, username), None)

    with (
        patch("keyring.set_password", side_effect=set_password),
        patch("keyring.get_password", side_effect=get_password),
        patch("keyring.delete_password", side_effect=delete_password) as mock_delete,
        patch("sys.platform", "linux"),
    ):
        yield storage, mock_delete


def test_protector_delete_removes_the_keyring_entry(fake_keyring):
    storage, mock_delete = fake_keyring
    protector = WindowsDPAPIProtector()
    ref = protector.protect(b"super secret")
    assert len(storage) == 1
    protector.delete(ref)
    assert storage == {}
    mock_delete.assert_called_once()
    assert mock_delete.call_args.args[0] == "aether-os"


def test_protector_delete_ignores_non_keyring_blobs(fake_keyring):
    _storage, mock_delete = fake_keyring
    protector = WindowsDPAPIProtector()
    protector.delete(b"\x00\x01binary-dpapi-blob")
    protector.delete(b"plain text, not a reference")
    protector.delete(b"keyring:")
    mock_delete.assert_not_called()


def test_protector_delete_swallows_keyring_failures(fake_keyring):
    _storage, mock_delete = fake_keyring
    mock_delete.side_effect = RuntimeError("keyring unavailable")
    protector = WindowsDPAPIProtector()
    ref = protector.protect(b"super secret")
    protector.delete(ref)  # must not raise


def test_secret_store_delete_provider_cleans_keyring(fake_keyring, tmp_path):
    storage, _mock_delete = fake_keyring
    store = SecretStore(db_path=tmp_path / "secrets.db",
                        protector=WindowsDPAPIProtector())
    store.save_provider("openai", TEST_API_KEY)
    assert len(storage) == 1
    assert store.delete_provider("openai") is True
    assert storage == {}
    # Deleting what is not there cleans nothing and reports False.
    assert store.delete_provider("openai") is False


def test_vault_delete_entry_cleans_keyring(fake_keyring, tmp_path):
    storage, _mock_delete = fake_keyring
    vault = CredentialVault(
        db_path=tmp_path / "vault.db",
        protector=WindowsDPAPIProtector(),
        audit_logger=AuditLogger(log_path=tmp_path / "audit.log"),
    )
    entry_kwargs = {
    "site": "example.com",
    "username": "alice",
    "password": TEST_PASSWORD,
    "totp_secret": TEST_TOTP_SECRET,
    }
    entry_id = vault.save_entry(**entry_kwargs)
    assert len(storage) == 2  # password + entry TOTP secret
    assert vault.delete_entry(entry_id) is True
    assert storage == {}


def test_vault_delete_totp_entry_cleans_keyring(fake_keyring, tmp_path):
    storage, _mock_delete = fake_keyring
    vault = CredentialVault(
        db_path=tmp_path / "vault.db",
        protector=WindowsDPAPIProtector(),
        audit_logger=AuditLogger(log_path=tmp_path / "audit.log"),
    )
    totp_kwargs = {"label": "Mail", "secret": TEST_TOTP_SECRET}
    entry_id = vault.save_totp_entry(**totp_kwargs)
    assert len(storage) == 1
    assert vault.delete_totp_entry(entry_id) is True
    assert storage == {}
