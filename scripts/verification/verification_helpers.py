# Shared verification helpers for cooldown and routing tests
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts.chat_helpers import parse_chat_response
import os
import uuid
import time
import httpx

def load_litellm_key(workspace_dir: str) -> str:
    """Read LITELLM_MASTER_KEY from .env, defaulting to 'gateway-pass'."""
    env_path = os.path.join(workspace_dir, ".env")
    litellm_key = "gateway-pass"
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if line.startswith("LITELLM_MASTER_KEY="):
                    # extract value inside quotes
                    litellm_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    return litellm_key

def get_triage_request_count(metrics_url: str = "http://localhost:5000/metrics") -> int:
    try:
        response = httpx.get(metrics_url, timeout=5.0)
        response.raise_for_status()
        lines = response.text.splitlines()
        total_count = 0
        found = False
        for line in lines:
            if line.startswith("triage_requests_total"):
                total_count += int(float(line.split()[1]))
                found = True
        if found:
            return total_count
    except (httpx.HTTPError, ValueError) as e:
        print(f"Error fetching metrics: {e}")
    return 0

def send_litellm_request(model: str, prompt: str, litellm_url: str = "http://localhost:4000/v1/chat/completions", litellm_key: str = "gateway-pass") -> tuple[bool, str]:
    """Send a request to LiteLLM and return (success_bool, result_model_or_error_msg)."""
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
            litellm_url,
            json=payload,
            headers={"Authorization": f"Bearer {litellm_key}"},
            timeout=120.0
        )
        response.raise_for_status()
        result = response.json()
        model_returned = result.get("model", "unknown")
        text = parse_chat_response(result)[0]
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
