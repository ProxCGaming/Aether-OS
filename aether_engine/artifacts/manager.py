import os
from pathlib import Path

_DEFAULT_ARTIFACTS_DIR = Path.home() / ".aether" / "artifacts"

def get_task_artifact_dir(task_id: str) -> Path:
    """
    Returns the artifact directory for a given task, creating it if it doesn't exist.
    """
    task_dir = _DEFAULT_ARTIFACTS_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir

def get_node_artifact_dir(task_id: str, node_type: str) -> Path:
    """
    Returns the specific artifact directory for a node (e.g. 'research', 'code', 'plan').
    """
    node_dir = get_task_artifact_dir(task_id) / node_type
    node_dir.mkdir(parents=True, exist_ok=True)
    return node_dir

def save_artifact(task_id: str, node_type: str, filename: str, content: str) -> str:
    """
    Saves content to an artifact file and returns the absolute path as a string.
    """
    node_dir = get_node_artifact_dir(task_id, node_type)
    file_path = node_dir / filename
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
        
    return str(file_path)
