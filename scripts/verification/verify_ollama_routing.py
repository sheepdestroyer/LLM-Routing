#!/usr/bin/env python3
import json
import sys
import os
import httpx

URL = "http://localhost:5000/v1/chat/completions"

# Resolve the absolute path to .env file in the workspace
workspace_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from verification_helpers import load_litellm_key

# Read LITELLM_MASTER_KEY from .env
litellm_key = load_litellm_key(workspace_dir)

def send_request(model: str, prompt: str, expected_model: str):
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.0,
        "max_tokens": 10
    }
    try:
        response = httpx.post(
            URL,
            json=payload,
            headers={"Authorization": f"Bearer {litellm_key}"},
            timeout=30.0
        )
        response.raise_for_status()
        result = response.json()
        model_returned = result.get("model", "unknown")
        text = (result["choices"][0]["message"].get("content") or "").strip()
        print(f"Request: model={model}, prompt='{prompt[:40]}...'")
        print(f"Response: model={model_returned}, text='{text[:60]}...'")
        if model_returned != expected_model:
            print(f"❌ FAILURE: Expected model '{expected_model}', but got '{model_returned}'", file=sys.stderr)
            sys.exit(1)
        print("✓ SUCCESS: Routed correctly!")
    except httpx.HTTPStatusError as e:
        print(f"❌ HTTP ERROR: Request to model={model} failed with status {e.response.status_code}: {e}\nResponse body:\n{e.response.text}", file=sys.stderr)
        sys.exit(1)
    except httpx.HTTPError as e:
        print(f"❌ HTTP ERROR: Request to model={model} failed: {e}", file=sys.stderr)
        sys.exit(1)
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"❌ PARSE ERROR: Failed to parse response for model={model}: {e}", file=sys.stderr)
        sys.exit(1)

def main():
    print("--- 1. Testing llm-routing-auto-ollama ---")
    # Simple prompt -> should route to agent-simple-core
    send_request("llm-routing-auto-ollama", "Write a hello world in Python", "agent-simple-core")
    
    # Complex prompt -> should route to ollama-deepseek-v4-flash
    send_request("llm-routing-auto-ollama", "Implement a custom memory-efficient Trie in C++", "ollama-deepseek-v4-flash")
    
    # Reasoning prompt -> should route to ollama-deepseek-v4-pro
    send_request("llm-routing-auto-ollama", "Design a distributed pub/sub system with Valkey and describe failover states", "ollama-deepseek-v4-pro")

    print("\n--- 2. Testing llm-routing-ollama ---")
    # Simple prompt -> should route to ollama-deepseek-v4-flash
    send_request("llm-routing-ollama", "Write a hello world in Python", "ollama-deepseek-v4-flash")
    
    # Complex prompt -> should route to ollama-deepseek-v4-flash
    send_request("llm-routing-ollama", "Implement a custom memory-efficient Trie in C++", "ollama-deepseek-v4-flash")
    
    # Reasoning prompt -> should route to ollama-deepseek-v4-pro
    send_request("llm-routing-ollama", "Design a distributed pub/sub system with Valkey and describe failover states", "ollama-deepseek-v4-pro")

if __name__ == "__main__":
    main()
