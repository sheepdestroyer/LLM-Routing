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
    try:
        agentapi_path = os.path.expanduser("~/.gemini/antigravity-cli/bin/agentapi")
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
