import getpass
import sys
from pathlib import Path

# Ensure root workspace directory is in python path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aether_engine.secrets.storage import SecretStore


def main():
    print("=== AETHER Secrets Manager: Google Gemini API Key ===")
    print("This script will securely encrypt and save your Gemini API key using Windows DPAPI.")
    print("Target Database: ~/.aether/secrets.db\n")

    if len(sys.argv) > 1:
        key = sys.argv[1].strip()
    else:
        try:
            key = getpass.getpass("Enter your Google AI Studio Gemini API Key: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(1)

    if not key:
        print("Error: API key cannot be empty.")
        sys.exit(1)

    try:
        store = SecretStore()
        store.save_provider("google_gemini", key)
        # Verify read
        loaded = store.load_provider("google_gemini")
        if loaded == key:
            print("\n[SUCCESS] Google Gemini API key has been encrypted with Windows DPAPI and stored successfully!")
            print(f"Stored providers: {store.list_providers()}")
        else:
            print("\n[ERROR] Key verification mismatch after write.")
            sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] Failed to store API key: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
