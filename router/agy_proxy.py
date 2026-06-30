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

import json
import logging
import os
import time
import httpx
from typing import Optional, Protocol, runtime_checkable, Dict, Any
from redis_client import get_redis

@runtime_checkable
class CooldownPersistence(Protocol):
    """Interface for persisting/syncing Valkey cooldown state."""
    async def sync(self) -> None:
        """Pull latest cooldown state from Valkey."""
        ...

    async def save(self) -> None:
        """Push updated cooldown state to Valkey."""
        ...

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

class BoundedSessionStore:
    """A simple in-memory bounded session store with TTL (Option B fallback)."""
    def __init__(self, maxsize: int = 10000, ttl: int = 86400):
        self.maxsize = maxsize
        self.ttl = ttl
        self.warn_threshold = 5000
        self._has_warned = False
        self._data: Dict[str, Dict[str, Any]] = {}
        self._expiry: Dict[str, float] = {}

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        if key in self._data:
            if now < self._expiry.get(key, 0):
                return self._data[key]
            else:
                self.delete(key)
        return None

    def set(self, key: str, value: Dict[str, Any], ttl: Optional[int] = None):
        if self.maxsize <= 0:
            return
        now = time.time()
        current_size = len(self._data)

        if current_size >= self.warn_threshold and not self._has_warned:
            logger.warning(f"agy proxy: high session count detected ({current_size}). Memory growth may increase.")
            self._has_warned = True
        elif current_size < self.warn_threshold:
            self._has_warned = False

        if current_size >= self.maxsize and key not in self._data and self._data:
            # Evict oldest by first key in dict (Python 3.7+ dict is ordered)
            oldest_key = next(iter(self._data))
            self.delete(oldest_key)

        self._data[key] = value
        self._expiry[key] = now + (ttl if ttl is not None else self.ttl)

    def delete(self, key: str):
        self._data.pop(key, None)
        self._expiry.pop(key, None)

_local_session_cache = BoundedSessionStore()

async def _get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve session data from Valkey (Option A) or local cache (Option B)."""
    # 1. Try Valkey (Redis)
    redis = get_redis()
    if redis:
        try:
            raw = await redis.get(f"agy:session:{session_id}")
            if raw:
                data = json.loads(raw)
                _local_session_cache.set(session_id, data)
                return data
        except Exception as e:
            logger.warning(f"Failed to get session from Valkey: {e}")

    # 2. Fallback to local cache
    return _local_session_cache.get(session_id)

async def _store_session(session_id: str, data: Dict[str, Any], ttl: int = 86400):
    """Store session data in Valkey (Option A) and local cache (Option B)."""
    # 1. Store in Valkey (Redis)
    redis = get_redis()
    if redis:
        try:
            await redis.set(f"agy:session:{session_id}", json.dumps(data), ex=ttl)
        except Exception as e:
            logger.warning(f"Failed to store session in Valkey: {e}")

    # 2. Always update local cache as primary/fallback
    _local_session_cache.set(session_id, data, ttl=ttl)

async def _delete_session(session_id: str):
    """Remove session from Valkey and local cache."""
    redis = get_redis()
    if redis:
        try:
            await redis.delete(f"agy:session:{session_id}")
        except Exception as e:
            logger.warning(f"Failed to delete session from Valkey: {e}")

    _local_session_cache.delete(session_id)

def get_session_count() -> int:
    """Return the current number of active sessions in the local cache."""
    return len(_local_session_cache._data)


AGY_DAEMON_URL = os.environ.get("AGY_DAEMON_URL", "http://127.0.0.1:5005")


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


async def _run_agy_print(client: httpx.AsyncClient, prompt: str, model_override: str = "",
                         conversation_id: Optional[str] = None,
                         timeout: float = AGY_TIMEOUT_SECS) -> tuple[int, str, str, Optional[str]]:
    """
    Forward the agy execution request to the host-side agy daemon.
    """
    url = f"{AGY_DAEMON_URL}/run"
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
        r = await client.post(url, json=payload, timeout=timeout + 5.0)
        if r.status_code == 200:
            result = r.json()
            ret_code = result.get("returncode", 0)
            stdout_val = result.get("stdout", "")
            stderr_val = result.get("stderr", "")
            conv_id = result.get("conversation_id", None)
            return (
                0 if ret_code is None else ret_code,
                "" if stdout_val is None else stdout_val,
                "" if stderr_val is None else stderr_val,
                conv_id
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
                        target_tier: str = "agent-advanced-core",
                        client: Optional[httpx.AsyncClient] = None,
                        cooldown_persistence: Optional[CooldownPersistence] = None) -> Optional[dict]:
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
        client: Shared HTTP client instance from caller
        cooldown_persistence: Valkey synchronization callback interface
    
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

    should_close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=total_timeout + 5.0)
        should_close_client = True

    stream_returned = False
    try:
        if cooldown_persistence is not None:
            try:
                await cooldown_persistence.sync()
            except Exception as e:
                logger.warning(f"Failed to sync state from Valkey: {e}")

        # Per-model circuit breakers — Google and vendor (Claude/GPT) have independent
        # rate-limit windows (separate 5-hour quota refresh cycles).
        google_breaker = get_google_breaker()
        vendor_breaker = get_vendor_breaker()

        # Check if ANY model path is available without mutating state
        if not google_breaker.is_currently_allowed() and not vendor_breaker.is_currently_allowed():
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
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role", "user")
                content = msg.get("content") or ""
                if isinstance(content, list):
                    content = "".join(block.get("text") or "" for block in content if isinstance(block, dict) and block.get("type") == "text")
                if role == "user":
                    context_parts.append(f"User: {content}")
                elif role == "assistant":
                    context_parts.append(f"Assistant: {content}")
            proxy_prompt = "\n".join(context_parts[-6:])

        # Check if we have an existing session with a conversation ID
        existing_conv_id = None
        start_tier_index = 0
        if session_id:
            session = await _get_session(session_id)
            if session:
                existing_conv_id = session.get("conversation_id")
                start_tier_index = session.get("current_tier_index", 0)
                conv_id_str = f"conversation={existing_conv_id[:8]}..." if existing_conv_id else "no conversation_id"
                logger.info(f"agy proxy: resuming session {session_id[:8]}..., {conv_id_str}")
        
        start_time = time.time()
        last_conv_id = existing_conv_id
        
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
                    f"(tier {tier_breaker.tier}, {max(0.0, tier_breaker.cooldown_until - time.time()):.0f}s remaining) — skipping"
                )
                continue

            tier_timeout = min(AGY_TIMEOUT_SECS, remaining)
            
            if stream:
                url = f"{AGY_DAEMON_URL}/run"
                payload = {
                    "prompt": proxy_prompt,
                    "model_override": tier["env_override"],
                    "conversation_id": last_conv_id if actual_tier_idx > 0 or existing_conv_id else None,
                    "timeout": tier_timeout,
                    "stream": True
                }
                
                model_tag = tier["env_override"] if tier["env_override"] else "default (gemini-3.5-flash)"
                logger.info(f"agy proxy connecting stream to daemon: [{model_tag}]...")
                
                req = client.build_request("POST", url, json=payload, timeout=tier_timeout + 5.0)
                try:
                    r = await client.send(req, stream=True)
                except Exception as e:
                    logger.error(f"Failed to connect stream to daemon: {e}")
                    continue
                    
                # Read first line to see if it's successful or quota error
                first_line = None
                try:
                    lines_iter = r.aiter_lines()
                    first_line = await anext(lines_iter)
                except StopAsyncIteration:
                    pass
                except Exception as e:
                    logger.warning(f"agy proxy: failed reading initial stream line from {tier['model_name']}: {e}")
                    
                if not first_line:
                    await r.aclose()
                    logger.warning(f"agy proxy: tier {tier['model_name']} returned empty stream. Trying next tier...")
                    continue
                    
                try:
                    first_data = json.loads(first_line)
                except Exception:
                    await r.aclose()
                    logger.error(f"agy proxy: invalid JSON from daemon: {first_line}")
                    continue
                    
                # Check if first message is a status failure
                if first_data.get("type") == "status":
                    raw_rc = first_data.get("returncode", 0)
                    rc = 0 if raw_rc is None else raw_rc
                    raw_stderr = first_data.get("stderr", "")
                    stderr_content = "" if raw_stderr is None else raw_stderr
                    if _is_quota_exhausted(rc, "", stderr_content) or rc != 0:
                        if _is_quota_exhausted(rc, "", stderr_content):
                            tier_breaker.record_failure()
                            if cooldown_persistence is not None:
                                try:
                                    await cooldown_persistence.save()
                                except Exception as e:
                                    logger.warning(f"Failed to save cooldowns to Valkey: {e}")
                        await r.aclose()
                        logger.warning(f"agy proxy: tier {tier['model_name']} failed immediately (rc={rc}). Trying next tier...")
                        continue
                        
                # Success! Stream has started.
                tier_breaker.record_success()
                if cooldown_persistence is not None:
                    try:
                        await cooldown_persistence.save()
                    except Exception as e:
                        logger.warning(f"Failed to save cooldowns to Valkey: {e}")
                
                async def token_generator(stream_resp, httpx_client, initial_line, current_conv_id, close_client):
                    """Asynchronously yields tokens from the agy daemon stream and manages session state updates."""
                    # Yield the initial token if it was a token
                    init_data = json.loads(initial_line)
                    if init_data.get("type") == "token" and init_data.get("content"):
                        yield init_data["content"]
                    elif init_data.get("type") == "conversation_id" and init_data.get("id"):
                        current_conv_id = init_data["id"]
                        if session_id:
                            await _store_session(session_id, {
                                "conversation_id": current_conv_id,
                                "current_tier_index": actual_tier_idx,
                            })
                    
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
                                    await _store_session(session_id, {
                                        "conversation_id": current_conv_id,
                                        "current_tier_index": actual_tier_idx,
                                    })
                    finally:
                        await stream_resp.aclose()
                        if close_client:
                            await httpx_client.aclose()
                        
                stream_returned = True
                return {
                    "stream": token_generator(r, client, first_line, last_conv_id, should_close_client),
                    "model": tier["model_name"]
                }
                
            else:
                # Non-streaming path
                returncode, stdout, stderr, result_conv_id = await _run_agy_print(
                    client,
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
                    if cooldown_persistence is not None:
                        try:
                            await cooldown_persistence.save()
                        except Exception as e:
                            logger.warning(f"Failed to save cooldowns to Valkey: {e}")
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
                    if session_id and last_conv_id is not None:
                        await _store_session(session_id, {
                            "conversation_id": last_conv_id,
                            "current_tier_index": actual_tier_idx,
                        })
                        logger.info(f"agy proxy: saved session {session_id[:8]}..."
                                    f" → conversation={last_conv_id[:8]}..., tier={tier['model_name']}")
                    
                    logger.info(
                        f"agy proxy: ✅ tier {tier['model_name']} succeeded "
                        f"({len(stdout)} chars, {elapsed_total:.1f}s)"
                    )
                    tier_breaker.record_success()
                    if cooldown_persistence is not None:
                        try:
                            await cooldown_persistence.save()
                        except Exception as e:
                            logger.warning(f"Failed to save cooldowns to Valkey: {e}")
                    return _wrap_response(stdout, tier["model_name"], proxy_prompt)
                else:
                    logger.warning(
                        f"agy proxy: tier {tier['model_name']} returned empty response"
                    )
                    continue

        # All tiers exhausted — clean up session
        if session_id:
            await _delete_session(session_id)
        
        logger.warning("agy proxy: all tiers exhausted — falling back to LiteLLM")
        return None
    finally:
        if should_close_client and not stream_returned:
            await client.aclose()