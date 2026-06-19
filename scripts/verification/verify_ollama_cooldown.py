#!/usr/bin/env python3
import urllib.request
import json
import time
import os

# Resolve the absolute path to .env file in the workspace
workspace_dir = "/home/gpav/.gemini/antigravity/worktrees/LLM-Routing/finalize-pr-two-review"
env_path = os.path.join(workspace_dir, ".env")

# Read LITELLM_MASTER_KEY from .env
litellm_key = "gateway-pass"
if os.path.exists(env_path):
    with open(env_path, "r") as f:
        for line in f:
            if line.startswith("LITELLM_MASTER_KEY="):
                # extract value inside quotes
                litellm_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

LITELLM_URL = "http://localhost:4000/v1/chat/completions"
METRICS_URL = "http://localhost:5000/metrics"

def get_triage_request_count():
    try:
        with urllib.request.urlopen(METRICS_URL, timeout=5) as response:
            lines = response.read().decode("utf-8").splitlines()
            for line in lines:
                if line.startswith("triage_requests_total"):
                    return int(line.split()[1])
    except Exception as e:
        print(f"Error fetching metrics: {e}")
    return 0

def send_litellm_request(model: str, prompt: str):
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
        LITELLM_URL,
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {litellm_key}"}
    )
    start_time = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            res_body = response.read().decode("utf-8")
            result = json.loads(res_body)
            model_returned = result.get("model", "unknown")
            text = (result["choices"][0]["message"].get("content") or "").strip()
            print(f"Success in {time.time() - start_time:.1f}s: model={model_returned}, text='{text[:40]}'")
            return True, model_returned
    except Exception as e:
        # Check if the error body is readable
        err_msg = str(e)
        if hasattr(e, "read"):
            try:
                err_msg += " - " + e.read().decode("utf-8")
            except Exception:
                pass
        print(f"Failed in {time.time() - start_time:.1f}s: {err_msg}")
        return False, err_msg

def main():
    print("--- Verifying Ollama Cooldown and Skip Behavior ---")
    print(f"Using LiteLLM Master Key: {litellm_key[:10]}...")
    
    # 1. Get initial triage request count
    count_init = get_triage_request_count()
    print(f"Initial triage requests count: {count_init}")
    
    # 2. Send first request to agent-advanced-core.
    print("\nSending first request to agent-advanced-core...")
    send_litellm_request("agent-advanced-core", "Design a distributed pub/sub system with Valkey and describe failover states")
    
    # 3. Check triage requests count.
    count_after_1 = get_triage_request_count()
    print(f"Triage requests count after 1st request: {count_after_1}")
    
    # 4. Send second request to agent-advanced-core.
    print("\nSending second request to agent-advanced-core (llm-routing-ollama should be skipped)...")
    send_litellm_request("agent-advanced-core", "Design a distributed pub/sub system with Valkey and describe failover states")
    
    # 5. Check triage requests count.
    count_after_2 = get_triage_request_count()
    print(f"Triage requests count after 2nd request: {count_after_2}")
    
    diff = count_after_2 - count_after_1
    
    # Verify by checking if the count incremented on the first request and stayed constant on the second
    if count_after_1 > count_init:
        print("✓ First request successfully reached the triage router via fallback!")
        if diff == 0:
            print("✅ SUCCESS: llm-routing-ollama was successfully skipped (cooled down) on the second request!")
        else:
            print(f"❌ FAILURE: llm-routing-ollama was NOT skipped (count increased by {diff})!")
    else:
        print("❌ FAILURE: First request did not even reach the triage router (check if all free models failed immediately without fallback).")

if __name__ == "__main__":
    main()
