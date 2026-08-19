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
import orjson
import aiofiles
import logging
import os
import time
import uuid
import httpx
from typing import Optional, Protocol, runtime_checkable
from dataclasses import dataclass

@runtime_checkable
class CooldownPersistence(Protocol):
    """Interface for persisting/syncing Valkey cooldown state."""
    async def sync(self) -> None:
        """Pull latest cooldown state from Valkey."""
        ...

    async def save(self) -> None:
        """Push updated cooldown state to Valkey."""
        ...

try:
    from router.circuit_breaker import get_google_breaker, get_vendor_breaker
except ModuleNotFoundError as e:
    if e.name == "router":
        from circuit_breaker import get_google_breaker, get_vendor_breaker
    else:
        raise



logger = logging.getLogger("agy-proxy")

# In container: mounted from host ~/.local/bin/agy
AGY_BINARY = os.environ.get("AGY_BINARY_PATH", "/usr/local/bin/agy")
if not os.path.exists(AGY_BINARY):
    AGY_BINARY = os.path.expanduser("~/.local/bin/agy")
if not os.path.exists(AGY_BINARY):
    AGY_BINARY = "agy"

# Ordered fallback tiers
AGY_FALLBACK_TIERS = [
    {"model_name": "gemini-3.5-flash",  "env_override": ""},                             # Tier 1: default
    {"model_name": "claude-opus-4.6",   "env_override": "claude-opus-4-6@default"},      # Tier 2
]

AGY_TIMEOUT_SECS = 120
AGY_TOTAL_TIMEOUT_SECS = 300

# In-memory session store: {router_session_id: agy_conversation_data}
# agy_conversation_data = {"conversation_id": str, "current_tier_index": int, "last_accessed": float}
_session_store: dict = {}
MAX_SESSION_STORE_SIZE = 10000
SESSION_TTL_SECONDS = 86400  # 24 hours


def cleanup_session_store(max_size: int = MAX_SESSION_STORE_SIZE) -> None:
    """Purge expired sessions and evict oldest (LRU) sessions if size exceeds max_size."""
    now = time.time()
    expired_keys = [
        k for k, v in _session_store.items()
        if isinstance(v, dict) and now - v.get("last_accessed", now) >= SESSION_TTL_SECONDS
    ]
    for k in expired_keys:
        _session_store.pop(k, None)

    if len(_session_store) > max_size:
        sorted_keys = sorted(
            _session_store.keys(),
            key=lambda k: _session_store[k].get("last_accessed", 0) if isinstance(_session_store[k], dict) else 0
        )
        excess = len(_session_store) - max_size
        for k in sorted_keys[:excess]:
            _session_store.pop(k, None)


def get_session_store(session_id: str) -> Optional[dict]:
    """Retrieve session data for session_id with TTL check and LRU timestamp update."""
    if session_id not in _session_store:
        return None
    session = _session_store[session_id]
    if not isinstance(session, dict):
        _session_store.pop(session_id, None)
        return None
    now = time.time()
    if now - session.get("last_accessed", now) >= SESSION_TTL_SECONDS:
        _session_store.pop(session_id, None)
        return None
    session["last_accessed"] = now
    return session


def set_session_store(session_id: str, conversation_id: str, current_tier_index: int) -> None:
    """Set or update session data with max capacity enforcement and LRU eviction."""
    if session_id not in _session_store and len(_session_store) >= MAX_SESSION_STORE_SIZE:
        cleanup_session_store(MAX_SESSION_STORE_SIZE - 1)
    _session_store[session_id] = {
        "conversation_id": conversation_id,
        "current_tier_index": current_tier_index,
        "last_accessed": time.time(),
    }

AGY_DAEMON_URL = os.environ.get("AGY_DAEMON_URL", "http://127.0.0.1:5005")


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

async def _is_quota_exhausted(returncode: int, stdout: str, stderr: str) -> bool:
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
                async with aiofiles.open(log_path, "rb") as f:
                    try:
                        await f.seek(0, 2)
                        file_size = await f.tell()
                        await f.seek(max(0, file_size - 1024))
                    except OSError:
                        raise
                    content_bytes = await f.read()
                    content = content_bytes.decode("utf-8", errors="ignore")
                    for line in content.splitlines()[-5:]:
                        if "RESOURCE_EXHAUSTED" in line or "code 429" in line:
                            return True
                    return False
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
        "id": f"chatcmpl-{uuid.uuid4().hex}",
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

@dataclass
class AgyProxyRequest:
    prompt: str
    messages: Optional[list] = None
    session_id: Optional[str] = None
    total_timeout: float = AGY_TOTAL_TIMEOUT_SECS
    stream: bool = False
    target_tier: str = "agent-advanced-core"
    client: Optional[httpx.AsyncClient] = None
    cooldown_persistence: Optional[CooldownPersistence] = None

async def try_agy_proxy(request: AgyProxyRequest) -> Optional[dict]:
    """
    Attempt agy proxy with session-aware tier fallback.
    
    Args:
        request: AgyProxyRequest containing all parameters
    
    Returns:
        OpenAI-compatible response dict, streaming dict, or None if all tiers failed.
    """
    prompt = request.prompt
    messages = request.messages
    session_id = request.session_id
    total_timeout = request.total_timeout
    stream = request.stream
    target_tier = request.target_tier
    client = request.client
    cooldown_persistence = request.cooldown_persistence
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
            session = get_session_store(session_id)
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
                    first_data = orjson.loads(first_line)
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
                    is_exhausted = await _is_quota_exhausted(rc, "", stderr_content)
                    if is_exhausted or rc != 0:
                        if is_exhausted:
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
                    try:
                        # Yield the initial token if it was a token
                        try:
                            init_data = orjson.loads(initial_line)
                            if init_data.get("type") == "token" and init_data.get("content"):
                                yield init_data["content"]
                            elif init_data.get("type") == "conversation_id" and init_data.get("id"):
                                current_conv_id = init_data["id"]
                                if session_id:
                                    set_session_store(
                                        session_id,
                                        current_conv_id,
                                        actual_tier_idx,
                                    )
                        except (orjson.JSONDecodeError, json.JSONDecodeError, Exception) as parse_err:
                            logger.warning(f"agy proxy: failed parsing initial token line: {parse_err}")

                        async for line in lines_iter:
                            if not line.strip():
                                continue
                            try:
                                data = orjson.loads(line)
                                if data.get("type") == "token" and data.get("content"):
                                    yield data["content"]
                                elif data.get("type") == "conversation_id" and data.get("id"):
                                    current_conv_id = data["id"]
                                    if session_id:
                                        set_session_store(
                                            session_id,
                                            current_conv_id,
                                            actual_tier_idx,
                                        )
                            except (orjson.JSONDecodeError, json.JSONDecodeError, Exception) as parse_err:
                                logger.warning(f"agy proxy: failed parsing streaming token line: {parse_err}")
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
                if await _is_quota_exhausted(returncode, stdout, stderr):
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
                        set_session_store(session_id, last_conv_id, actual_tier_idx)
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
        if session_id and session_id in _session_store:
            del _session_store[session_id]
        
        logger.warning("agy proxy: all tiers exhausted — falling back to LiteLLM")
        return None
    finally:
        if should_close_client and not stream_returned:
            await client.aclose()