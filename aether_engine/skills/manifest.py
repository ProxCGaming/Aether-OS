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
    license: str = ""
    compatibility: str = ""
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

    @classmethod
    def from_skill_md(cls, path: Path) -> "SkillManifest":
        import yaml
        if not path.exists():
            raise FileNotFoundError(f"Skill file not found at {path}")
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        
        # Extract YAML frontmatter
        yaml_lines = []
        in_yaml = False
        for line in lines:
            if line.strip() == "---":
                if not in_yaml:
                    in_yaml = True
                    continue
                else:
                    break
            if in_yaml:
                yaml_lines.append(line)
                
        if not yaml_lines:
            raise ValueError(f"No YAML frontmatter found in {path}")
            
        metadata = yaml.safe_load("\n".join(yaml_lines))
        if not metadata or not isinstance(metadata, dict):
            raise ValueError(f"Invalid YAML frontmatter in {path}")
            
        name = metadata.get("name")
        if not name:
            raise ValueError(f"Missing required 'name' field in {path} frontmatter")
        
        meta_block = metadata.get("metadata", {})
        if not isinstance(meta_block, dict):
            meta_block = {}
            
        manifest = cls(
            name=name,
            description=metadata.get("description", ""),
            version=meta_block.get("version", "1.0.0"),
            author=meta_block.get("author", ""),
            license=metadata.get("license", ""),
            compatibility=metadata.get("compatibility", ""),
        )
        
        # Set up trigger
        manifest.trigger.slash_commands = [f"/{name}"]
        manifest.trigger.auto_match = str(meta_block.get("auto_match", "true")).lower() == "true"
        
        # Tools
        allowed_tools_str = metadata.get("allowed-tools", "")
        if allowed_tools_str and isinstance(allowed_tools_str, str):
            manifest.requirements.tools = allowed_tools_str.split()
            
        return manifest
