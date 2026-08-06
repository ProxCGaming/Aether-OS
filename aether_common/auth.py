from pathlib import Path
import secrets

DEFAULT_TOKEN_PATH = Path(".run/engine.token")


def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def write_token(token: str, path: Path = DEFAULT_TOKEN_PATH) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token.strip(), encoding="utf-8")
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
