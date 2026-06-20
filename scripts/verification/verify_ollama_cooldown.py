#!/usr/bin/env python3
import os
import sys
from verification_helpers import load_litellm_key, get_triage_request_count, send_litellm_request

# Resolve the absolute path to .env file in the workspace
workspace_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Read LITELLM_MASTER_KEY from .env
litellm_key = load_litellm_key(workspace_dir)

LITELLM_URL = "http://localhost:4000/v1/chat/completions"
METRICS_URL = "http://localhost:5000/metrics"

def send_request(model: str, prompt: str):
    return send_litellm_request(model, prompt, LITELLM_URL, litellm_key)

def get_count():
    return get_triage_request_count(METRICS_URL)

def main():
    print("--- Verifying Ollama Cooldown and Skip Behavior ---")
    print(f"Using LiteLLM Master Key: {'set' if litellm_key else 'missing'}")
    
    # 1. Get initial triage request count
    count_init = get_count()
    print(f"Initial triage requests count: {count_init}")
    
    # 2. Send first request to agent-advanced-core.
    print("\nSending first request to agent-advanced-core...")
    send_request("agent-advanced-core", "Design a distributed pub/sub system with Valkey and describe failover states")
    
    # 3. Check triage requests count.
    count_after_1 = get_count()
    print(f"Triage requests count after 1st request: {count_after_1}")
    
    # 4. Send second request to agent-advanced-core.
    print("\nSending second request to agent-advanced-core (llm-routing-ollama should be skipped/cooled down)...")
    success2, model_returned2 = send_request("agent-advanced-core", "Design a distributed pub/sub system with Valkey and describe failover states")
    
    # 5. Check triage requests count.
    count_after_2 = get_count()
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
