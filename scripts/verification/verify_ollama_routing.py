#!/usr/bin/env python3
import urllib.request
import json
import sys

URL = "http://localhost:5000/v1/chat/completions"

def send_request(model: str, prompt: str):
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.0,
        "max_tokens": 10
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        URL,
        data=data,
        headers={"Content-Type": "application/json", "Authorization": "Bearer gateway-pass"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            res_body = response.read().decode("utf-8")
            result = json.loads(res_body)
            model_returned = result.get("model", "unknown")
            text = (result["choices"][0]["message"].get("content") or "").strip()
            print(f"Request: model={model}, prompt='{prompt[:40]}...'")
            print(f"Response: model={model_returned}, text='{text[:60]}...'")
    except Exception as e:
        print(f"Request: model={model}, prompt='{prompt[:40]}...' failed/timed out as expected (API downstream might be simulated/unreachable): {e}")

def main():
    print("--- 1. Testing llm-routing-auto-ollama ---")
    # Simple prompt -> should route to agent-simple-core
    send_request("llm-routing-auto-ollama", "Write a hello world in Python")
    
    # Complex prompt -> should route to ollama-deepseek-v4-flash
    send_request("llm-routing-auto-ollama", "Implement a custom memory-efficient Trie in C++")
    
    # Reasoning prompt -> should route to ollama-deepseek-v4-pro
    send_request("llm-routing-auto-ollama", "Design a distributed pub/sub system with Valkey and describe failover states")

    print("\n--- 2. Testing llm-routing-ollama ---")
    # Simple prompt -> should route to ollama-deepseek-v4-flash
    send_request("llm-routing-ollama", "Write a hello world in Python")
    
    # Complex prompt -> should route to ollama-deepseek-v4-flash
    send_request("llm-routing-ollama", "Implement a custom memory-efficient Trie in C++")
    
    # Reasoning prompt -> should route to ollama-deepseek-v4-pro
    send_request("llm-routing-ollama", "Design a distributed pub/sub system with Valkey and describe failover states")

if __name__ == "__main__":
    main()
