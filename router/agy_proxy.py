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

from circuit_breaker import get_google_breaker, get_vendor_breaker

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
        from main import get_http_client
        client = get_http_client()
        r = await client.post(url, json=payload, timeout=timeout + 5.0)
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
                        total_timeout: float = AGY_TOTAL_TIMEOUT_SECS,
                        stream: bool = False,
                        target_tier: str = "agent-advanced-core") -> Optional[dict]:
    """
    Attempt agy proxy with session-aware tier fallback.
    
    Args:
        prompt: Current user prompt
        messages: Full message history for context
        session_id: Router session identifier for conversation continuity
        total_timeout: Max total time across all tiers
        stream: If True, returns a dict with {"stream": async_generator, "model": model_name}
        target_tier: Classified tier — "agent-reasoning-core" uses gemini-3.5-flash (low thinking),
                     "agent-advanced-core" uses full 2-tier chain (gemini-3.5-flash → claude-opus-4.6)
    
    Returns:
        OpenAI-compatible response dict, streaming dict, or None if all tiers failed.
    """
    # Select model chain based on target tier
    # Reasoning: single tier, gemini-3.5-flash with low thinking
    # Advanced: full 2-tier chain (gemini-3.5-flash → claude-opus-4.6)
    if target_tier == "agent-reasoning-core":
        agy_tiers = [
            {"model_name": "gemini-3.5-flash", "env_override": ""},  # low thinking default
        ]
    else:
        agy_tiers = AGY_FALLBACK_TIERS  # full chain: gemini-3.5-flash → claude-opus-4.6
    # Sync states from Valkey first
    try:
        from main import sync_cooldowns_from_valkey
        await sync_cooldowns_from_valkey()
    except Exception:
        pass

    # Per-model circuit breakers — Google and vendor (Claude/GPT) have independent
    # rate-limit windows (separate 5-hour quota refresh cycles).
    google_breaker = get_google_breaker()
    vendor_breaker = get_vendor_breaker()

    # Check if ANY model path is available
    if not google_breaker.is_allowed() and not vendor_breaker.is_allowed():
        logger.info(
            f"agy proxy: both circuit breakers open (google tier={google_breaker.tier}, "
            f"vendor tier={vendor_breaker.tier}) — skipping agy, falling through to LiteLLM"
        )
        return None

    # Build context-aware prompt from message history
    proxy_prompt = prompt
    if messages:
        context_parts = []
        for msg in messages[-10:]:
            role = msg.get("role", "user")
            content = msg.get("content") or ""
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
    
    import httpx
    
    for tier_idx, tier in enumerate(agy_tiers[start_tier_index:]):
        actual_tier_idx = start_tier_index + tier_idx
        elapsed = time.time() - start_time
        remaining = total_timeout - elapsed
        if remaining <= 0:
            logger.warning(f"agy proxy: total timeout exhausted at tier {tier['model_name']}")
            break

        # Determine which breaker to use for this tier
        # Tier 0 (idx 0): gemini-3.5-flash → google_breaker
        # Tier 1 (idx 1): claude-opus-4.6  → vendor_breaker
        is_google_tier = "gemini" in tier.get("model_name", "").lower()
        tier_breaker = google_breaker if is_google_tier else vendor_breaker

        if not tier_breaker.is_allowed():
            logger.info(
                f"agy proxy: tier {tier['model_name']} blocked by circuit breaker "
                f"(tier {tier_breaker.tier}, {tier_breaker.cooldown_until - time.time():.0f}s remaining) — skipping"
            )
            continue

        tier_timeout = min(AGY_TIMEOUT_SECS, remaining)
        
        if stream:
            url = "http://127.0.0.1:5005/run"
            payload = {
                "prompt": proxy_prompt,
                "model_override": tier["env_override"],
                "conversation_id": last_conv_id if actual_tier_idx > 0 or existing_conv_id else None,
                "timeout": tier_timeout,
                "stream": True
            }
            
            model_tag = tier["env_override"] if tier["env_override"] else "default (gemini-3.5-flash)"
            logger.info(f"agy proxy connecting stream to daemon: [{model_tag}]...")
            
            from main import get_http_client
            client = get_http_client()
            should_close_client = False
            req = client.build_request("POST", url, json=payload)
            try:
                r = await client.send(req, stream=True, timeout=tier_timeout + 5.0)
            except Exception as e:
                logger.error(f"Failed to connect stream to daemon: {e}")
                if should_close_client:
                    await client.aclose()
                continue
                
            # Read first line to see if it's successful or quota error
            first_line = None
            try:
                lines_iter = r.aiter_lines()
                first_line = await anext(lines_iter)
            except (StopAsyncIteration, Exception):
                pass
                
            if not first_line:
                await r.aclose()
                if should_close_client:
                    await client.aclose()
                logger.warning(f"agy proxy: tier {tier['model_name']} returned empty stream. Trying next tier...")
                continue
                
            try:
                first_data = json.loads(first_line)
            except Exception:
                await r.aclose()
                if should_close_client:
                    await client.aclose()
                logger.error(f"agy proxy: invalid JSON from daemon: {first_line}")
                continue
                
            # Check if first message is a status failure
            if first_data.get("type") == "status":
                rc = first_data.get("returncode", 0)
                stderr_content = first_data.get("stderr", "")
                if _is_quota_exhausted(rc, "", stderr_content) or rc != 0:
                    if _is_quota_exhausted(rc, "", stderr_content):
                        tier_breaker.record_failure()
                        try:
                            from main import save_cooldowns_to_valkey
                            await save_cooldowns_to_valkey()
                        except Exception:
                            pass
                    await r.aclose()
                    if should_close_client:
                        await client.aclose()
                    logger.warning(f"agy proxy: tier {tier['model_name']} failed immediately (rc={rc}). Trying next tier...")
                    continue
                    
            # Success! Stream has started.
            tier_breaker.record_success()
            try:
                from main import save_cooldowns_to_valkey
                await save_cooldowns_to_valkey()
            except Exception:
                pass
            async def token_generator(stream_resp, httpx_client, initial_line, current_conv_id):
                """Asynchronously yields tokens from the agy daemon stream and manages session state updates."""
                # Yield the initial token if it was a token
                init_data = json.loads(initial_line)
                if init_data.get("type") == "token" and init_data.get("content"):
                    yield init_data["content"]
                elif init_data.get("type") == "conversation_id" and init_data.get("id"):
                    current_conv_id = init_data["id"]
                    if session_id:
                        _session_store[session_id] = {
                            "conversation_id": current_conv_id,
                            "current_tier_index": actual_tier_idx,
                        }
                
                try:
                    async for line in lines_iter:
                        if not line.strip():
                            continue
                        data = json.loads(line)
                        if data.get("type") == "token" and data.get("content"):
                            yield data["content"]
                        elif data.get("type") == "conversation_id" and data.get("id"):
                            current_conv_id = data["id"]
                            if session_id:
                                _session_store[session_id] = {
                                    "conversation_id": current_conv_id,
                                    "current_tier_index": actual_tier_idx,
                                }
                finally:
                    await stream_resp.aclose()
                    if should_close_client:
                        await httpx_client.aclose()
                    
            return {
                "stream": token_generator(r, client, first_line, last_conv_id),
                "model": tier["model_name"]
            }
            
        else:
            # Non-streaming path
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
                tier_breaker.record_failure()
                try:
                    from main import save_cooldowns_to_valkey
                    await save_cooldowns_to_valkey()
                except Exception:
                    pass
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
                tier_breaker.record_success()
                try:
                    from main import save_cooldowns_to_valkey
                    await save_cooldowns_to_valkey()
                except Exception:
                    pass
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