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
        print(f"agentapi binary not found at {agentapi_path}; skipping health check")
        if __name__ != "__main__":
            try:
                import pytest
                pytest.skip(f"agentapi binary not found at {agentapi_path}; skipping health check")
            except ImportError:
                pass
        return

    try:
        # Testing non-interactive new-conversation mode
        result = subprocess.run(
            [agentapi_path, "new-conversation", "--model=flash_lite", "Hello, who are you?"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True
        )
        print(f"Antigravity AgentAPI response: {result.stdout.strip()}")
        # Verify JSON contains expected fields
        resp_data = json.loads(result.stdout)
        if "response" in resp_data and "newConversation" in resp_data["response"]:
            print("Success: Antigravity-cli bridge confirmed.")
        else:
            raise ValueError(f"Unexpected response structure: {result.stdout.strip()}")
    except Exception as e:
        print(f"Failed to connect: {e}")
        raise

if __name__ == "__main__":
    test_antigravity_connection()
