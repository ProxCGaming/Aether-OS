# ADR 0012: DPAPI String Buffer and Null Terminator Fix

## Status
Accepted

## Context
AETHER uses Windows Data Protection API (DPAPI) to securely encrypt and decrypt sensitive user secrets (such as LLM API keys) at rest. These functions are implemented in `aether_engine/secrets/dpapi.py` via `ctypes`.

Initially, the encryption routine used `ctypes.create_string_buffer(data)` to allocate memory for the incoming byte string before passing it to `CryptProtectData`. By design in Python's `ctypes`, `create_string_buffer` allocates a C-style string buffer which implicitly appends a null terminator byte (`\x00`) to the end of the data. 

Because of this, the null terminator was permanently included in the encrypted payload, and upon decryption, the trailing null byte was restored and appended to the API key string.

## Why This Was a Problem (The "Why")
This hidden null byte created a cascading chain of subtle bugs across the networking stack:

1. **HTTP Client Validation Failures:** When Aether Engine's LLM provider (`litellm`) attempted to use the decrypted API key, the underlying asynchronous HTTP client (`aiohttp`) encountered the null byte inside the `Authorization` or `x-goog-api-key` header. `aiohttp` strictly enforces header character limits and immediately threw a `ValueError: Forbidden control character detected in headers`.
2. **Obscured Root Cause in UI:** When this `ValueError` occurred, `litellm` triggered a fallback mechanism that subsequently failed (`litellm.MidStreamFallbackError`). The Aether UI caught this exception and rendered it to the user as `[time] ✖ Failed: Network/service error...`.
3. **Secondary Encoding Errors:** In an attempt to fix the opaque network error, the user likely copied the UI's error log (which contained the Unicode `✖` icon) and accidentally pasted it into an API key or Base URL configuration field. On the next execution, a different transport layer (`httpx`) attempted to encode the headers into ASCII, hit the `✖` character, and threw a `UnicodeEncodeError: 'ascii' codec can't encode character '\u2716' in position 97`. 

The root cause of all these issues was the seemingly innocuous trailing null byte introduced during encryption.

## Decision (The "How" and "What Changed")
To resolve this issue while maintaining backwards compatibility with previously saved user secrets, we made the following changes to `aether_engine/secrets/dpapi.py`:

1. **Fixing Encryption (Preventing Future Null Bytes):** 
   Replaced `ctypes.create_string_buffer(data)` with an exact-length character array `(ctypes.c_char * len(data)).from_buffer_copy(data)`. This ensures that memory is allocated precisely for the byte string without implicitly appending a null terminator.
   
2. **Fixing Decryption (Backwards Compatibility):** 
   Appended `.rstrip(b'\x00')` to the return statement of `_dpapi_decrypt`. This safely strips any trailing null bytes from the decrypted payload.

## Consequences
### Positive
- API keys and secrets are cleanly encoded and decoded without hidden control characters.
- Underlying HTTP clients (`aiohttp`, `httpx`) will no longer fail on header validation due to null bytes.
- Backwards compatibility is preserved: users do not have to re-enter or migrate their previously saved (and null-terminated) API keys. The `.rstrip` implementation handles it transparently.

### Negative / Trade-offs
- None. The `rstrip` operation on secret data is highly safe as legitimate API keys and base URLs do not intentionally terminate with a null byte.
