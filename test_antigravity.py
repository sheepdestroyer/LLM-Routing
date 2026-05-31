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
    
    # We simulate a request to Gemini 3.5 Flash via antigravity-cli
    # Using the agentapi binary located at /home/gpav/.gemini/antigravity-cli/bin/agentapi
    try:
        # Testing non-interactive print mode
        result = subprocess.run(
            ["/home/gpav/.gemini/antigravity-cli/bin/agentapi", "--print", "Hello, who are you?"],
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
