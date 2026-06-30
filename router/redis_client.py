import os
import time
import logging
import redis.asyncio as aioredis

logger = logging.getLogger("llm-triage-router")

_redis_client = None
_redis_last_init_attempt = 0.0
_REDIS_RETRY_INTERVAL_SECONDS = 5.0

def get_redis():
    """Lazily initialize and return the async Redis/Valkey client.
    Returns None if connection fails or is disabled (non-fatal fallback)."""
    global _redis_client, _redis_last_init_attempt
    if _redis_client is None:
        now = time.monotonic()
        if now - _redis_last_init_attempt < _REDIS_RETRY_INTERVAL_SECONDS:
            return None
        _redis_last_init_attempt = now
        try:
            host = os.getenv("VALKEY_HOST", "127.0.0.1")
            port = int(os.getenv("VALKEY_PORT", "6379"))
            _redis_client = aioredis.Redis(host=host, port=port, decode_responses=True, socket_timeout=1.0)
            logger.info(f"Valkey client initialized at {host}:{port}")
        except Exception as e:
            logger.warning(f"Failed to initialize Valkey client: {e} — falling back to local memory")
            _redis_client = None
    return _redis_client

async def close_redis():
    """Close the shared Redis client connection."""
    global _redis_client
    if _redis_client is not None:
        try:
            # Check if it's False (sentinel used in some contexts, though not here yet)
            if _redis_client is not False:
                await _redis_client.aclose()
        except Exception as e:
            logger.warning(f"Error closing Redis client: {e}")
        finally:
            _redis_client = None

def reset_redis_on_failure():
    """Reset the Redis client state on failure to trigger a retry on next access."""
    global _redis_client, _redis_last_init_attempt
    _redis_client = None
    _redis_last_init_attempt = time.monotonic()
