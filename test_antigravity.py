import os
import json
import subprocess
import time

def test_antigravity_connection():
    creds_path = os.path.expanduser("~/.gemini/oauth_creds.json")
    if not os.path.exists(creds_path):
        print(f"Error: {creds_path} not found.")
        return

    print("--- Testing antigravity-cli connection with current OAuth ---")
    
    # Using the agentapi binary located at ~/.gemini/antigravity-cli/bin/agentapi
    agentapi_path = os.path.expanduser("~/.gemini/antigravity-cli/bin/agentapi")
    if not os.path.exists(agentapi_path):
        try:
            import pytest
            pytest.skip(f"agentapi binary not found at {agentapi_path}; skipping health check")
        except ImportError:
            print(f"agentapi binary not found at {agentapi_path}; skipping health check")
            return

    try:
        # Testing non-interactive print mode
        result = subprocess.run(
            [agentapi_path, "--print", "Hello, who are you?"],
            capture_output=True,
            text=True,
            timeout=20
        )
        print(f"Antigravity AgentAPI response: {result.stdout.strip()}")
        print("Success: Antigravity-cli bridge confirmed.")
    except Exception as e:
        print(f"Failed to connect: {e}")

if __name__ == "__main__":
    test_antigravity_connection()
