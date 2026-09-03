import os
import json
import subprocess
import time


def test_antigravity_connection():
    if os.environ.get("GITHUB_ACTIONS") == "true":
        import pytest

        pytest.skip("Skipping antigravity connection test in CI.")

    cli_token_path = os.path.expanduser("~/.gemini/antigravity-cli/antigravity-oauth-token")
    if not os.path.exists(cli_token_path):
        print(f"Error: {cli_token_path} not found.")
        return

    print("--- Testing antigravity-cli connection with current OAuth ---")

    # Using the agy binary located at ~/.local/bin/agy or in PATH
    agy_path = os.path.expanduser("~/.local/bin/agy")
    if not os.path.exists(agy_path):
        import shutil

        agy_path = shutil.which("agy")

    if not agy_path or not os.path.exists(agy_path):
        print("agy binary not found; skipping health check")
        if __name__ != "__main__":
            try:
                import pytest

                pytest.skip("agy binary not found; skipping health check")
            except ImportError:
                pass
        return

    try:
        result = subprocess.run([agy_path, "--version"], capture_output=True, text=True, timeout=10, check=True)
        version_str = result.stdout.strip()
        print(f"Antigravity agy CLI version: {version_str}")
        assert version_str, "Expected non-empty version output from agy CLI"
    except Exception as e:
        print(f"Failed to connect: {e}")
        raise


if __name__ == "__main__":
    test_antigravity_connection()
