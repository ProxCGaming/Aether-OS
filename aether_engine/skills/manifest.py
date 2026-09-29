import json
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional

class SkillTrigger(BaseModel):
    description_embedding: Optional[List[float]] = None
    keywords: List[str] = Field(default_factory=list)
    slash_commands: List[str] = Field(default_factory=list)
    auto_match: bool = True

class SkillRequirements(BaseModel):
    tools: List[str] = Field(default_factory=list)
    agents: List[str] = Field(default_factory=list)
    capabilities: List[str] = Field(default_factory=list)

class SkillInjection(BaseModel):
    mode: str = "system_prompt_append"
    max_tokens: int = 2000
    priority: int = 5

class SkillSource(BaseModel):
    type: str = "local"
    origin: str = "builtin"
    installed_at: Optional[str] = None

class SkillManifest(BaseModel):
    name: str
    version: str = "1.0.0"
    description: str = ""
    author: str = ""
    trigger: SkillTrigger = Field(default_factory=SkillTrigger)
    requirements: SkillRequirements = Field(default_factory=SkillRequirements)
    injection: SkillInjection = Field(default_factory=SkillInjection)
    source: SkillSource = Field(default_factory=SkillSource)

    @classmethod
    def from_file(cls, path: Path) -> "SkillManifest":
        if not path.exists():
            raise FileNotFoundError(f"Manifest not found at {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)
