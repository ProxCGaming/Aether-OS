import os
import shutil
import zipfile
from pathlib import Path
from typing import Dict, Optional, Any
from .manifest import parse_manifest, PluginManifest

class PluginInstaller:
    def __init__(self, plugins_dir: Path):
        self.plugins_dir = plugins_dir
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.registry_file = self.plugins_dir / "registry.json"
        self._registry: Dict[str, Any] = self._load_registry()

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
        return {
            "source": source,
            "manifest": manifest.model_dump()
        }

    def install(self, prepare_data: dict):
        source = Path(prepare_data["source"])
        manifest = PluginManifest(**prepare_data["manifest"])
        plugin_name = manifest.name
        
        target_dir = self.plugins_dir / plugin_name
        
        # Rollback mechanism
        backup = None
        if target_dir.exists():
            backup = self.plugins_dir / f"{plugin_name}_backup"
            if backup.exists():
                shutil.rmtree(backup)
            shutil.move(str(target_dir), str(backup))

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

            self._registry[plugin_name] = {
                "manifest": manifest.model_dump(),
                "enabled": True
            }
            self._save_registry()
            
            if backup and backup.exists():
                shutil.rmtree(backup)
                
        except Exception as e:
            # Rollback
            if target_dir.exists():
                shutil.rmtree(target_dir)
            if backup and backup.exists():
                shutil.move(str(backup), str(target_dir))
            raise RuntimeError(f"Installation failed, rolled back. Error: {e}")

    def uninstall(self, plugin_name: str):
        if plugin_name in self._registry:
            del self._registry[plugin_name]
            self._save_registry()
            
        target_dir = self.plugins_dir / plugin_name
        if target_dir.exists():
            shutil.rmtree(target_dir)

    def toggle(self, plugin_name: str, enabled: bool):
        if plugin_name in self._registry:
            self._registry[plugin_name]["enabled"] = enabled
            self._save_registry()
        else:
            raise KeyError(f"Plugin not found: {plugin_name}")

    def get_all(self) -> Dict[str, Any]:
        return self._registry
