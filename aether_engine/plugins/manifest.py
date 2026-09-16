import json
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional

class CapabilityDef(BaseModel):
    name: str
    description: Optional[str] = None
    file_path: Optional[str] = None

class PluginManifest(BaseModel):
    name: str
    version: str
    description: Optional[str] = None
    skills: List[CapabilityDef] = Field(default_factory=list)
    tools: List[CapabilityDef] = Field(default_factory=list)
    mcp_servers: List[CapabilityDef] = Field(default_factory=list)

def parse_manifest(path: Path) -> PluginManifest:
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return PluginManifest(**data)
