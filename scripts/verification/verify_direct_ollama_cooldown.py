#!/usr/bin/env python3
import os
import sys
try:
    from .verification_helpers import load_litellm_key, get_triage_request_count, send_litellm_request
except ImportError:
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
    print("--- Verifying Direct llm-routing-ollama Cooldown ---")
    print(f"Using LiteLLM Master Key: {'set' if litellm_key else 'missing'}")
    
    # 1. Get initial triage request count
    count_init = get_count()
    print(f"Initial triage requests count: {count_init}")
    
    # 2. Send first request directly to llm-routing-ollama.
    # Since Ollama deepseek-v4-pro is offline/unauthorized, it will fail, which should return an error
    # to LiteLLM, triggering immediate cooldown for llm-routing-ollama.
    print("\nSending first request to llm-routing-ollama...")
    send_request("llm-routing-ollama", "Design a distributed pub/sub system with Valkey and describe failover states")
    
    # 3. Check triage requests count.
    count_after_1 = get_count()
    print(f"Triage requests count after 1st request: {count_after_1}")
    
    # 4. Send second request to llm-routing-ollama.
    # Since llm-routing-ollama cooldown is managed router-side, the request should reach the triage router
    # but be immediately rejected with an HTTP 429.
    print("\nSending second request to llm-routing-ollama (should be rejected with 429)...")
    success2, response_msg2 = send_request("llm-routing-ollama", "Design a distributed pub/sub system with Valkey and describe failover states")
    
    # 5. Check triage requests count.
    count_after_2 = get_count()
    print(f"Triage requests count after 2nd request: {count_after_2}")
    
    diff = count_after_2 - count_after_1
    
    if count_after_1 > count_init:
        print("✓ First request successfully reached the triage router.")
        if not success2 and "429" in response_msg2 and count_after_2 == count_after_1:
            print(f"✅ SUCCESS: llm-routing-ollama was successfully cooled down and LiteLLM locally blocked the second request (diff={diff}, err='{response_msg2}')!")
        else:
            print(f"❌ FAILURE: llm-routing-ollama was NOT cooled down properly! success={success2}, err='{response_msg2}', diff={diff}")
            sys.exit(1)
    else:
        print("❌ FAILURE: First request did not even reach the triage router.")
        sys.exit(1)

if __name__ == "__main__":
    main()
