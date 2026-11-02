import base64
from unittest.mock import patch

import pytest

from aether_engine.secrets.dpapi import SecretDecryptionError, WindowsDPAPIProtector


@pytest.fixture
def mock_keyring():
    with patch("keyring.set_password") as mock_set, \
         patch("keyring.get_password") as mock_get:
        
        storage = {}
        def set_password(service, username, password):
            storage[(service, username)] = password
        def get_password(service, username):
            return storage.get((service, username))
            
        mock_set.side_effect = set_password
        mock_get.side_effect = get_password
        
        yield mock_set, mock_get

@pytest.fixture
def non_windows_platform():
    with patch("sys.platform", "linux"):
        yield

def test_dpapi_keyring_fallback_protect_unprotect(mock_keyring, non_windows_platform):
    mock_set, _mock_get = mock_keyring
    protector = WindowsDPAPIProtector()
    test_data = b"super_secret_api_key_123"
    
    # Test protect
    protected_data = protector.protect(test_data)
    
    # Verify it was wrapped as keyring string
    assert protected_data.startswith(b"keyring:")
    
    # Extract the UUID and verify it's stored in mock keyring
    _secret_id = protected_data.decode("utf-8").split("keyring:", 1)[1]
    assert mock_set.called
    
    # Test unprotect
    unprotected_data = protector.unprotect(protected_data)
    assert unprotected_data == test_data

def test_dpapi_keyring_fallback_unprotect_not_found(mock_keyring, non_windows_platform):
    protector = WindowsDPAPIProtector()
    
    with pytest.raises(SecretDecryptionError, match="Secret not found in OS keychain"):
        protector.unprotect(b"keyring:non_existent_id")

def test_dpapi_keyring_fallback_unprotect_invalid_format(non_windows_platform):
    protector = WindowsDPAPIProtector()

    with pytest.raises(SecretDecryptionError, match="Unsupported secret format"):
        protector.unprotect(b"not_a_keyring_format")


def test_dpapi_unprotect_bare_base64_blob_fails_closed(non_windows_platform):
    """S2-M10: a bare base64 blob must NOT silently decode to plaintext.

    Previously any non-``keyring:`` value fell into a legacy branch that
    returned ``base64.b64decode(data)`` with no key, MAC or validation —
    so any base64 stored as a secret was effectively plaintext at rest.
    """
    protector = WindowsDPAPIProtector()
    plaintext = b"super_secret_api_key_123"
    bare_b64 = base64.b64encode(plaintext)

    # Sanity: this is exactly the value the old fallback would have returned.
    assert base64.b64decode(bare_b64) == plaintext

    with pytest.raises(SecretDecryptionError, match="Unsupported secret format"):
        protector.unprotect(bare_b64)


def test_dpapi_unprotect_bare_base64_text_fails_closed(non_windows_platform):
    """A base64 blob that happens to decode to valid UTF-8 also fails closed."""
    protector = WindowsDPAPIProtector()
    bare_b64 = base64.b64encode(b"sk-live-abcdef123456")

    with pytest.raises(SecretDecryptionError):
        protector.unprotect(bare_b64)


def test_dpapi_unprotect_plaintext_fails_closed(non_windows_platform):
    protector = WindowsDPAPIProtector()

    with pytest.raises(SecretDecryptionError, match="Unsupported secret format"):
        protector.unprotect(b"plain-text-secret-not-a-reference")


def test_dpapi_unprotect_non_utf8_blob_fails_closed(non_windows_platform):
    """Non-UTF-8 bytes (e.g. a foreign DPAPI blob) fail closed, typed."""
    protector = WindowsDPAPIProtector()

    with pytest.raises(SecretDecryptionError, match="Unsupported secret format"):
        protector.unprotect(b"\xff\xfe\x00\x01binary-blob")


def test_dpapi_unprotect_empty_fails_closed(non_windows_platform):
    protector = WindowsDPAPIProtector()

    with pytest.raises(SecretDecryptionError, match="Unsupported secret format"):
        protector.unprotect(b"")


def test_dpapi_unprotect_empty_keyring_id_fails_closed(
    mock_keyring, non_windows_platform
):
    protector = WindowsDPAPIProtector()

    with pytest.raises(SecretDecryptionError, match="Unsupported secret format"):
        protector.unprotect(b"keyring:")


def test_secret_store_bare_base64_row_fails_closed(
    mock_keyring, non_windows_platform, tmp_path
):
    """Storage-layer check: a DB row holding bare base64 is not a secret.

    Writes the row directly (bypassing protect) to simulate a legacy /
    tampered secrets.db, then asserts load fails closed instead of
    returning the decoded plaintext.
    """
    import contextlib
    import sqlite3

    from aether_engine.secrets.storage import SecretStore

    db_path = tmp_path / "secrets.db"
    store = SecretStore(db_path=db_path, protector=WindowsDPAPIProtector())
    # Round-trip through the real (mocked-keyring) protector still works.
    store.save_provider("good_provider", "sk-good-key")
    assert store.load_provider("good_provider") == "sk-good-key"

    bare_b64 = base64.b64encode(b"sk-legacy-plaintext")
    with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO providers (name, api_key) VALUES (?, ?)",
            ("legacy_provider", bare_b64),
        )

    with pytest.raises(SecretDecryptionError, match="Unsupported secret format"):
        store.load_provider("legacy_provider")
