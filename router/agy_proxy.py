"""
agy Proxy Module — 3-tier fallback via antigravity CLI with session preservation

Integrates with router/main.py to delegate complex tasks to agy --print
with automatic model fallback and conversation continuation.

Session Architecture:
  - First call in session: agy --print "prompt" → creates conversation
  - Tier switch within session: agy --conversation <id> --print "prompt" with new model override
  - Subsequent calls: agy --conversation <id> --print "next prompt" (same model tier)
  
  The conversation ID is tracked per router session and persisted across calls.
  When a tier switch is needed (quota), the SAME conversation is continued with
  a different model backend — preserving full context.

Fallback Tiers (same conversation, different model):
  Tier 1: Default → Gemini 3.5 Flash  (Cloud Code Assist quota)
  Tier 2: claude-opus-4-6@default     (premium tier)
  Fallback: Existing LiteLLM chain (OpenRouter free → local Qwen)
"""

import asyncio
import json
import logging
import os
import shlex
import time
from typing import Optional

logger = logging.getLogger("agy-proxy")

# In container: mounted from host /home/gpav/.local/bin/agy
AGY_BINARY = os.environ.get("AGY_BINARY_PATH", "/usr/local/bin/agy")
if not os.path.exists(AGY_BINARY):
    AGY_BINARY = os.path.expanduser("~/.local/bin/agy")
if not os.path.exists(AGY_BINARY):
    AGY_BINARY = "agy"

# Cache file for conversation IDs (mapping workspace → conversation ID)
LAST_CONVERSATIONS_PATH = os.path.expanduser(
    "~/.gemini/antigravity-cli/cache/last_conversations.json"
)
AGY_WORKSPACE = os.environ.get("AGY_WORKSPACE", os.getcwd())

# Ordered fallback tiers
AGY_FALLBACK_TIERS = [
    {"model_name": "gemini-3.5-flash",  "env_override": ""},                             # Tier 1: default
    {"model_name": "claude-opus-4.6",   "env_override": "claude-opus-4-6@default"},      # Tier 2
]

AGY_TIMEOUT_SECS = 120
AGY_TOTAL_TIMEOUT_SECS = 300

# In-memory session store: {router_session_id: agy_conversation_data}
# agy_conversation_data = {"conversation_id": str, "current_tier_index": int}
_session_store: dict = {}


def _get_last_conversation_id() -> Optional[str]:
    """Read the last conversation ID for our workspace from agy's cache file."""
    try:
        if os.path.exists(LAST_CONVERSATIONS_PATH):
            with open(LAST_CONVERSATIONS_PATH, "r") as f:
                data = json.load(f)
            conv_id = data.get(AGY_WORKSPACE)
            if conv_id:
                logger.debug(f"agy session: last conversation for workspace = {conv_id}")
                return conv_id
    except Exception as e:
        logger.debug(f"agy session: failed to read last_conversations.json: {e}")
    return None


async def _run_agy_print(prompt: str, model_override: str = "",
                         conversation_id: Optional[str] = None,
                         timeout: float = AGY_TIMEOUT_SECS) -> tuple[int, str, str, Optional[str]]:
    """
    Forward the agy execution request to the host-side agy daemon.
    """
    import httpx
    
    url = "http://127.0.0.1:5005/run"
    payload = {
        "prompt": prompt,
        "model_override": model_override,
        "conversation_id": conversation_id,
        "timeout": timeout
    }
    
    model_tag = model_override if model_override else "default (gemini-3.5-flash)"
    conv_tag = f" (continuing {conversation_id[:8]}...)" if conversation_id else " (new)"
    logger.info(f"agy proxy forwarding to host: [{model_tag}]{conv_tag} {prompt[:60]}...")
    
    try:
        async with httpx.AsyncClient(timeout=timeout + 5.0) as client:
            r = await client.post(url, json=payload)
            if r.status_code == 200:
                result = r.json()
                return (
                    result.get("returncode", 0),
                    result.get("stdout", ""),
                    result.get("stderr", ""),
                    result.get("conversation_id", None)
                )
            else:
                return -1, "", f"Daemon returned HTTP status {r.status_code}", None
    except Exception as e:
        logger.error(f"Failed to communicate with Host agy Daemon: {e}")
        return -1, "", f"Daemon connection error: {e}", None


# Track the last log check time to avoid hammering the file
_last_log_check: float = 0

def _is_quota_exhausted(returncode: int, stdout: str, stderr: str) -> bool:
    """
    Detect quota exhaustion from agy subprocess results.
    
    agy returns:
      rc=0, stdout="", stderr="" → quota exhausted (error goes to cli.log)
      rc=0, stdout="response"    → success
      rc!=0                       → other error
    """
    # Direct stderr check
    if any(marker in stderr for marker in [
        "RESOURCE_EXHAUSTED", "code 429", "quota reached", "rate limit"
    ]):
        return True
    
    # agy returns rc=0 with empty stdout when quota is exhausted
    # The error is written to cli.log, not stderr
    if returncode == 0 and not stdout and not stderr:
        global _last_log_check
        now = time.time()
        if now - _last_log_check > 2.0:  # throttle: check log at most every 2s
            _last_log_check = now
            log_path = os.path.expanduser("~/.gemini/antigravity-cli/cli.log")
            try:
                if os.path.exists(log_path):
                    with open(log_path, "r") as f:
                        for line in f.readlines()[-5:]:
                            if "RESOURCE_EXHAUSTED" in line or "code 429" in line:
                                return True
            except Exception:
                pass
        # Empty stdout+stderr with rc=0 strongly suggests quota exhaustion
        return True
    
    return False


def _wrap_response(text: str, model_name: str, prompt: str) -> dict:
    """Wrap agy text output into OpenAI-compatible chat completion format."""
    prompt_tokens = len(prompt) // 4
    completion_tokens = len(text) // 4
    return {
        "id": "chatcmpl-agy-proxy",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": f"{model_name} (via agy)",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


async def try_agy_proxy(prompt: str, messages: list = None,
                        session_id: str = None,
                        total_timeout: float = AGY_TOTAL_TIMEOUT_SECS) -> Optional[dict]:
    """
    Attempt agy proxy with session-aware tier fallback.
    
    Args:
        prompt: Current user prompt
        messages: Full message history for context
        session_id: Router session identifier for conversation continuity
        total_timeout: Max total time across all tiers
    
    Returns:
        OpenAI-compatible response dict, or None if all tiers failed.
    """
    # Build context-aware prompt from message history
    proxy_prompt = prompt
    if messages:
        context_parts = []
        for msg in messages[-10:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                context_parts.append(f"User: {content}")
            elif role == "assistant":
                context_parts.append(f"Assistant: {content}")
        proxy_prompt = "\n".join(context_parts[-6:])

    # Check if we have an existing session with a conversation ID
    existing_conv_id = None
    start_tier_index = 0
    if session_id and session_id in _session_store:
        session = _session_store[session_id]
        existing_conv_id = session.get("conversation_id")
        start_tier_index = session.get("current_tier_index", 0)
        logger.info(f"agy proxy: resuming session {session_id[:8]}..., "
                    f"conversation={existing_conv_id[:8]}...")
    
    start_time = time.time()
    last_conv_id = existing_conv_id
    
    for tier_idx, tier in enumerate(AGY_FALLBACK_TIERS[start_tier_index:]):
        actual_tier_idx = start_tier_index + tier_idx
        elapsed = time.time() - start_time
        remaining = total_timeout - elapsed
        if remaining <= 0:
            logger.warning(f"agy proxy: total timeout exhausted at tier {tier['model_name']}")
            break

        tier_timeout = min(AGY_TIMEOUT_SECS, remaining)
        
        returncode, stdout, stderr, result_conv_id = await _run_agy_print(
            proxy_prompt,
            model_override=tier["env_override"],
            conversation_id=last_conv_id if actual_tier_idx > 0 or existing_conv_id else None,
            timeout=tier_timeout,
        )

        # Update the conversation ID from the result
        if result_conv_id:
            last_conv_id = result_conv_id

        # Check for quota exhaustion
        if _is_quota_exhausted(returncode, stdout, stderr):
            logger.warning(
                f"agy proxy: tier {tier['model_name']} quota exhausted. "
                f"Falling to tier {actual_tier_idx + 2}..."
            )
            continue

        # Check for other errors
        if returncode != 0:
            logger.warning(
                f"agy proxy: tier {tier['model_name']} failed "
                f"(rc={returncode}, stderr={stderr[:200]}). "
                f"Falling to next tier..."
            )
            continue

        # Success!
        if stdout:
            elapsed_total = time.time() - start_time
            
            # Save session state for continuation
            if session_id:
                _session_store[session_id] = {
                    "conversation_id": last_conv_id,
                    "current_tier_index": actual_tier_idx,
                }
                logger.info(f"agy proxy: saved session {session_id[:8]}..."
                            f" → conversation={last_conv_id[:8]}..., tier={tier['model_name']}")
            
            logger.info(
                f"agy proxy: ✅ tier {tier['model_name']} succeeded "
                f"({len(stdout)} chars, {elapsed_total:.1f}s)"
            )
            return _wrap_response(stdout, tier["model_name"], proxy_prompt)
        else:
            logger.warning(
                f"agy proxy: tier {tier['model_name']} returned empty response"
            )
            continue

    # All tiers exhausted — clean up session
    if session_id and session_id in _session_store:
        del _session_store[session_id]
    
    logger.warning("agy proxy: all tiers exhausted — falling back to LiteLLM")
    return None