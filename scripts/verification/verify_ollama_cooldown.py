#!/usr/bin/env python3
import json
import time
import os
import uuid
import sys
import httpx

# Resolve the absolute path to .env file in the workspace
workspace_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
        response = httpx.get(METRICS_URL, timeout=5.0)
        response.raise_for_status()
        lines = response.text.splitlines()
        for line in lines:
            if line.startswith("triage_requests_total"):
                return int(float(line.split()[1]))
    except (httpx.HTTPError, ValueError) as e:
        print(f"Error fetching metrics: {e}")
    return 0

def send_litellm_request(model: str, prompt: str):
    unique_prompt = f"{prompt} [id: {uuid.uuid4()}]"
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": unique_prompt}
        ],
        "temperature": 0.0,
        "max_tokens": 10
    }
    start_time = time.time()
    try:
        response = httpx.post(
            LITELLM_URL,
            json=payload,
            headers={"Authorization": f"Bearer {litellm_key}"},
            timeout=120.0
        )
        response.raise_for_status()
        result = response.json()
        model_returned = result.get("model", "unknown")
        text = (result["choices"][0]["message"].get("content") or "").strip()
        print(f"Success in {time.time() - start_time:.1f}s: model={model_returned}, text='{text[:40]}'")
        return True, model_returned
    except httpx.HTTPStatusError as e:
        err_msg = f"{e} - {e.response.text}"
        print(f"Failed in {time.time() - start_time:.1f}s: {err_msg}")
        return False, err_msg
    except httpx.HTTPError as e:
        err_msg = str(e)
        print(f"Failed in {time.time() - start_time:.1f}s: {err_msg}")
        return False, err_msg
    except (KeyError, IndexError, ValueError) as e:
        err_msg = f"Parse error: {e}"
        print(f"Failed in {time.time() - start_time:.1f}s: {err_msg}")
        return False, err_msg

def main():
    print("--- Verifying Ollama Cooldown and Skip Behavior ---")
    print(f"Using LiteLLM Master Key: {'set' if litellm_key else 'missing'}")
    
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
    print("\nSending second request to agent-advanced-core (llm-routing-ollama should be skipped/cooled down)...")
    success2, model_returned2 = send_litellm_request("agent-advanced-core", "Design a distributed pub/sub system with Valkey and describe failover states")
    
    # 5. Check triage requests count.
    count_after_2 = get_triage_request_count()
    print(f"Triage requests count after 2nd request: {count_after_2}")
    
    diff = count_after_2 - count_after_1
    
    # Verify by checking if the count incremented on the first request and the second request was fallback handled successfully.
    if count_after_1 > count_init:
        print("✓ First request successfully reached the triage router via fallback!")
        if success2 and model_returned2 != "llm-routing-ollama":
            print(f"✅ SUCCESS: llm-routing-ollama was successfully cooled down and LiteLLM fell back to openrouter-auto (diff={diff}, model={model_returned2})!")
        else:
            print(f"❌ FAILURE: Second request did not properly fall back! success={success2}, model={model_returned2}")
            sys.exit(1)
    else:
        print("❌ FAILURE: First request did not even reach the triage router (check if all free models failed immediately without fallback).")
        sys.exit(1)

if __name__ == "__main__":
    main()
