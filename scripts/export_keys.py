"""Export stored API keys from the DPAPI-encrypted secrets.db to a separate
plaintext JSON file for backup or transfer purposes.

Output:  ~/.aether/exported_keys.json

WARNING: The exported file contains plaintext API keys.
         Store it securely and never commit it to version control.
"""
import json
import sys
from pathlib import Path

# Ensure root workspace directory is in python path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aether_engine.secrets.storage import SecretStore


EXPORT_PATH = Path.home() / ".aether" / "exported_keys.json"


def main():
    print("=" * 56)
    print("  AETHER Secrets Exporter")
    print("=" * 56)
    print()
    print(f"Source DB   : ~/.aether/secrets.db")
    print(f"Export File : {EXPORT_PATH}")
    print()

    store = SecretStore()
    providers = store.list_providers()

    if not providers:
        print("[INFO] No providers found in the secret store. Nothing to export.")
        sys.exit(0)

    print(f"Found {len(providers)} provider(s): {', '.join(providers)}\n")

    exported = {}
    for name in providers:
        try:
            key = store.load_provider(name)
            exported[name] = key
            # Show only first 6 and last 4 chars for safety in terminal
            masked = key[:6] + "…" + key[-4:] if len(key) > 12 else "****"
            print(f"  ✔ {name:25s} → {masked}")
        except Exception as e:
            print(f"  ✖ {name:25s} → ERROR: {e}")

    if not exported:
        print("\n[ERROR] No keys could be decrypted. Nothing exported.")
        sys.exit(1)

    # Write export file
    EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXPORT_PATH.write_text(json.dumps(exported, indent=2), encoding="utf-8")

    print()
    print(f"[SUCCESS] Exported {len(exported)} key(s) to:")
    print(f"          {EXPORT_PATH}")
    print()
    print("⚠  WARNING: This file contains PLAINTEXT API keys.")
    print("   Do NOT commit it to version control or share it publicly.")
    print("   Add it to .gitignore if it is inside a repository.")


if __name__ == "__main__":
    main()
