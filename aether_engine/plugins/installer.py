import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from .manifest import parse_manifest, PluginManifest


def _read_mcp_config_file(source: Path, file_path: str) -> dict:
    """Read one MCP server config JSON from a plugin package (zip or dir).

    Raises ValueError with a named message when the file is missing,
    is not valid JSON, or does not contain a JSON object. Never invents
    a fallback config: a corrupt config is a validation failure, not a
    dummy server.

    Issue 011 (S4): *file_path* must stay inside the package. An
    absolute path or any ``..`` segment is rejected outright, and in
    the dir branch the resolved path must stay inside the resolved
    package directory — a crafted manifest must not read an arbitrary
    JSON file on disk as a server config (with an arbitrary
    ``command``). The same guard runs before the zip branch, where a
    ``..`` entry name would otherwise be looked up verbatim.
    """
    path = Path(file_path)
    if path.is_absolute():
        raise ValueError(
            f"MCP server config file '{file_path}' must be inside the "
            "plugin package: absolute paths are not allowed."
        )
    if ".." in path.parts:
        raise ValueError(
            f"MCP server config file '{file_path}' must be inside the "
            "plugin package: '..' segments are not allowed."
        )
    raw: bytes
    if source.is_file() and source.suffix == ".zip":
        try:
            with zipfile.ZipFile(source, "r") as z:
                try:
                    raw = z.read(file_path)
                except KeyError:
                    raise ValueError(
                        f"MCP server config file '{file_path}' not found "
                        "in plugin package."
                    )
        except zipfile.BadZipFile as e:
            raise ValueError(f"Plugin package is not a valid zip file: {e}")
    elif source.is_dir():
        cfg_path = (source / file_path).resolve()
        if not cfg_path.is_relative_to(source.resolve()):
            raise ValueError(
                f"MCP server config file '{file_path}' must be inside the "
                "plugin package."
            )
        if not cfg_path.exists():
            raise ValueError(
                f"MCP server config file '{file_path}' not found "
                "in plugin package."
            )
        raw = cfg_path.read_bytes()
    else:
        raise ValueError(f"Plugin source not found: {source}")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(
            f"MCP server config file '{file_path}' is not valid JSON: {e}"
        )
    if not isinstance(data, dict):
        raise ValueError(
            f"MCP server config file '{file_path}' must contain a JSON object."
        )
    return data


def resolve_mcp_server_configs(source: Any, manifest: PluginManifest) -> List[Tuple[str, dict]]:
    """Load + validate every MCP server a manifest declares.

    Validate first, add second: this returns the *normalised* configs
    (same schema as issue 001, ``validate_mcp_server_config``) and
    raises before the caller mutates anything — no file is copied, no
    skill installed, no server added — when any entry is invalid.
    Raises ValueError / MCPConfigError with a named message.
    """
    from aether_engine.mcp.registry import validate_mcp_server_config

    src_path = Path(source)
    resolved: List[Tuple[str, dict]] = []
    for mcp_def in manifest.mcp_servers:
        if not mcp_def.file_path:
            raise ValueError(
                f"MCP server {mcp_def.name!r} declares no config file "
                "(file_path): a plugin MCP server needs a JSON config "
                "inside the package."
            )
        raw_config = _read_mcp_config_file(src_path, mcp_def.file_path)
        resolved.append(
            (mcp_def.name, validate_mcp_server_config(mcp_def.name, raw_config))
        )
    return resolved


class PluginInstaller:
    def __init__(self, plugins_dir: Path):
        self.plugins_dir = plugins_dir
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.registry_file = self.plugins_dir / "registry.json"
        self._registry: Dict[str, Any] = self._load_registry()
        self.pending_installs: Dict[str, dict] = {}

    def _load_registry(self) -> Dict[str, Any]:
        if self.registry_file.exists():
            import json
            try:
                return json.loads(self.registry_file.read_text())
            except Exception:
                return {}
        return {}

    def _save_registry(self):
        import json
        self.registry_file.write_text(json.dumps(self._registry, indent=2))

    def prepare_install(self, source: str) -> dict:
        """Parses manifest from source (zip or dir) and returns prepare data."""
        src_path = Path(source)
        if not src_path.exists():
            raise FileNotFoundError(f"Source not found: {source}")

        manifest_data = None
        if src_path.is_file() and src_path.suffix == ".zip":
            with zipfile.ZipFile(src_path, 'r') as z:
                # Find manifest.json
                for info in z.infolist():
                    if info.filename.endswith("manifest.json"):
                        import json
                        manifest_data = json.loads(z.read(info.filename).decode())
                        break
        elif src_path.is_dir():
            m_path = src_path / "manifest.json"
            if m_path.exists():
                import json
                manifest_data = json.loads(m_path.read_text(encoding="utf-8"))

        if not manifest_data:
            raise ValueError("Invalid plugin package: manifest.json not found")

        manifest = PluginManifest(**manifest_data)
        # Issue 008: fail fast, before the approval step — a manifest
        # whose MCP servers do not validate is rejected here with the
        # named error, not after the user approves the install.
        resolve_mcp_server_configs(src_path, manifest)
        return {
            "source": source,
            "manifest": manifest.model_dump()
        }

    def install(self, prepare_data: dict):
        source = Path(prepare_data["source"])
        manifest = PluginManifest(**prepare_data["manifest"])
        plugin_name = manifest.name

        # Issue 008: validate FIRST, before any mutation — before the
        # backup move, before any file is copied, before any server is
        # added. A bad manifest fails here with the named error and the
        # previous install (if any) is left untouched.
        try:
            mcp_configs = resolve_mcp_server_configs(source, manifest)
        except Exception as e:
            raise RuntimeError(f"Installation failed, rolled back. Error: {e}")

        target_dir = self.plugins_dir / plugin_name
        
        # Rollback mechanism
        backup = None
        if target_dir.exists():
            backup = self.plugins_dir / f"{plugin_name}_backup"
            if backup.exists():
                shutil.rmtree(backup)
            shutil.move(str(target_dir), str(backup))

        installed_skills = []
        installed_tools = []
        installed_mcp = []
        
        from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
        
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            if source.is_file() and source.suffix == ".zip":
                with zipfile.ZipFile(source, 'r') as z:
                    z.extractall(target_dir)
            elif source.is_dir():
                # Copy contents
                for item in source.iterdir():
                    s = item
                    d = target_dir / item.name
                    if s.is_dir():
                        shutil.copytree(s, d)
                    else:
                        shutil.copy2(s, d)

            # 1. Install Skills
            skills_dest = Path.home() / ".aether" / "skills"
            skills_dest.mkdir(parents=True, exist_ok=True)
            for skill in manifest.skills:
                if skill.file_path:
                    src_skill = target_dir / skill.file_path
                    if src_skill.exists():
                        dest_skill_dir = skills_dest / skill.name
                        dest_skill_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_skill, dest_skill_dir / "SKILL.md")
                        installed_skills.append(dest_skill_dir)
                        
            # 2. Record Tools (no global registry)
            for tool_def in manifest.tools:
                installed_tools.append(tool_def.name)
                
            # 3. Install MCP Servers — configs were already loaded and
            # validated (issue 008) before any mutation above, so this
            # only adds the pre-validated configs. No re-reading, no
            # dummy fallback: a corrupt config can never become a
            # silently installed server.
            for mcp_name, config_dict in mcp_configs:
                GLOBAL_MCP_REGISTRY.add_server(mcp_name, config_dict)
                installed_mcp.append(mcp_name)

            self._registry[plugin_name] = {
                "manifest": manifest.model_dump(),
                "enabled": True,
                "installed_skills": [str(p) for p in installed_skills],
                "installed_tools": installed_tools,
                "installed_mcp": installed_mcp
            }
            self._save_registry()
            
            if backup and backup.exists():
                shutil.rmtree(backup)
                
        except Exception as e:
            # Atomic Rollback
            if target_dir.exists():
                shutil.rmtree(target_dir)
            if backup and backup.exists():
                shutil.move(str(backup), str(target_dir))
                
            for skill_dir in installed_skills:
                if skill_dir.exists():
                    shutil.rmtree(skill_dir)
            for mcp_name in installed_mcp:
                # Issue 011 (S6): a connected server refuses a sync
                # remove — that refusal must not mask the install
                # failure being rolled back, so it is best-effort here.
                try:
                    GLOBAL_MCP_REGISTRY.remove_server_sync(mcp_name)
                except Exception:
                    pass

            raise RuntimeError(f"Installation failed, rolled back. Error: {e}")

    def uninstall(self, plugin_name: str):
        if plugin_name not in self._registry:
            return
            
        data = self._registry[plugin_name]
        
        # Remove MCP
        from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
        for mcp_name in data.get("installed_mcp", []):
            GLOBAL_MCP_REGISTRY.remove_server_sync(mcp_name)
            
        # Remove skills
        for skill_path_str in data.get("installed_skills", []):
            skill_dir = Path(skill_path_str)
            if skill_dir.exists():
                shutil.rmtree(skill_dir)
                
        # Remove plugin files
        target_dir = self.plugins_dir / plugin_name
        if target_dir.exists():
            shutil.rmtree(target_dir)
            
        del self._registry[plugin_name]
        self._save_registry()

    def toggle(self, plugin_name: str, enabled: bool):
        if plugin_name in self._registry:
            self._registry[plugin_name]["enabled"] = enabled
            self._save_registry()
            
            data = self._registry[plugin_name]
            # Since toggle doesn't uninstall, if disabled we might want to unregister tools temporarily.
            # But the prompt says "same underlying mechanism as disabling an individual tool already does".
            # For now, toggling is just state update, and the engine logic should check enabled state.
        else:
            raise KeyError(f"Plugin not found: {plugin_name}")

    def get_all(self) -> Dict[str, Any]:
        return self._registry
