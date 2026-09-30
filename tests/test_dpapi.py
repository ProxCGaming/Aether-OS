import sys
import pytest
from unittest.mock import patch, MagicMock

from aether_engine.secrets.dpapi import WindowsDPAPIProtector, SecretDecryptionError

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
    mock_set, mock_get = mock_keyring
    protector = WindowsDPAPIProtector()
    test_data = b"super_secret_api_key_123"
    
    # Test protect
    protected_data = protector.protect(test_data)
    
    # Verify it was wrapped as keyring string
    assert protected_data.startswith(b"keyring:")
    
    # Extract the UUID and verify it's stored in mock keyring
    secret_id = protected_data.decode("utf-8").split("keyring:", 1)[1]
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
    
    with pytest.raises(SecretDecryptionError, match="Failed to decode legacy secret"):
        protector.unprotect(b"not_a_keyring_format")
