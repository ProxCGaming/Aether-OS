import json
import tempfile
import unittest
from pathlib import Path

from aether_engine.config import (
    CLOUD_PROVIDERS,
    LOCAL_MODELS,
    PROVIDER_MAP,
    UserConfig,
    load_config,
    save_config,
)


class TestProviderConfig(unittest.TestCase):
    def test_cloud_providers_registry(self):
        self.assertEqual(len(CLOUD_PROVIDERS), 6)
        names = [p.name for p in CLOUD_PROVIDERS]
        self.assertIn("google_gemini", names)
        self.assertIn("openai", names)
        self.assertIn("anthropic", names)
        self.assertIn("deepseek", names)
        self.assertIn("openrouter", names)
        self.assertIn("custom_openai", names)

    def test_provider_info_attributes(self):
        gemini = PROVIDER_MAP["google_gemini"]
        self.assertEqual(gemini.name, "google_gemini")
        self.assertEqual(gemini.display_name, "Google Gemini")
        self.assertTrue(gemini.key_url.startswith("https://"))

    def test_provider_map_matches_list(self):
        for p in CLOUD_PROVIDERS:
            self.assertIn(p.name, PROVIDER_MAP)
            self.assertIs(PROVIDER_MAP[p.name], p)

    def test_local_models_static_list(self):
        self.assertEqual(len(LOCAL_MODELS), 4)
        names = [m["name"] for m in LOCAL_MODELS]
        self.assertIn("Qwen 3 8B", names)
        self.assertIn("Llama 3.1 8B", names)

    def test_user_config_roundtrip(self):
        cfg = UserConfig(
            default_provider="openai",
            default_model="gpt-4o",
            provider_models={"openai": ["gpt-4o", "gpt-4o-mini"]},
        )
        d = cfg.to_dict()
        restored = UserConfig.from_dict(d)
        self.assertEqual(restored.default_provider, "openai")
        self.assertEqual(restored.default_model, "gpt-4o")
        self.assertEqual(restored.provider_models["openai"], ["gpt-4o", "gpt-4o-mini"])

    def test_user_config_defaults(self):
        cfg = UserConfig()
        self.assertEqual(cfg.default_provider, "google_gemini")
        self.assertEqual(cfg.default_model, "gemini-2.0-flash")
        self.assertEqual(cfg.provider_models, {})

    def test_save_and_load_config(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test_config.json"
            cfg = UserConfig(default_provider="anthropic", default_model="claude-4")
            save_config(cfg, path)

            loaded = load_config(path)
            self.assertEqual(loaded.default_provider, "anthropic")
            self.assertEqual(loaded.default_model, "claude-4")

    def test_load_config_missing_file(self):
        cfg = load_config(Path("/nonexistent/config.json"))
        self.assertEqual(cfg.default_provider, "google_gemini")

    def test_load_config_corrupt_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "corrupt.json"
            path.write_text("not valid json {[", encoding="utf-8")
            cfg = load_config(path)
            self.assertEqual(cfg.default_provider, "google_gemini")


if __name__ == "__main__":
    unittest.main()
