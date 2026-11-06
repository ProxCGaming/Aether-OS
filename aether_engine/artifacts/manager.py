from pathlib import Path

_DEFAULT_ARTIFACTS_DIR = Path.home() / ".aether" / "artifacts"


def _validate_component(kind: str, value: str) -> str:
    """One path component, or ValueError (E7-09).

    ``task_id``, ``node_type`` and ``filename`` are joined straight
    into a filesystem path by this module. Present callers pass
    constants and server-generated ids, but this is the write
    primitive for agent output: a component carrying a separator, a
    ``..`` hop, or an absolute anchor would write outside the
    artifacts root, so every component must be a single plain name.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Artifact {kind} must be a non-empty string.")
    if (
        value in (".", "..")
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or Path(value).is_absolute()
    ):
        raise ValueError(
            f"Artifact {kind} must be a single path component, "
            f"got {value!r}.")
    return value


def _contained(path: Path) -> Path:
    """Resolve ``path`` and prove it stays under the artifacts root."""
    root = _DEFAULT_ARTIFACTS_DIR.resolve()
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(
            f"Artifact path {path} escapes the artifacts directory.")
    return resolved


def get_task_artifact_dir(task_id: str) -> Path:
    """
    Returns the artifact directory for a given task, creating it if it doesn't exist.
    """
    task_dir = _contained(
        _DEFAULT_ARTIFACTS_DIR / _validate_component("task_id", task_id))
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir


def get_node_artifact_dir(task_id: str, node_type: str) -> Path:
    """
    Returns the specific artifact directory for a node (e.g. 'research', 'code', 'plan').
    """
    node_dir = _contained(
        get_task_artifact_dir(task_id)
        / _validate_component("node_type", node_type))
    node_dir.mkdir(parents=True, exist_ok=True)
    return node_dir


def save_artifact(task_id: str, node_type: str, filename: str, content: str) -> str:
    """
    Saves content to an artifact file and returns the absolute path as a string.
    """
    node_dir = get_node_artifact_dir(task_id, node_type)
    file_path = _contained(
        node_dir / _validate_component("filename", filename))

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

    return str(file_path)
