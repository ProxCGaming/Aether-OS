import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

from aether_engine.skills.manifest import SkillManifest

logger = logging.getLogger("aether_engine.skills.manager")

@dataclass
class SkillMatch:
    name: str
    manifest: SkillManifest
    score: float

@dataclass
class RequirementCheck:
    met: bool
    missing_tools: List[str]
    reason: str

@dataclass
class SkillSummary:
    name: str
    description: str
    slash_commands: List[str]
    required_tools: List[str]
    auto_match: bool
    source: str


class SkillManager:
    """Central service for skill discovery, matching, and injection."""
    
    def __init__(self, global_dir: Optional[Path] = None, workspace_dirs: Optional[List[Path]] = None):
        self.global_dir = global_dir or (Path.home() / ".aether" / "skills")
        self.workspace_dirs = workspace_dirs or []
        
        self.global_dir.mkdir(parents=True, exist_ok=True)
        self._index: Dict[str, SkillManifest] = {}
        self._content_cache: Dict[str, str] = {}
        self._slash_map: Dict[str, str] = {}
        self._skill_paths: Dict[str, Path] = {} # Maps skill name to the directory it was loaded from
        self._rebuild_index()
    
    def update_workspace_roots(self, roots: List[str]):
        new_dirs = []
        for r in roots:
            p = Path(r) / ".agents" / "skills"
            if p.exists() and p.is_dir():
                new_dirs.append(p)
        if set(new_dirs) != set(self.workspace_dirs):
            self.workspace_dirs = new_dirs
            self._rebuild_index()
            
    def _rebuild_index(self):
        """Scan skills_dirs and build manifest index + slash command map."""
        self._index.clear()
        self._slash_map.clear()
        self._skill_paths.clear()
        
        # Priority: Global < Workspace 1 < Workspace 2 ...
        dirs_to_scan = [self.global_dir] + self.workspace_dirs
        
        for base_dir in dirs_to_scan:
            if not base_dir.exists() or not base_dir.is_dir():
                continue
                
            for skill_dir in base_dir.iterdir():
                if not skill_dir.is_dir():
                    continue
                
                md_paths = [
                    skill_dir / "SKILL.md",
                    skill_dir / f"{skill_dir.name}_SKILL.md",
                    skill_dir / f"{skill_dir.name}-skill.md"
                ]
                
                manifest = None
                for md_path in md_paths:
                    if md_path.exists():
                        try:
                            manifest = SkillManifest.from_skill_md(md_path)
                            break
                        except Exception as e:
                            logger.warning(f"Failed to parse SKILL.md at {md_path}: {e}")
                
                if not manifest:
                    manifest_path = skill_dir / "manifest.json"
                    if manifest_path.exists():
                        try:
                            manifest = SkillManifest.from_file(manifest_path)
                        except Exception as e:
                            logger.warning(f"Failed to load manifest.json at {manifest_path}: {e}")
                
                if manifest:
                    if manifest.name != skill_dir.name:
                        logger.warning(f"Skill name mismatch: {manifest.name} != {skill_dir.name}")
                        
                    self._index[manifest.name] = manifest
                    self._skill_paths[manifest.name] = skill_dir
                    for cmd in manifest.trigger.slash_commands:
                        self._slash_map[cmd] = manifest.name
    
    def resolve_slash_command(self, command: str) -> Optional[SkillManifest]:
        """Resolve a slash command to its skill manifest."""
        skill_name = self._slash_map.get(command)
        return self._index.get(skill_name) if skill_name else None
    
    def match_skills(self, prompt: str, agent_id: str, limit: int = 2) -> List[SkillMatch]:
        """Auto-match skills to a prompt for a given agent. 
        Returns top matches sorted by relevance score."""
        matches = []
        for name, manifest in self._index.items():
            if not manifest.trigger.auto_match:
                continue
            # Only match skills compatible with this agent
            if manifest.requirements.agents and agent_id not in manifest.requirements.agents:
                continue
            score = self._compute_match_score(prompt, manifest)
            if score > 0.3:  # threshold
                matches.append(SkillMatch(name=name, manifest=manifest, score=score))
        return sorted(matches, key=lambda m: m.score, reverse=True)[:limit]
    
    def _compute_match_score(self, prompt: str, manifest: SkillManifest) -> float:
        """Keyword-based matching (fast). Can be upgraded to embedding similarity later."""
        lower = prompt.lower()
        if not manifest.trigger.keywords:
            return 0.0
        keyword_hits = sum(1 for kw in manifest.trigger.keywords if kw.lower() in lower)
        return keyword_hits / len(manifest.trigger.keywords)
    
    def load_skill_content(self, skill_name: str) -> str:
        """Load the full SKILL.md content (on-demand, cached)."""
        if skill_name in self._content_cache:
            return self._content_cache[skill_name]
            
        skill_dir = self._skill_paths.get(skill_name)
        if not skill_dir:
            return ""
            
        md_paths = [
            skill_dir / "SKILL.md",
            skill_dir / f"{skill_dir.name}_SKILL.md",
            skill_dir / f"{skill_dir.name}-skill.md"
        ]
        
        content = ""
        for md_path in md_paths:
            if md_path.exists():
                content = md_path.read_text(encoding="utf-8")
                break
                
        if not content:
            return ""
            
        # Strip YAML frontmatter if present
        lines = content.splitlines()
        if lines and lines[0].strip() == "---":
            end_idx = -1
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    end_idx = i
                    break
            if end_idx != -1:
                content = "\n".join(lines[end_idx+1:]).strip()
                
        self._content_cache[skill_name] = content
        return content
    
    def check_requirements(self, skill_name: str, agent_tools: List[str]) -> RequirementCheck:
        """Verify the agent has the tools this skill needs."""
        manifest = self._index.get(skill_name)
        if not manifest:
            return RequirementCheck(met=False, missing_tools=[], reason="Skill not found")
        required = manifest.requirements.tools or []
        missing = [t for t in required if t not in agent_tools]
        return RequirementCheck(
            met=len(missing) == 0,
            missing_tools=missing,
            reason=f"Missing tools: {missing}" if missing else "All requirements met"
        )
    
    def get_attached_skills(self, agent_id: str, config: Optional[Any] = None) -> List[str]:
        """Return skills permanently attached to an agent (from config)."""
        if config and hasattr(config, 'agent_skills') and agent_id in config.agent_skills:
            return config.agent_skills[agent_id]
        return []
    
    def list_all(self) -> List[SkillSummary]:
        """Return summaries of all indexed skills (for UI listing)."""
        return [
            SkillSummary(
                name=m.name,
                description=m.description,
                slash_commands=m.trigger.slash_commands,
                required_tools=m.requirements.tools,
                auto_match=m.trigger.auto_match,
                source=m.source.type
            )
            for m in self._index.values()
        ]
    
    def list_skill_scripts(self, skill_name: str) -> List[Path]:
        """List executable scripts bundled with a skill (scripts/ directory)."""
        skill_dir = self._skill_paths.get(skill_name)
        if not skill_dir:
            return []
        scripts_dir = skill_dir / "scripts"
        if not scripts_dir.exists() or not scripts_dir.is_dir():
            return []
        return [f for f in scripts_dir.iterdir() if f.is_file()]
    
    def load_skill_resource(self, skill_name: str, relative_path: str) -> str:
        """Load a referenced file from within a skill directory (Stage 3 progressive disclosure).
        
        Args:
            skill_name: The indexed skill name.
            relative_path: Path relative to the skill root (e.g. 'references/REFERENCE.md').
            
        Returns:
            The file contents as a string, or empty string if not found.
        """
        skill_dir = self._skill_paths.get(skill_name)
        if not skill_dir:
            return ""
        target = (skill_dir / relative_path).resolve()
        # Security: ensure the resolved path is still inside the skill directory
        try:
            target.relative_to(skill_dir.resolve())
        except ValueError:
            logger.warning(f"Skill resource path escapes skill directory: {relative_path}")
            return ""
        if target.exists() and target.is_file():
            return target.read_text(encoding="utf-8")
        return ""

GLOBAL_SKILL_MANAGER = SkillManager()
