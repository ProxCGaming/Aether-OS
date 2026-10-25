import os
import tempfile
from pathlib import Path
import secrets

DEFAULT_TOKEN_PATH = Path(".run/engine.token")


def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def _chmod_owner_only(path: Path) -> None:
    """Best-effort owner-only (0600) permissions.

    On POSIX this is a real 0600. On Windows there are no POSIX mode
    bits: ``os.chmod(path, 0o600)`` only toggles the read-only flag and
    does NOT restrict the file ACL — a truly owner-only ACL would need
    ``win32security``/``icacls``. The token file therefore relies on
    the default per-user ACL of the profile directory on Windows;
    callers that need a hard guarantee on Windows must set the ACL
    explicitly. Never raises: permission hardening must not break
    engine startup.
    """
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def write_token(token: str, path: Path = DEFAULT_TOKEN_PATH) -> Path:
    """Write the engine token atomically with owner-only permissions.

    The token is written to a temporary file in the same directory
    (created by ``mkstemp``, which is 0600 on POSIX), fsynced, then
    atomically renamed over ``path`` — a reader never sees a partial
    token, and an existing world-readable file at ``path`` is replaced
    (its old permissions do not survive the rename).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = token.strip().encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        _chmod_owner_only(tmp_path)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        # os.replace preserves the temp file's mode on POSIX, but
        # re-apply after the rename so the final path is 0600 even on
        # platforms where the rename does not preserve it.
        _chmod_owner_only(path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


def read_token(path: Path = DEFAULT_TOKEN_PATH) -> str:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Token file not found: {path}")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"Token file empty: {path}")
    return token


def verify_token(provided: str, expected: str) -> bool:
    if not provided or not expected:
        return False
    return secrets.compare_digest(provided.strip(), expected.strip())
