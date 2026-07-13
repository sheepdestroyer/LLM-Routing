"""Main FastAPI application for the LLM Triage & Fallback Gateway."""
import os
import uuid
import aiofiles
import re
import sys
import json
import time
import asyncio
import logging
import copy
import tempfile
import yaml
import httpx
import redis.asyncio as aioredis
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from urllib.parse import urlparse
try:
    from router.circuit_breaker import get_breaker
except ImportError:
    from circuit_breaker import get_breaker
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, RootModel
from typing import Dict, Optional, Union

try:
    from langfuse import propagate_attributes  # noqa: F401
except ImportError:
    propagate_attributes = None

LITELLM_URL = (os.getenv("LITELLM_ADMIN_URL") or f"http://127.0.0.1:{os.getenv('LITELLM_PORT') or '4000'}").rstrip("/")
LANGFUSE_HOST = (os.getenv("LANGFUSE_HOST") or f"http://127.0.0.1:{os.getenv('LANGFUSE_WEB_PORT') or '3001'}").rstrip("/")

GEMINI_OAUTH_CREDS_PATH = "/config/gemini_auth/oauth_creds.json"


_redis_client = None
_redis_last_init_attempt = 0.0
_REDIS_RETRY_INTERVAL_SECONDS = 5.0


def _valkey_port() -> int:
    """Resolve the Valkey cache port from env, preferring VALKEY_CACHE_PORT."""
    port_str = os.getenv("VALKEY_CACHE_PORT") or os.getenv("VALKEY_PORT", "6379")
    try:
        return int(port_str)
    except ValueError:
        logger.warning(f"Invalid Valkey port '{port_str}', defaulting to 6379")
        return 6379

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
            url = os.getenv("VALKEY_URL")
            if url:
                _redis_client = aioredis.Redis.from_url(url, decode_responses=True, socket_timeout=1.0)
                logger.info("Valkey client initialized from URL")
            else:
                host = os.getenv("VALKEY_HOST", "127.0.0.1")
                port = _valkey_port()
                _redis_client = aioredis.Redis(host=host, port=port, decode_responses=True, socket_timeout=1.0)
                logger.info(f"Valkey client initialized at {host}:{port}")
        except Exception as e:
            logger.warning(f"Failed to initialize Valkey client: {e} — falling back to local memory")
            _redis_client = None
    return _redis_client


# Connection pool limits configuration for the shared HTTP client
HTTP_MAX_CONNECTIONS = int(os.getenv("HTTP_MAX_CONNECTIONS") or "1000")
HTTP_MAX_KEEPALIVE_CONNECTIONS = int(
    os.getenv("HTTP_MAX_KEEPALIVE_CONNECTIONS") or "500"
)
HTTP_KEEPALIVE_EXPIRY = float(os.getenv("HTTP_KEEPALIVE_EXPIRY") or "5.0")

_http_client = None


def _http_limits() -> httpx.Limits:
    """Shared connection limits for all httpx clients."""
    return httpx.Limits(
        max_connections=HTTP_MAX_CONNECTIONS,
        max_keepalive_connections=HTTP_MAX_KEEPALIVE_CONNECTIONS,
        keepalive_expiry=HTTP_KEEPALIVE_EXPIRY,
    )


def get_http_client():
    """Return the shared global httpx.AsyncClient singleton with configured limits."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(limits=_http_limits(), timeout=3600.0)
    return _http_client


_classifier_client: httpx.AsyncClient | None = None


def _resolve_verify(env_var: str) -> bool | str:
    """Resolve TLS verify setting from an environment variable.

    Returns:
        False if unset, empty, or boolean-like false.
        True if boolean-like true.
        A string path for a CA bundle file.
    """
    ca_bundle = os.getenv(env_var)
    if ca_bundle is None:
        return False
    v = ca_bundle.strip()
    if v.lower() in ("false", "0", "off", "no", "none", "null", "disabled", ""):
        return False
    if v.lower() in ("true", "1", "on", "yes"):
        return True
    return v


def get_classifier_client():
    """Return a singleton httpx client for classifier calls (internal self-signed TLS).

    By default verify is disabled because the classifier sits behind HAProxy with
    a self-signed certificate on the internal network. Set CLASSIFIER_CA_BUNDLE
    to a PEM file path to enable TLS verification (e.g. for CI or staging).
    """
    global _classifier_client
    if _classifier_client is None:
        _classifier_client = httpx.AsyncClient(
            limits=_http_limits(),
            timeout=3600.0,
            verify=_resolve_verify("CLASSIFIER_CA_BUNDLE"),
        )
    return _classifier_client


_llama_client: httpx.AsyncClient | None = None


def get_llama_client():
    """Return a singleton httpx client for llama.cpp server calls (internal self-signed TLS).

    By default verify is disabled because the llama-server sits behind HAProxy with
    a self-signed certificate on the internal network. Set LLAMA_CA_BUNDLE
    to a PEM file path to enable TLS verification (e.g. for CI or staging).
    """
    global _llama_client
    if _llama_client is None:
        _llama_client = httpx.AsyncClient(
            limits=_http_limits(),
            timeout=3600.0,
            verify=_resolve_verify("LLAMA_CA_BUNDLE"),
        )
    return _llama_client


# Compiled regular expressions for token estimation heuristics
WORD_RE = re.compile(r'[a-zA-Z0-9]+')
NON_ASCII_RE = re.compile(r'[^\s\x00-\x7F]')
PUNC_RE = re.compile(r'[\x21-\x2f\x3a-\x40\x5b-\x60\x7b-\x7e]')


def _count_tokens_heuristic(text: str) -> float:
    """Heuristically estimate token count using weighted categories and optimized regex splitting.

    This replaces the naive character-count logic with a more granular approach that
    balances English words, technical identifiers, punctuation, and multi-byte characters.

    Returns a float to prevent intermediate rounding errors when summing across multiple
    message blocks. Callers should round the total sum to convert it to an integer.
    """
    if not text:
        return 0.0

    # 1. Alphanumeric runs (Words/Identifiers/Hashes/Base64)
    # Use a length-aware heuristic to avoid under-counting technical content.
    word_matches = WORD_RE.findall(text)
    word_total = sum(1.2 if len(w) <= 8 else len(w) / 4.0 for w in word_matches)

    # 2. Non-ASCII characters (CJK/Emoji)
    # Each character is weighted at 0.35 tokens.
    non_ascii_count = len(NON_ASCII_RE.findall(text))

    # 3. ASCII Punctuation/Symbols
    # Characters that are ASCII but not alphanumeric or whitespace.
    punc_count = len(PUNC_RE.findall(text))

    return word_total + (non_ascii_count * 0.35) + (punc_count * 0.4)


METADATA_OVERHEAD = 50


def estimate_prompt_tokens(body: dict) -> int:
    """Estimate prompt tokens using a regex-based weighted heuristic for mixed content.
    """
    total = 0.0
    for msg in body.get("messages", []):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content") or ""
        if isinstance(content, str):
            total += _count_tokens_heuristic(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        total += _count_tokens_heuristic(text)

    # Include a flat estimate for system prompt / metadata overhead.
    # Use rounding to avoid truncation bias (e.g., 1.9 -> 1).
    return max(1, round(total) + METADATA_OVERHEAD)


async def sync_cooldowns_from_valkey() -> None:
    """Sync Ollama cooldown and circuit breaker states from Valkey to local memory."""
    redis = get_redis()
    if not redis:
        return
    try:
        val = await redis.get("cooldown:ollama")
        global _ollama_cooldown_until
        if val is not None:
            epoch_until = float(val)
            remaining = epoch_until - time.time()
            if remaining > 0:
                _ollama_cooldown_until = time.monotonic() + remaining
            else:
                _ollama_cooldown_until = 0.0
        else:
            if _ollama_cooldown_until <= time.monotonic():
                _ollama_cooldown_until = 0.0

        breaker = get_breaker()
        await breaker.sync_from_valkey(redis)
    except Exception as e:
        logger.warning(f"Failed to sync cooldowns from Valkey: {e}")
        global _redis_client, _redis_last_init_attempt
        _redis_client = None
        _redis_last_init_attempt = time.monotonic()


async def save_cooldowns_to_valkey() -> None:
    """Save local Ollama cooldown and circuit breaker states to Valkey."""
    redis = get_redis()
    if not redis:
        return
    try:
        global _ollama_cooldown_until
        now_mono = time.monotonic()
        if _ollama_cooldown_until > now_mono:
            remaining = _ollama_cooldown_until - now_mono
            epoch_until = time.time() + remaining
            ttl = int(max(1.0, remaining))
            await redis.set("cooldown:ollama", str(epoch_until), ex=ttl)
        else:
            await redis.delete("cooldown:ollama")

        breaker = get_breaker()
        await breaker.save_to_valkey(redis)
    except Exception as e:
        logger.warning(f"Failed to save cooldowns to Valkey: {e}")
        global _redis_client, _redis_last_init_attempt
        _redis_client = None
        _redis_last_init_attempt = time.monotonic()


class ValkeyCooldownPersistence:
    """Persistence provider mapping Valkey/Redis client synchronization to the global handlers."""

    async def sync(self) -> None:
        """Synchronize cooldowns from Valkey to local memory."""
        await sync_cooldowns_from_valkey()

    async def save(self) -> None:
        """Persist local memory cooldowns to Valkey."""
        await save_cooldowns_to_valkey()


# Configure logging — respect LOG_LEVEL env var (default: WARNING)
_log_level_str = os.getenv("LOG_LEVEL", "WARNING").upper()
_log_level = getattr(logging, _log_level_str, logging.WARNING)
logging.basicConfig(level=_log_level, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("llm-triage-router")
logger.info(f"Log level set to {_log_level_str} (from LOG_LEVEL env var)")

# Langfuse observability — per-request traces + aggregate score pushes
_langfuse_client = None


def get_langfuse():
    """Return the Langfuse client singleton, lazily initialized.
    Returns None if Langfuse is unreachable (non-fatal)."""
    global _langfuse_client
    if _langfuse_client is None:
        try:
            import langfuse

            _langfuse_client = langfuse.Langfuse(
                public_key=os.getenv("LANGFUSE_PUBLIC_KEY", ""),
                secret_key=os.getenv("LANGFUSE_SECRET_KEY", ""),
                host=LANGFUSE_HOST,
                release="llm-triage-router-v1",
            )
            logger.info("Langfuse client initialized")
        except (ImportError, ValueError, TypeError) as e:
            logger.warning(
                f"Langfuse client initialization failed: {e} — traces disabled"
            )
            _langfuse_client = False  # sentinel to avoid retry
    return _langfuse_client if _langfuse_client is not False else None


def _end_parent_obs(parent_obs, output=None, metadata=None) -> None:
    """Safely finalize a Langfuse parent observation (SDK v4: update + end).

    Non-fatal — swallows all exceptions.
    """
    if parent_obs is None:
        return
    try:
        update_kwargs = {}
        if output is not None:
            update_kwargs["output"] = output
        if metadata is not None:
            update_kwargs["metadata"] = metadata
        if update_kwargs:
            parent_obs.update(**update_kwargs)
        parent_obs.end()
    except Exception:
        logger.debug("_end_parent_obs failed (non-fatal)", exc_info=True)
        pass
        return


def _end_child_span(span, output=None, metadata=None) -> None:
    """Safely finalize a Langfuse child span (SDK v4: update + end).

    Non-fatal — errors are never propagated.
    """
    if span is None:
        return
    try:
        update_kwargs = {}
        if output is not None:
            update_kwargs["output"] = output
        if metadata is not None:
            update_kwargs["metadata"] = metadata
        if update_kwargs:
            span.update(**update_kwargs)
        span.end()
    except Exception:
        logger.debug("_end_child_span failed (non-fatal)", exc_info=True)
        pass


def _close_prop_ctx(prop_ctx):
    """Safely exit a propagate_attributes context manager if active.

    Non-fatal — swallows all exceptions.
    Returns None after exit for idempotent cleanup.
    """
    if prop_ctx is not None:
        try:
            prop_ctx.__exit__(None, None, None)
        except Exception:
            logger.debug("_close_prop_ctx failed (non-fatal)", exc_info=True)
            pass
    return


def _make_prop_ctx(session_id, user_id):
    """Create a propagate_attributes context manager if session/user propagation is active.

    Returns a context manager (entered by the caller) or None if
    propagate_attributes is unavailable or no session/user data is provided.
    DRY-consolidates the 4 duplicate condition+builder blocks across streaming
    generators and the non-streaming init path.
    """
    if not propagate_attributes or not (session_id or user_id):
        return None
    return propagate_attributes(
        session_id=session_id or None,
        user_id=user_id or None,
        tags=[os.getenv("ENVIRONMENT", "production"), "llm-routing"],
    )


async def push_aggregate_scores():
    """Push aggregate KPIs as Langfuse scores every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        lf = get_langfuse()
        if not lf:
            continue
        try:
            total = stats["total_requests"]
            if total == 0:
                continue
            router = get_breaker()
            scores = [
                {
                    "name": "simple_ratio_pct",
                    "value": stats.get("simple_requests", 0) / total * 100,
                },
                {
                    "name": "medium_ratio_pct",
                    "value": stats.get("medium_requests", 0) / total * 100,
                },
                {
                    "name": "complex_ratio_pct",
                    "value": stats.get("complex_requests", 0) / total * 100,
                },
                {
                    "name": "reasoning_ratio_pct",
                    "value": stats.get("reasoning_requests", 0) / total * 100,
                },
                {
                    "name": "advanced_ratio_pct",
                    "value": stats.get("advanced_requests", 0) / total * 100,
                },
                {
                    "name": "cache_hit_rate_pct",
                    "value": stats["cache_hits"] / total * 100,
                },
                {
                    "name": "avg_triage_latency_ms",
                    "value": stats["avg_triage_latency_ms"],
                },
                {
                    "name": "avg_proxy_latency_ms",
                    "value": stats["avg_proxy_latency_ms"],
                },
                {"name": "total_requests", "value": float(total)},
                {
                    "name": "circuit_breaker_google_tier",
                    "value": float(router.google.tier),
                },
                {
                    "name": "circuit_breaker_vendor_tier",
                    "value": float(router.vendor.tier),
                },
                {
                    "name": "google_oauth_direct_ratio_pct",
                    "value": stats["routing_paths"]["google_oauth_direct"]
                    / total
                    * 100,
                },
            ]
            trace_id = lf.create_trace_id(seed=f"aggregate_scores_{int(time.time())}")
            lf.start_observation(
                trace_context={"trace_id": trace_id},
                name="push-aggregate-scores",
                level="DEFAULT",
            )
            for s in scores:
                lf.create_score(name=s["name"], value=s["value"], trace_id=trace_id)
            lf.flush()
            logger.info(
                f"Pushed {len(scores)} aggregate scores to Langfuse (trace_id={trace_id})"
            )
        except Exception as e:
            logger.warning(f"Langfuse score push failed (non-fatal): {e}")


# Load configuration
CONFIG_PATH = os.getenv("CONFIG_PATH", "/config/config.yaml")
try:
    with open(CONFIG_PATH, "r") as f:
        config = yaml.safe_load(f)
except Exception as e:
    logger.error(f"Failed to load config from {CONFIG_PATH}: {e}")
    sys.exit(1)

host = config.get("server", {}).get("host", "0.0.0.0")
port = config.get("server", {}).get("port", 5000)

router_model_conf = config.get("router", {}).get("router_model", {})
router_api_base = router_model_conf.get("api_base") or "http://127.0.0.1:8080/v1"
if isinstance(router_api_base, str):
    if router_api_base.startswith("os.environ/"):
        env_var = router_api_base.split("/", 1)[1]
        router_api_base = os.environ.get(env_var, "")
        if not router_api_base:
            if "pytest" in sys.modules:
                router_api_base = "http://127.0.0.1:8080/v1"
            else:
                raise RuntimeError(
                    f"Configuration error: Environment variable '{env_var}' is missing or empty."
                )
    router_api_base = router_api_base.rstrip("/")

router_api_key = router_model_conf.get("api_key")
if not router_api_key:
    raise RuntimeError("Configuration error: 'api_key' is missing from router_model configuration.")
if not isinstance(router_api_key, str):
    router_api_key = str(router_api_key)
if router_api_key.startswith("os.environ/"):
    env_var = router_api_key.split("/", 1)[1]
    router_api_key = os.environ.get(env_var)
    if not router_api_key:
        if "pytest" in sys.modules:
            router_api_key = "local-token"
        else:
            raise RuntimeError(f"Configuration error: Environment variable '{env_var}' is missing or empty.")
router_model_name = router_model_conf.get("model", "qwen-4b-routing")

system_prompt = config.get("classification_rules", {}).get("system_prompt", "")
backends = {b["name"]: b for b in config.get("backends", [])}

# --- Resolve llama_server_url from config (os.environ/ pattern) ---
_raw_llama_url = config.get("llama_server_url") or "http://127.0.0.1:8080"
if isinstance(_raw_llama_url, str) and _raw_llama_url.startswith("os.environ/"):
    env_var = _raw_llama_url.split("/", 1)[1]
    _raw_llama_url = os.environ.get(env_var, "")
    if not _raw_llama_url:
        if "pytest" in sys.modules:
            _raw_llama_url = "http://127.0.0.1:8080"
        else:
            logger.warning(
                "LLAMA_SERVER_URL env var not set, falling back to http://127.0.0.1:8080"
            )
            _raw_llama_url = "http://127.0.0.1:8080"
LLAMA_SERVER_URL = str(_raw_llama_url).rstrip("/")

# Default colors for tool visualization badges and charts
TOOL_COLORS = {
    "tree": "#34d399",   # Green
    "shell": "#fbbf24",  # Amber/Orange
    "write": "#a78bfa",  # Violet
    "view": "#60a5fa",   # Blue
    "other": "#f472b6",  # Pink
}

# Triage and Performance Metric Trackers
stats = {
    "total_requests": 0,
    "simple_requests": 0,
    "medium_requests": 0,
    "complex_requests": 0,
    "reasoning_requests": 0,
    "advanced_requests": 0,
    "cache_hits": 0,
    "last_triage_decision": "None",
    "avg_triage_latency_ms": 0.0,
    "avg_proxy_latency_ms": 0.0,
    "total_triage_time_ms": 0.0,
    "total_proxy_time_ms": 0.0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "tool_tokens": {"tree": 0, "shell": 0, "write": 0, "view": 0, "other": 0},
    "routing_paths": {"google_oauth_direct": 0, "litellm_fallback": 0},
    "timeline": [],
}

# ---------------------------------------------------------------------------
# OLLAMA COOLDOWN — router-side cooldown for the Ollama backend.
# LiteLLM Community Edition's deployment cooldown is unreliable for single-
# deployment model groups (it bypasses cooldown when there's only 1 deployment)
# and doesn't reliably cooldown fallback-target model groups. Instead, the
# triage router tracks Ollama failures itself and returns 429 immediately
# during the cooldown window, skipping the LiteLLM call entirely.
# ---------------------------------------------------------------------------
_ollama_cooldown_until: float = 0.0  # monotonic timestamp when cooldown expires
try:
    OLLAMA_COOLDOWN_SECONDS: int = int(
        os.getenv("OLLAMA_COOLDOWN_SECONDS", "300")
    )  # 5 min default
    if OLLAMA_COOLDOWN_SECONDS <= 0:
        raise ValueError("OLLAMA_COOLDOWN_SECONDS must be positive")
except (TypeError, ValueError) as e:
    logger.warning(f"Invalid OLLAMA_COOLDOWN_SECONDS value: {e}; defaulting to 300")
    OLLAMA_COOLDOWN_SECONDS = 300

STATS_JSON_PATH = "/config/router_dir/router_stats.json"

# Module-level set to hold references to fire-and-forget background tasks,
# preventing premature garbage collection before the task completes (Ruff RUF006).
_background_tasks: set = set()


def load_persisted_stats():
    """Loads persisted statistics from disk on startup to prevent resets on pod redeployment."""
    global stats
    if os.path.exists(STATS_JSON_PATH):
        try:
            with open(STATS_JSON_PATH, "r") as f:
                loaded = json.load(f)
                # Merge loaded stats with default stats dictionary
                for k, v in loaded.items():
                    if isinstance(v, dict) and k in stats:
                        stats[k].update(v)
                    else:
                        stats[k] = v
            logger.info("✓ Successfully loaded persisted gateway statistics from disk.")
            # Load timeline from disk (may be stale after pod restart, but better than empty)
            timeline_path = os.path.join(
                os.path.dirname(CONFIG_PATH), "router_timeline.json"
            )
            if os.path.exists(timeline_path):
                try:
                    with open(timeline_path, "r") as f:
                        stats["timeline"] = json.load(f)
                except Exception:
                    pass  # stale/broken timeline file → start fresh
        except Exception as e:
            logger.error(f"Failed to load persisted stats: {e}")


def _atomic_write_json_sync(path: str, data) -> None:
    """Synchronously write JSON data to path using atomic temp-file + os.replace."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            os.close(fd)
            raise

        with f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


async def _atomic_write_json_async(path: str, data) -> None:
    """Asynchronously write JSON data to path via thread pool executor.

    Deep-copies the data to prevent concurrent modification from the main thread
    while the executor thread is serializing it.
    """
    loop = asyncio.get_running_loop()
    data_copy = copy.deepcopy(data)
    await loop.run_in_executor(None, _atomic_write_json_sync, path, data_copy)


_last_stats_save = 0.0


async def save_persisted_stats(force=False):
    """Persists current statistics in-memory structure to disk securely (non-blocking).

    Offloads the synchronous file write to a thread pool executor so the
    event loop is not blocked. The 2-second throttle is checked before
    dispatching.
    """
    global _last_stats_save
    now = time.monotonic()

    # Throttle disk writes to max once per 2 seconds, unless forced
    if not force and (now - _last_stats_save < 2.0):
        return

    _last_stats_save = now  # Set immediately to prevent concurrent writes during await
    try:
        await _atomic_write_json_async(STATS_JSON_PATH, stats)
    except Exception as e:
        _last_stats_save = 0.0  # Reset on failure to allow immediate retry
        logger.error(f"Failed to persist stats to disk: {e}")


# Load initial stats from persistent storage
load_persisted_stats()

# Triage Decision Cache (In-Memory dictionary mapping normalized prompt -> (classification, timestamp))
triage_cache = {}
CACHE_TTL_SECONDS = 86400  # Decisions cached for 24 hours
classification_lock = asyncio.Lock()


async def _purge_stale_deployments(db_url: str, pattern: str):
    """Purge stale deployments matching the pattern from LiteLLM's DB."""
    import asyncpg

    conn = await asyncpg.connect(db_url)
    try:
        await conn.execute(
            'DELETE FROM "LiteLLM_ProxyModelTable" WHERE model_name LIKE $1', pattern
        )
    finally:
        await conn.close()


async def sync_adaptive_router_roster(master_key: str):
    """Fetch free OpenRouter models and register them as deployments in LiteLLM."""
    if not master_key:
        logger.warning("No LITELLM_MASTER_KEY — skipping roster sync")
        return
    headers = {"Authorization": f"Bearer {master_key}", "Content-Type": "application/json"}
    admin_url = LITELLM_URL
    try:
        client = get_http_client()
        r = await client.get("https://openrouter.ai/api/v1/models", timeout=5.0)
        if r.status_code != 200:
            logger.warning(f"OpenRouter models API returned {r.status_code}")
            return
        all_models = r.json().get("data", [])
    except Exception as e:
        logger.warning(f"Failed to fetch OpenRouter models: {e}")
        return
    if not _AA_SCORES_LOADED:
        await asyncio.to_thread(_load_aa_scores)
    free_models = []
    model_contexts = {}
    model_supported_params = {}
    for m in all_models:
        mid = m.get("id", "")
        # Skip internal OpenRouter encoded IDs that LiteLLM can't map to a provider
        if not mid or (len(mid) > 64 and "/" not in mid):
            continue

        # 1. Enforce Tool/Function Calling Support
        supported_params = m.get("supported_parameters") or []
        if "tools" not in supported_params:
            logger.info(f"🚫 Skipping {mid} — Model does not support tool calling.")
            continue

        # 2. Denylist: skip models known to be problematic (stale, wrong context_length, etc.)
        # llama-3.3-70b reports 131K ctx but actual endpoint enforces 65K → context_limit errors.
        # All meta-llama and llama-derived models are too old and unreliable on free tier.
        _denylist_prefixes = (
            "meta-llama/",
            "nousresearch/hermes-3-llama",
        )
        if any(mid.startswith(p) for p in _denylist_prefixes):
            logger.info(
                f"🚫 Skipping {mid} — denylisted (stale/unreliable free tier model)"
            )
            continue

        pricing = m.get("pricing", {})
        if pricing.get("prompt") in ("0", 0, "0.0", 0.0) and pricing.get(
            "completion"
        ) in ("0", 0, "0.0", 0.0):
            try:
                score = compute_free_model_score(m)
            except Exception:
                score = 25.0  # conservative fallback for unparseable models
            free_models.append((score, mid))
            model_contexts[mid] = m.get("context_length") or 262144
            model_supported_params[mid] = supported_params
    free_models.sort(reverse=True)
    if not free_models:
        logger.warning("No free models found — skipping roster sync")
        return
    tier_assignments = {
        "agent-simple-core": [],
        "agent-medium-core": [],
        "agent-complex-core": [],
        "agent-reasoning-core": [],
        "agent-advanced-core": [],
    }
    # Normalize scores to 0-100 scale based on the actual max score in this roster.
    # This auto-adapts when Artificial Analysis updates scores — if the max is 55,
    # a score of 48 normalizes to 87; if the max rises to 80, 48 normalizes to 60.
    # Without normalization, hardcoded 80/75/68/60 thresholds are impossible to reach
    # when the AA Agentic Index caps free models at ~55.
    raw_scores = [s for s, _ in free_models]
    max_score = max(raw_scores) if raw_scores else 55.0
    if max_score < 1.0:
        max_score = 55.0  # safety floor

    def norm(s: float) -> float:
        """Helper to scale raw model index score against max score in roster to 0-100 range."""
        return (s / max_score) * 100.0

    for (
        score,
        mid,
    ) in (
        free_models
    ):  # include all models — top 2 are also assigned to their correct tier
        n = norm(score)
        if n >= 80:
            tier_assignments["agent-advanced-core"].append(mid)
        elif n >= 75:
            tier_assignments["agent-reasoning-core"].append(mid)
        elif n >= 68:
            tier_assignments["agent-complex-core"].append(mid)
        elif n >= 60:
            tier_assignments["agent-medium-core"].append(mid)
        else:
            tier_assignments["agent-simple-core"].append(mid)
    # Cascading: models capable of higher tiers also serve lower tiers.
    # A model that qualifies for advanced should be available for reasoning,
    # complex, and medium requests too — not just advanced. Without this,
    # tiers like complex and reasoning end up with only 1 model while 5
    # sit idle in advanced. Simple tier is excluded from cascading:
    # fast/small models belong there, not the 550B heavyweight.
    # Advanced → reasoning, complex, medium
    for mid in tier_assignments["agent-advanced-core"]:
        for t in ["agent-reasoning-core", "agent-complex-core", "agent-medium-core"]:
            if mid not in tier_assignments[t]:
                tier_assignments[t].append(mid)
    # Reasoning → complex, medium
    for mid in tier_assignments["agent-reasoning-core"]:
        for t in ["agent-complex-core", "agent-medium-core"]:
            if mid not in tier_assignments[t]:
                tier_assignments[t].append(mid)
    # Complex → medium
    for mid in tier_assignments["agent-complex-core"]:
        if mid not in tier_assignments["agent-medium-core"]:
            tier_assignments["agent-medium-core"].append(mid)
    # Safety net: if any tier is still empty after assignment, use top 2 models as fallback.
    # This shouldn't happen with current AA coverage, but guards against edge cases.
    top_two = [mid for _, mid in free_models[:2]]
    for tier_name, models in tier_assignments.items():
        if not models:
            tier_assignments[tier_name] = top_two[:]

    client = get_http_client()
    # Purge all existing agent-* deployments before re-registering.
    # Without this, every roster sync accumulates stale deployments (4,591+
    # in 24h), bloating the DB and slowing LiteLLM startup. Each sync now
    # starts clean — delete all, then register only the current roster.
    try:
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            logger.warning(
                "DATABASE_URL is not set; skipping purge of stale agent-* deployments"
            )
        else:
            await _purge_stale_deployments(db_url, "agent-%")
            logger.info("🧹 Purged stale agent-* deployments before roster sync")
    except Exception as e:
        logger.warning(f"Failed to purge stale deployments (non-fatal): {e}")

    registered = 0
    failed = 0
    for tier_name, model_ids in tier_assignments.items():
        for mid in model_ids:
            ctx_len = model_contexts.get(mid, 262144)
            sp = model_supported_params.get(mid, [])
            payload = {
                "model_name": tier_name,
                "litellm_params": {"model": f"openrouter/{mid}", "request_timeout": 20},
                "model_info": {
                    "supports_vision": "vision" in sp,
                    "supports_reasoning": True,  # OpenRouter API has no "reasoning" param; assume all modern LLMs support it
                    "supports_function_calling": "tools" in sp,
                    "mode": "chat",
                    "max_tokens": ctx_len,
                    "max_input_tokens": ctx_len,
                    "is_public_model_group": True,
                },
            }
            try:
                r = await client.post(
                    f"{admin_url}/model/new",
                    headers=headers,
                    json=payload,
                    timeout=10.0,
                )
                if r.status_code in (200, 201):
                    registered += 1
                else:
                    failed += 1
                    logger.warning(
                        f"model/new {mid} → {tier_name}: HTTP {r.status_code} — {r.text[:200]}"
                    )
            except Exception as e:
                failed += 1
                logger.warning(f"Failed to register {mid} under {tier_name}: {e}")
    logger.info(
        f"📊 Roster sync: registered {registered} deployments ({failed} failed) across 5 tiers — {sum(len(v) for v in tier_assignments.values())} attempted"
    )


async def _register_ollama_models_in_db(master_key: str):
    """Register static ollama models via /model/new so they become DB models.

    LiteLLM's /model_group/info endpoint aggregates model info using its internal
    model cost map for known providers.  For ollama_chat models not in the map,
    capabilities (vision, reasoning, function_calling) and token limits come back
    as null/false.  Registering them as DB models ensures our model_info wins.
    """
    if not master_key:
        logger.warning(
            "No LiteLLM master key provided — skipping Ollama DB registration"
        )
        return

    admin_url = LITELLM_URL
    headers = {"Authorization": f"Bearer {master_key}", "Content-Type": "application/json"}

    ollama_models = []
    litellm_config_path = os.getenv(
        "LITELLM_CONFIG_PATH", "/config/litellm_dir/config.yaml"
    )

    config_paths_to_try = [
        litellm_config_path,
        str(Path(__file__).resolve().parent.parent / "litellm" / "config.yaml"),
        "./litellm/config.yaml",
    ]

    def _load_yaml(p):
        """Helper to load a YAML file safely."""
        with open(p, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    loaded_from_config = False
    for path in config_paths_to_try:
        if path:
            try:
                litellm_config = await asyncio.to_thread(_load_yaml, path)
                if isinstance(litellm_config, dict) and isinstance(litellm_config.get("model_list"), list):
                    for item in litellm_config["model_list"]:
                        if isinstance(item, dict):
                            model_name = item.get("model_name", "")
                            if isinstance(model_name, str) and model_name.startswith(
                                "ollama-deepseek-"
                            ):
                                # Create a clean deep copy to avoid mutating configuration structures
                                ollama_models.append(copy.deepcopy(item))
                    if ollama_models:
                        logger.info(
                            f"Loaded {len(ollama_models)} Ollama model configurations dynamically from {path}"
                        )
                        loaded_from_config = True
                        break
            except Exception as e:
                logger.warning(f"Failed to load/parse LiteLLM config at {path}: {e}")

    if not loaded_from_config:
        logger.warning(
            "Could not load Ollama models from config.yaml, falling back to static definitions"
        )
        ollama_models = [
            {
                "model_name": "ollama-deepseek-v4-pro",
                "litellm_params": {
                    "model": "ollama_chat/deepseek-v4-pro",
                    "api_base": "https://api.ollama.com",
                    "api_key": "os.environ/OLLAMA_API_KEY",
                    "request_timeout": 120,
                },
                "model_info": {
                    "supports_vision": True,
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                    "mode": "chat",
                    "max_tokens": 524288,
                    "max_input_tokens": 524288,
                    "input_cost_per_token": 0.00000174,
                    "output_cost_per_token": 0.00000348,
                    "is_public_model_group": True,
                },
            },
            {
                "model_name": "ollama-deepseek-v4-flash",
                "litellm_params": {
                    "model": "ollama_chat/deepseek-v4-flash",
                    "api_base": "https://api.ollama.com",
                    "api_key": "os.environ/OLLAMA_API_KEY",
                    "request_timeout": 120,
                },
                "model_info": {
                    "supports_vision": True,
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                    "mode": "chat",
                    "max_tokens": 524288,
                    "max_input_tokens": 524288,
                    "input_cost_per_token": 0.00000014,
                    "output_cost_per_token": 0.00000028,
                    "is_public_model_group": True,
                },
            },
        ]

    # Purge stale ollama-deepseek DB entries before re-registering.
    # Mirrors the agent-* purge pattern above — delete all, then register fresh.
    try:
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            logger.warning(
                "DATABASE_URL is not set; skipping purge of stale ollama-deepseek-* DB entries"
            )
        else:
            await _purge_stale_deployments(db_url, "ollama-deepseek-%")
            logger.info(
                "🧹 Purged stale ollama-deepseek-* DB entries before registration"
            )
    except Exception as e:
        logger.warning(f"Failed to purge stale ollama DB entries (non-fatal): {e}")

    client = get_http_client()
    registered = 0
    failed = 0
    for payload in ollama_models:
        try:
            r = await client.post(
                f"{admin_url}/model/new", headers=headers, json=payload, timeout=10.0
            )
            if r.status_code in (200, 201):
                registered += 1
            else:
                failed += 1
                logger.warning(
                    f"model/new {payload['model_name']}: HTTP {r.status_code} — {r.text[:200]}"
                )
        except Exception as e:
            failed += 1
            logger.warning(f"Failed to register {payload['model_name']}: {e}")
    logger.info(f"📊 Ollama DB registration: {registered} registered, {failed} failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: wait for LiteLLM readiness, then sync free-model roster."""
    # Initialize shared HTTPX client and sync cooldowns from Redis/Valkey
    get_http_client()
    await sync_cooldowns_from_valkey()

    litellm_ready_url = f"{LITELLM_URL}/health/readiness"
    litellm_master_key = os.getenv("LITELLM_MASTER_KEY", "")
    max_wait = 180
    logger.info(f"⏳ Waiting for LiteLLM on {LITELLM_URL} (max {max_wait}s)...")
    client = get_http_client()
    for i in range(max_wait):
        try:
            r = await client.get(litellm_ready_url, timeout=2.0)
            if r.status_code == 200:
                logger.info(f"✅ LiteLLM ready after {i + 1}s")
                break
        except Exception:
            pass
        await asyncio.sleep(1)
    else:
        logger.warning(
            "⚠️  LiteLLM not ready within timeout — proceeding without roster sync"
        )

    # Sync free-model roster into LiteLLM (non-fatal if it fails)
    if litellm_master_key:
        try:
            await sync_adaptive_router_roster(litellm_master_key)
        except Exception as e:
            logger.error(f"Roster sync failed: {e}")

        # Register static ollama models via /model/new so they become DB models.
        # The ollama_chat provider's internal model lookup overrides static config
        # model_info at the group aggregation level (/model_group/info), causing
        # features and token limits to show as null/false. DB models get priority.
        try:
            await _register_ollama_models_in_db(litellm_master_key)
        except Exception as e:
            logger.warning(f"Ollama DB registration failed (non-fatal): {e}")

    # Start background task before yield so it runs during app lifetime
    task = asyncio.create_task(push_aggregate_scores())

    try:
        yield
    finally:
        # Cancel background score task
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Close shared HTTPX client
        global _http_client
        if _http_client is not None:
            await _http_client.aclose()
            _http_client = None

        # Close classifier client
        global _classifier_client
        if _classifier_client is not None:
            await _classifier_client.aclose()
            _classifier_client = None

        # Close llama client
        global _llama_client
        if _llama_client is not None:
            await _llama_client.aclose()
            _llama_client = None

        # Close Redis client
        global _redis_client
        if _redis_client is not None and _redis_client is not False:
            await _redis_client.aclose()
            _redis_client = None

        # Flush any buffered stats/timeline on clean shutdown (always runs)
        await save_persisted_stats(force=True)
        try:
            timeline_path = os.path.join(
                os.path.dirname(CONFIG_PATH), "router_timeline.json"
            )
            await _atomic_write_json_async(timeline_path, stats["timeline"])
        except Exception as e:
            logger.warning(f"Failed to persist timeline on shutdown: {e}")


app = FastAPI(title="LLM Triage Router", lifespan=lifespan)


async def check_tcp_port(ip: str, port: int) -> bool:
    """Verifies if a TCP port is open locally asynchronously."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=0.5)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


async def check_http_endpoint(url: str) -> bool:
    """Verifies if an HTTP endpoint is responsive."""
    try:
        client = get_http_client()
        r = await client.get(url, timeout=3.0)
        return r.status_code < 500
    except Exception:
        return False


async def _check_llama_health() -> bool:
    """Check llama-server health using the llama client (verify=False for self-signed TLS)."""
    try:
        client = get_llama_client()
        r = await client.get(f"{LLAMA_SERVER_URL}/health", timeout=3.0)
        return r.status_code < 500
    except Exception:
        return False


async def classify_request(
    prompt: str, bypass_cache: bool = False, langfuse_trace_id: str | None = None
) -> tuple[str, float, bool, str]:
    """Queries the local fast Qwen instance to classify request complexity with TTL caching.

    When langfuse_trace_id is provided, the classifier HTTP call is wrapped in a child
    observation (span) so latency and output appear as a nested span in Langfuse traces.

    Args:
        prompt: The user prompt to classify.
        bypass_cache: If True, skip the in-memory TTL cache.
        langfuse_trace_id: Optional trace ID to associate with the classification span.

    Returns:
        A tuple containing (decision, latency_ms, cache_hit, raw_output).
    """
    global triage_cache, stats

    # Normalize the prompt text for cache mapping
    normalized_prompt = prompt.strip().lower()

    # 1. Check in-memory TTL cache (outside lock)
    if not bypass_cache and normalized_prompt in triage_cache:
        cached_decision, cached_time = triage_cache[normalized_prompt]
        if time.time() - cached_time < CACHE_TTL_SECONDS:
            logger.info(
                f"⚡ Triage Cache Hit for prompt: '{normalized_prompt[:50]}...' -> routed to '{cached_decision}'"
            )
            stats["cache_hits"] = stats.get("cache_hits", 0) + 1
            await save_persisted_stats()
            return cached_decision, 0.0, True, cached_decision  # was_cache_hit=True

    start_time = time.time()

    # 2. Query llama-server sequentially using a lock to prevent concurrent slot conflicts
    async with classification_lock:
        # Check cache again just in case a concurrent request finished and cached it while we waited
        if not bypass_cache and normalized_prompt in triage_cache:
            cached_decision, cached_time = triage_cache[normalized_prompt]
            if time.time() - cached_time < CACHE_TTL_SECONDS:
                logger.info(
                    f"⚡ Triage Cache Hit (post-queue) for prompt: '{normalized_prompt[:50]}...' -> routed to '{cached_decision}'"
                )
                stats["cache_hits"] = stats.get("cache_hits", 0) + 1
                await save_persisted_stats()
                return cached_decision, 0.0, True, cached_decision

        try:
            client = get_classifier_client()
            try:
                max_chars = max(0, int(os.getenv("CLASSIFIER_INPUT_MAX_CHARS", "300")))
            except ValueError:
                max_chars = 300
            truncated_prompt = prompt[:max_chars] if len(prompt) > max_chars else prompt
            payload = {
                "model": router_model_name,
                "messages": [{"role": "user", "content": system_prompt + truncated_prompt}],
                "temperature": 0.0,
                "max_tokens": 15,
            }
            headers = {"Authorization": f"Bearer {router_api_key}"}

            logger.info(
                f"Classifying intent via {router_api_base} using model {router_model_name}..."
            )

            # --- Langfuse child span: classifier call ---
            class_span_obj = None
            if langfuse_trace_id:
                lf_cls = get_langfuse()
                if lf_cls:
                    try:
                        class_span_obj = lf_cls.start_observation(
                            trace_context={"trace_id": langfuse_trace_id},
                            name="classifier-qwen",
                            input=prompt[:200],
                            metadata={"model": router_model_name},
                            level="DEFAULT",
                        )
                    except Exception:
                        pass

            response = await client.post(
                f"{router_api_base}/chat/completions",
                json=payload,
                headers=headers,
                timeout=120.0,
            )

            latency = (time.time() - start_time) * 1000.0

            if response.status_code != 200:
                _end_child_span(class_span_obj, 
                    output={
                        "status": response.status_code,
                        "error": "classification_failed",
                    },
                    metadata={"latency_ms": latency},
                )
                logger.error(
                    f"Classification failed with status {response.status_code}: {response.text}"
                )
                return "agent-advanced-core", latency, False, "advanced (fallback)"

            result = response.json()
            message_obj = result["choices"][0]["message"]
            content = message_obj.get("content") or ""
            content_clean = content.strip()
            raw_result = content_clean if content_clean else "advanced (empty)"
            logger.info(f"Raw classifier response: '{raw_result}'")

            # 5-tier grammar parsing (was 3-tier, missed medium + advanced)
            valid_tiers = {
                "agent-simple-core",
                "agent-medium-core",
                "agent-complex-core",
                "agent-reasoning-core",
                "agent-advanced-core",
            }
            if content_clean in valid_tiers:
                decision = content_clean
            else:
                decision = "agent-advanced-core"

            # Finalize classifier child span
            _end_child_span(class_span_obj, 
                output={"tier": decision, "raw": raw_result},
                metadata={"latency_ms": latency},
            )

            # Store in cache
            triage_cache[normalized_prompt] = (decision, time.time())
            return decision, latency, False, raw_result

        except Exception as e:
            latency = (time.time() - start_time) * 1000.0
            logger.error(f"Exception during classification: {e}")
            return "agent-advanced-core", latency, False, "advanced (exception)"


def _read_json_file_sync(file_path: str) -> dict:
    """Helper to read JSON files synchronously."""
    with open(file_path, "r") as f:
        return json.load(f)



async def get_gemini_oauth_status() -> dict:
    """Returns structured OAuth status for the dashboard banner."""
    try:
        if not await asyncio.to_thread(os.path.exists, GEMINI_OAUTH_CREDS_PATH):
            return {
                "status": "missing",
                "detail": "No oauth_creds.json found",
                "expiry_ms": 0,
            }
        data = await asyncio.to_thread(_read_json_file_sync, GEMINI_OAUTH_CREDS_PATH)
        access_token = data.get("access_token")
        expiry_ms = data.get("expiry_date", 0)
        current_ms = int(time.time() * 1000)
        if not access_token:
            return {
                "status": "missing",
                "detail": "No access token in file",
                "expiry_ms": 0,
            }
        diff_sec = (expiry_ms - current_ms) / 1000.0
        if diff_sec > 0:
            # Token is valid — compute human-readable remaining time
            if diff_sec < 60:
                remaining = f"{int(diff_sec)}s"
            elif diff_sec < 3600:
                remaining = f"{int(diff_sec // 60)}m {int(diff_sec % 60)}s"
            else:
                remaining = f"{int(diff_sec // 3600)}h {int((diff_sec % 3600) // 60)}m"
            return {
                "status": "valid",
                "detail": f"Expires in {remaining}",
                "expiry_ms": expiry_ms,
            }
        else:
            # Token is expired — compute human-readable elapsed time
            elapsed = abs(diff_sec)
            if elapsed < 3600:
                ago = f"{int(elapsed // 60)} minutes ago"
            elif elapsed < 86400:
                ago = f"{int(elapsed // 3600)} hours ago"
            else:
                ago = f"{int(elapsed // 86400)} days ago"
            return {
                "status": "expired",
                "detail": f"Expired {ago}",
                "expiry_ms": expiry_ms,
            }
    except Exception as e:
        return {"status": "error", "detail": str(e), "expiry_ms": 0}


def map_tool_to_category(tool_name: str) -> str:
    """Groups low-level developer tool names into the five high-level dashboard metrics."""
    name = tool_name.lower().strip()
    if "__" in name:
        name = name.split("__")[-1]

    if "tree" in name or "list_dir" in name or "list-dir" in name:
        return "tree"
    elif (
        "shell" in name
        or "command" in name
        or "cmd" in name
        or "execute" in name
        or "run" in name
    ):
        return "shell"
    elif (
        "write" in name
        or "edit" in name
        or "create" in name
        or "patch" in name
        or "replace" in name
        or "save" in name
    ):
        return "write"
    elif (
        "view" in name
        or "read" in name
        or "cat" in name
        or "grep" in name
        or "search" in name
        or "find" in name
    ):
        return "view"
    return "other"


def detect_active_tool(body: dict) -> str:
    """Inspects request payload messages to identify which developer tool is currently being invoked."""
    messages = body.get("messages", [])

    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role in ("tool", "function"):
            name = msg.get("name")
            if not name:
                # Look backwards for the assistant tool request that holds the matching id
                tool_call_id = msg.get("tool_call_id")
                if tool_call_id:
                    for prev_msg in reversed(messages[:idx]):
                        if not isinstance(prev_msg, dict):
                            continue
                        if prev_msg.get("role") == "assistant":
                            tcalls = prev_msg.get("tool_calls") or []
                            if isinstance(tcalls, list):
                                for tc in tcalls:


                                    if (
                                        isinstance(tc, dict)
                                        and tc.get("id") == tool_call_id
                                    ):
                                        fn = tc.get("function")


                                        if isinstance(fn, dict):
                                            name = fn.get("name")
                                        break
                        if name:
                            break
            name = name or "other"
            return map_tool_to_category(name)

        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls and isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        fn = tc.get("function")
                        name = (
                            fn.get("name") if isinstance(fn, dict) else None
                        ) or "other"
                        return map_tool_to_category(name)

    # Fallback to keyphrase scanning in the user message
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user":
            content = str(msg.get("content", "")).lower()
            if "tree" in content or "files" in content:
                return "tree"
            elif "shell" in content or "run" in content or "cmd" in content:
                return "shell"
            elif "write" in content or "create file" in content:
                return "write"
            elif "view" in content or "read" in content or "cat" in content:
                return "view"
    return "none"


@dataclass
class ToolUsageRecord:
    """Data class representing a single tool usage record for metrics tracking."""
    tool_name: str
    prompt_tokens: int
    completion_tokens: int
    model: str
    latency_ms: float
    route: str = "litellm_fallback"


def record_tool_usage(usage: ToolUsageRecord):
    """Accumulates token counts in memory for active tools and tracks request timelines.

    File writes are offloaded to a thread pool executor to avoid blocking the
    event loop. The 2-second throttle is checked synchronously before
    dispatching.
    """
    if usage.tool_name == "none":
        usage.tool_name = "other"

    total = usage.prompt_tokens + usage.completion_tokens
    stats["tool_tokens"][usage.tool_name] = stats["tool_tokens"].get(usage.tool_name, 0) + total

    # Save global prompt/completion metrics
    stats["prompt_tokens"] = stats.get("prompt_tokens", 0) + usage.prompt_tokens
    stats["completion_tokens"] = stats.get("completion_tokens", 0) + usage.completion_tokens

    # Track routing path distribution
    if "routing_paths" not in stats:
        stats["routing_paths"] = {"google_oauth_direct": 0, "litellm_fallback": 0}
    stats["routing_paths"][usage.route] = stats["routing_paths"].get(usage.route, 0) + 1

    # Append to timeline event stack (in-memory ring buffer + persistent disk backup)
    event = {
        "timestamp": time.strftime("%H:%M:%S"),
        "tool": usage.tool_name,
        "model": usage.model,
        "route": usage.route,
        "tokens": total,
        "latency_ms": int(usage.latency_ms),
    }
    stats["timeline"].append(event)
    if len(stats["timeline"]) > 15:
        stats["timeline"].pop(0)

    # Fire-and-forget stats write via save_persisted_stats (non-blocking).
    # Store the task reference in _background_tasks to prevent GC before completion (RUF006).
    now = time.monotonic()
    try:
        loop = asyncio.get_running_loop()
        _task = loop.create_task(save_persisted_stats())
        _background_tasks.add(_task)
        _task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        # No running event loop (e.g. during early startup) — fall back to sync write
        try:
            global _last_stats_save
            if now - _last_stats_save >= 2.0:
                _atomic_write_json_sync(STATS_JSON_PATH, stats)
                _last_stats_save = now
        except Exception as e:
            logger.error(f"Failed to persist stats to disk: {e}")

    # Throttle timeline file writes independently of the stats file (max once per 2 s)
    timeline_path = os.path.join(os.path.dirname(CONFIG_PATH), "router_timeline.json")
    if now - getattr(record_tool_usage, "_last_save", 0.0) >= 2.0:
        try:
            loop = asyncio.get_running_loop()
            fut = loop.run_in_executor(
                None,
                _atomic_write_json_sync,
                timeline_path,
                copy.deepcopy(list(stats["timeline"])),
            )
            record_tool_usage._last_save = now

            def done_callback(f):
                """Log any uncaught exceptions returned from the background timeline executor thread."""
                try:
                    f.result()
                except Exception as e:
                    logger.warning(f"Failed to persist timeline in background: {e}")

            fut.add_done_callback(done_callback)
        except RuntimeError:
            # No running event loop — fall back to sync write
            try:
                _atomic_write_json_sync(timeline_path, stats["timeline"])
                record_tool_usage._last_save = now
            except Exception as e:
                logger.warning(f"Failed to persist timeline: {e}")
        except Exception as e:
            logger.warning(f"Failed to persist timeline: {e}")


def get_goose_sessions() -> list:
    """Queries the live mounted SQLite goose database to fetch the latest agentic sessions."""
    sessions_list = []
    db_path = "/config/goose_sessions/sessions/sessions.db"
    if not os.path.exists(db_path):
        return []
    try:
        import sqlite3

        conn = sqlite3.connect(db_path, timeout=1.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, description, created_at, updated_at, accumulated_total_tokens, goose_mode
            FROM sessions
            ORDER BY updated_at DESC
            LIMIT 5
        """)
        for row in cursor.fetchall():
            sessions_list.append(dict(row))
        conn.close()
    except Exception as e:
        logger.error(f"Failed to query goose sessions SQLite DB: {e}")
    return sessions_list


async def get_llamacpp_metrics() -> dict:
    """Fetches live model inventory and slot statistics from the local llama-server."""
    result = {"models": [], "slots": [], "build": "unknown"}
    try:
        client = get_llama_client()
        # Fetch model list
        r = await client.get(f"{LLAMA_SERVER_URL}/v1/models", timeout=3.0)
        if r.status_code == 200:
            data = r.json()
            for m in data.get("data", []):
                meta = m.get("meta", {})
                status_obj = m.get("status", {})
                result["models"].append(
                    {
                        "id": m.get("id", "?"),
                        "status": status_obj.get("value", "unknown"),
                        "n_params": meta.get("n_params"),
                        "n_ctx": meta.get("n_ctx"),
                        "size_bytes": meta.get("size"),
                        "n_embd": meta.get("n_embd"),
                    }
                )
        # Fetch props for build info
        r2 = await client.get(f"{LLAMA_SERVER_URL}/props", timeout=3.0)
        if r2.status_code == 200:
            props = r2.json()
            result["build"] = props.get("build_info", "unknown")
        # Fetch slots for the loaded model, falling back to the first available model if all are unloaded
        loaded = [m["id"] for m in result["models"] if m["status"] == "loaded"]
        slot_model = (
            loaded[0]
            if loaded
            else (result["models"][0]["id"] if result["models"] else None)
        )
        if slot_model:
            r3 = await client.get(
                f"{LLAMA_SERVER_URL}/slots?model={slot_model}", timeout=3.0
            )
            if r3.status_code == 200:
                slots_data = r3.json()
                for s in slots_data:
                    next_tok = s.get("next_token")
                    decoded = 0
                    if isinstance(next_tok, dict):
                        decoded = next_tok.get("n_decoded", 0)
                    elif isinstance(next_tok, list) and next_tok:
                        first_tok = next_tok[0]
                        if isinstance(first_tok, dict):
                            decoded = first_tok.get("n_decoded", 0)
                    result["slots"].append({
                        "id": s.get("id", 0),
                        "is_processing": s.get("is_processing", False),
                        "n_ctx": s.get("n_ctx", 0),
                        "n_prompt_tokens": s.get("n_prompt_tokens", 0),
                        "n_prompt_processed": s.get("n_prompt_tokens_processed", 0),
                        "n_decoded": decoded,
                        "speculative": s.get("speculative", False),
                    })
    except Exception as e:
        logger.warning(f"Failed to fetch llama.cpp metrics: {e}")
    return result


# In-Memory Cache for OpenRouter Free Model list to prevent slow page renders
free_model_cache = {"data": None, "last_fetched": 0.0}
FREE_MODEL_CACHE_TTL = 3600  # Refresh cache every 1 hour

# --- Artificial Analysis Agentic Index scores cache ---
_AA_SCORES_CACHE: dict[str, float] = {}
_AA_SCORES_LOADED = False


def _load_aa_scores():
    """Load the Artificial Analysis agentic scores cache from local config."""
    global _AA_SCORES_CACHE, _AA_SCORES_LOADED
    if _AA_SCORES_LOADED:
        return
    try:
        import json

        scores_path = os.path.join(os.path.dirname(__file__), "aa_scores.json")
        with open(scores_path) as f:
            data = json.load(f)
            _AA_SCORES_CACHE = data.get("scores", {})
            _AA_SCORES_LOADED = True
            logger.info(
                f"📊 Loaded {len(_AA_SCORES_CACHE)} AA agentic index scores from {scores_path}"
            )
    except Exception as e:
        logger.warning(f"Could not load AA scores cache: {e}")
        _AA_SCORES_LOADED = True  # don't retry


def compute_free_model_score(m: dict) -> float:
    """Return AA agentic index score, or a low default for unknown models."""
    mid = m.get("id", "")
    return _AA_SCORES_CACHE.get(mid, 25.0)


def _save_free_models_roster(free_models: list[dict]) -> None:
    """Persist the full sorted free model list so Ralph can try alternatives."""
    import json as _json
    import datetime as _dt
    payload = {
        "models": free_models,
        "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "count": len(free_models)
    }
    try:
        with open("/config/router_dir/free_models_roster.json", "w") as f:
            _json.dump(payload, f, indent=2)
    except Exception:
        pass


def _save_best_model_to_disk(best_model: dict) -> None:
    """Persist the best free model to a JSON file Ralph can read."""
    import json as _json
    import datetime as _dt
    payload = {**best_model, "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")}
    try:
        with open("/config/router_dir/best_free_model.json", "w") as f:
            _json.dump(payload, f, indent=2)
    except Exception:
        pass  # Non-critical — Ralph falls back gracefully


async def get_best_free_model() -> dict:
    """Fetches currently free models from OpenRouter, matches against agentic scores, and returns the highest."""
    global free_model_cache
    if not _AA_SCORES_LOADED:
        await asyncio.to_thread(_load_aa_scores)
    now = time.time()

    # Check if cache is still valid
    if free_model_cache["data"] and (now - free_model_cache["last_fetched"] < FREE_MODEL_CACHE_TTL):
        await asyncio.to_thread(_save_best_model_to_disk, free_model_cache["data"])
        return free_model_cache["data"]

    fallback_best = {
        "id": "moonshotai/kimi-k2.6:free",
        "name": "MoonshotAI: Kimi K2.6 (free)",
        "score": 82.5,
        "context_length": 131072,
        "is_fallback": True,
    }

    try:
        client = get_http_client()
        r = await client.get("https://openrouter.ai/api/v1/models", timeout=2.0)
        if r.status_code == 200:
            data = r.json().get("data", [])
            best_model = None
            max_score = -1.0
            all_free = []

            for m in data:
                mid = m.get("id", "")
                # Denylist: skip stale/unreliable free tier models
                _denylist_prefixes = (
                    "meta-llama/",
                    "nousresearch/hermes-3-llama",
                )
                if any(mid.startswith(p) for p in _denylist_prefixes):
                    continue
                pricing = m.get("pricing", {})
                # Standard pricing is string or float
                p_prompt = pricing.get("prompt")
                p_comp = pricing.get("completion")

                # Verify if it is free
                if p_prompt in ("0", 0, "0.0", 0.0) and p_comp in ("0", 0, "0.0", 0.0):
                    score = compute_free_model_score(m)
                    entry = {
                        "id": mid,
                        "name": m.get("name", mid),
                        "score": score,
                        "context_length": m.get("context_length", 0),
                    }
                    all_free.append(entry)
                    if score > max_score:
                        max_score = score
                        best_model = {**entry, "is_fallback": False}
            # Sort by score descending
            all_free.sort(key=lambda x: x["score"], reverse=True)
            await asyncio.to_thread(_save_free_models_roster, all_free)
            if best_model:
                free_model_cache["data"] = best_model
                free_model_cache["last_fetched"] = now
                logger.info(f"🏆 Top free agentic model resolved: {best_model['id']} with score {best_model['score']}")
                await asyncio.to_thread(_save_best_model_to_disk, best_model)
                return best_model
    except Exception as e:
        logger.warning(f"Failed to query live OpenRouter models API for Agentic Index: {e}")
    
    await asyncio.to_thread(_save_best_model_to_disk, fallback_best)
    return fallback_best


def get_pie_chart_gradient() -> str:
    """Computes a CSS conic-gradient representing the dynamic token distribution across developer tools."""
    total_tokens = sum(stats["tool_tokens"].values())
    if total_tokens == 0:
        return "background: rgba(255, 255, 255, 0.05);"

    current_angle = 0.0
    gradient_parts = []
    
    for tool, tokens in stats["tool_tokens"].items():
        if tokens > 0:
            pct = (tokens / total_tokens) * 100.0
            next_angle = current_angle + pct
            color = TOOL_COLORS.get(tool, "#94a3b8")
            gradient_parts.append(f"{color} {current_angle:.1f}% {next_angle:.1f}%")
            current_angle = next_angle

    if not gradient_parts:
        return "background: rgba(255, 255, 255, 0.05);"

    return f"background: conic-gradient({', '.join(gradient_parts)});"


@app.api_route("/v1/memory{path:path}", methods=["GET", "POST", "DELETE", "PUT"])
async def proxy_memory(request: Request, path: str = ""):
    """Proxies memory API calls to the LiteLLM gateway on port 4000."""
    litellm_base = f"http://127.0.0.1:{os.getenv('LITELLM_PORT') or '4000'}/v1/memory"

    # Resolve the destination URL
    url = f"{litellm_base}{path}"

    # Prepare query parameters
    query_params = dict(request.query_params)

    # Read request body
    body = await request.body()

    # Resolve authorization header using LiteLLM master key
    litellm_key = os.getenv("LITELLM_MASTER_KEY")
    headers = {
        "Authorization": f"Bearer {litellm_key}",
        "Content-Type": request.headers.get("content-type", "application/json"),
    }

    logger.info(
        f"Proxying memory request: {request.method} {url} with params {query_params}"
    )

    try:
        client = get_http_client()
        r = await client.request(
            method=request.method,
            url=url,
            params=query_params,
            content=body,
            headers=headers,
            timeout=30.0,
        )

        # Return response matching status and headers
        response_headers = dict(r.headers)
        # Exclude standard headers that FastAPI/uvicorn will manage
        for h in [
            "content-encoding",
            "content-length",
            "transfer-encoding",
            "connection",
        ]:
            response_headers.pop(h, None)

        return Response(
            content=r.content, status_code=r.status_code, headers=response_headers
        )
    except Exception as e:
        logger.error(f"Failed to proxy memory request: {e}")
        raise HTTPException(status_code=502, detail="Memory proxy failed")


@app.get("/v1/models")
async def proxy_models():
    """Proxy /v1/models to LiteLLM, injecting llm-routing-auto-free as the first entry."""
    litellm_key = os.getenv("LITELLM_MASTER_KEY")
    try:
        client = get_http_client()
        auth_header = "Bearer " + (litellm_key or "")
        r = await client.get(
            f"{LITELLM_URL}/v1/models",
            headers={"Authorization": auth_header},
            timeout=10.0,
        )

        if r.status_code == 200:
            try:
                data = r.json()
                if isinstance(data, dict) and "data" in data:
                    # Inject llm-routing-* models at the top of the list.
                    # Auto models (classifier pipeline) first, then direct models.
                    # Context lengths aligned with downstream model targets:
                    # - auto-free / auto-agy: 262144 (262K)
                    # - auto-ollama / auto-agy-ollama / llm-routing-ollama: 524288 (512K)
                    # - llm-routing-agy: 1048576 (1M)
                    routing_models = [
                        {
                            "id": "llm-routing-auto-free",
                            "object": "model",
                            "created": 0,
                            "owned_by": "llm-routing",
                            "context_length": 262144,
                        },
                        {
                            "id": "llm-routing-auto-agy",
                            "object": "model",
                            "created": 0,
                            "owned_by": "llm-routing",
                            "context_length": 262144,
                        },
                        {
                            "id": "llm-routing-auto-ollama",
                            "object": "model",
                            "created": 0,
                            "owned_by": "llm-routing",
                            "context_length": 524288,
                        },
                        {
                            "id": "llm-routing-auto-agy-ollama",
                            "object": "model",
                            "created": 0,
                            "owned_by": "llm-routing",
                            "context_length": 524288,
                        },
                        {
                            "id": "llm-routing-agy",
                            "object": "model",
                            "created": 0,
                            "owned_by": "llm-routing",
                            "context_length": 1048576,
                        },
                        {
                            "id": "llm-routing-ollama",
                            "object": "model",
                            "created": 0,
                            "owned_by": "llm-routing",
                            "context_length": 524288,
                        },
                    ]
                    for entry in reversed(routing_models):
                        data["data"].insert(0, entry)
                    return JSONResponse(content=data, status_code=200)
            except Exception as parse_err:
                logger.warning(
                    f"Failed to parse /v1/models JSON despite status 200: {parse_err}"
                )

        # If not 200, or parsing failed, return the raw response with appropriate headers
        response_headers = dict(r.headers)
        for h in [
            "content-encoding",
            "content-length",
            "transfer-encoding",
            "connection",
        ]:
            response_headers.pop(h, None)
        return Response(
            content=r.content, status_code=r.status_code, headers=response_headers
        )
    except Exception as e:
        logger.error(f"Failed to proxy /v1/models: {e}")
        raise HTTPException(status_code=502, detail="Model proxy failed")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """Handle incoming OpenAI-compatible chat completions requests.

    Routes requests dynamically based on triage logic, handling cascading fallbacks,
    caching, and premium proxying (agy/ollama).

    Args:
        request: The incoming FastAPI Request object.

    Returns:
        A StreamingResponse or JSONResponse containing the model completion.
    """
    global stats
    start_time = time.time()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    await sync_cooldowns_from_valkey()

    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="Empty messages list")

    # Detect current active developer tool from request body
    active_tool = detect_active_tool(body)

    # Extract last user message for complexity triage
    last_user_message = ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user":
            content = msg.get("content") or ""
            if isinstance(content, list):
                content = "".join(
                    block.get("text") or ""
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            last_user_message = str(content)
            break

    # Known tier names that can be routed directly (bypass classifier)
    DIRECT_TIERS = {
        "agent-simple-core",
        "agent-medium-core",
        "agent-complex-core",
        "agent-reasoning-core",
        "agent-advanced-core",
        "llm-routing-agy",
    }

    AUTO_MODELS = {
        "llm-routing-auto-free",
        "llm-routing-auto-agy",
        "llm-routing-auto-ollama",
        "llm-routing-auto-agy-ollama",
    }

    client_model = body.get("model", "llm-routing-auto-free")

    # Extract session_id and user_id for Langfuse tracing
    _trace_session_id = (
        body.get("session_id")
        or body.get("session")
        or request.headers.get("x-session-id")
    )
    if _trace_session_id:
        _trace_session_id = str(_trace_session_id)
    _trace_user_id = (
        body.get("user")
        or request.headers.get("x-user-id")
    )
    if _trace_user_id:
        _trace_user_id = str(_trace_user_id)

    # --- Langfuse parent trace: create early so child spans can reference it ---
    langfuse_trace_id = None
    parent_obs = None
    _prop_ctx = None
    _is_streaming = body.get("stream", False)
    lf = get_langfuse()
    if lf:
        try:
            langfuse_trace_id = lf.create_trace_id(
                seed=str(uuid.uuid4())
            )
            # Propagate session_id/user_id via Langfuse's native session mechanism.
            # For non-streaming: enter here (same asyncio task, contextvars work).
            # For streaming: each generator creates its own context in its own task
            # because OpenTelemetry contextvars are task-isolated.
            if not _is_streaming:
                _prop_ctx = _make_prop_ctx(_trace_session_id, _trace_user_id)
                if _prop_ctx is not None:
                    _prop_ctx.__enter__()
            parent_obs = lf.start_observation(
                trace_context={"trace_id": langfuse_trace_id},
                name=f"triage-{client_model}",
                input=last_user_message[:200],
                level="DEFAULT",
                metadata={
                    "client_model": client_model,
                    "environment": os.getenv("ENVIRONMENT", "production"),
                },
            )
        except Exception as e:
            logger.warning(f"Langfuse trace init failed (non-fatal): {e}")
            langfuse_trace_id = None
            parent_obs = None
            if _prop_ctx:
                _prop_ctx = _close_prop_ctx(_prop_ctx)

    try:
        _non_streaming_finalized = False
        if client_model in AUTO_MODELS or client_model == "llm-routing-ollama":
            # Full pipeline: classify → route to best tier
            bypass_cache = request.headers.get("x-bypass-cache") == "true"
            (
                target_model,
                triage_latency,
                was_cache_hit,
                raw_classification,
            ) = await classify_request(
                last_user_message,
                bypass_cache=bypass_cache,
                langfuse_trace_id=langfuse_trace_id,
            )
            logger.info(f"Triage decision (auto/gated): Routing to -> '{target_model}'")
        elif client_model in DIRECT_TIERS:
            # Direct routing: client knows what tier they want, skip classifier
            target_model = client_model
            triage_latency = 0.0
            was_cache_hit = False
            raw_classification = f"direct ({client_model})"
            logger.info(
                f"Direct routing: Client requested '{client_model}', skipping classifier"
            )
        else:
            # guard: end parent obs before raising
            _end_parent_obs(parent_obs,
                output={"error": f"Unknown model: {client_model}"})
            _close_prop_ctx(_prop_ctx)
            _non_streaming_finalized = True
            raise HTTPException(
                status_code=400,
                detail=f"Unknown model '{client_model}'. Use 'llm-routing-auto-free' for automatic routing, "
                f"or one of: {', '.join(sorted(DIRECT_TIERS))}",
            )

        # Update in-memory statistics
        stats["total_requests"] += 1
        stats["last_triage_decision"] = target_model
        stats["total_triage_time_ms"] += triage_latency
        stats["avg_triage_latency_ms"] = (
            stats["total_triage_time_ms"] / stats["total_requests"]
        )

        if target_model == "agent-simple-core":
            stats["simple_requests"] = stats.get("simple_requests", 0) + 1
        elif target_model == "agent-medium-core":
            stats["medium_requests"] = stats.get("medium_requests", 0) + 1
        elif target_model == "agent-complex-core":
            stats["complex_requests"] = stats.get("complex_requests", 0) + 1
        elif target_model == "agent-reasoning-core":
            stats["reasoning_requests"] = stats.get("reasoning_requests", 0) + 1
        elif target_model == "agent-advanced-core":
            stats["advanced_requests"] = stats.get("advanced_requests", 0) + 1
        await save_persisted_stats()

        # Update the parent Langfuse observation with classification results
        if parent_obs:
            try:
                parent_obs.update(
                    output={"tier": target_model, "raw": raw_classification},
                    metadata={
                        "triage_latency_ms": round(triage_latency, 2),
                        "cache_hit": was_cache_hit,
                        "total_requests": stats["total_requests"],
                    },
                )
            except Exception as e:
                logger.warning(f"Langfuse trace update failed (non-fatal): {e}")

        # --- PREMIUM PROXY ROUTES ---
        # agy: triggered unconditionally for llm-routing-agy (direct).
        #      For AUTO models: only triggered when classifier picks agent-advanced-core
        #      or agent-reasoning-core.
        #      Reasoning tier → gemini-3.5-flash (single tier, low thinking)
        #      Advanced tier → gemini-3.5-flash → claude-opus-4.6 (full 2-tier chain)
        #      Proxied to host agy daemon on port 5005.
        # ollama: triggered unconditionally for llm-routing-ollama (direct).
        #      For AUTO models: only triggered when classifier picks agent-advanced-core
        #      or agent-reasoning-core.
        #      Reasoning tier → deepseek-v4-flash (lighter, faster)
        #      Advanced tier → deepseek-v4-pro (full power)
        #      Proxied to LiteLLM as ollama-deepseek-v4-* — LiteLLM handles the
        #      native Ollama API call via its built-in ollama_chat provider.
        # Classification gating (2026-06-16): auto models skip premium proxies entirely
        # unless classified as advanced or reasoning, avoiding 4-minute agy timeouts on
        # simple/medium/complex prompts that the fast OpenRouter free tier handles better.

        should_try_agy = (
            client_model == "llm-routing-agy"  # direct — always try
            or (
                client_model in ("llm-routing-auto-agy", "llm-routing-auto-agy-ollama")
                and target_model in ("agent-advanced-core", "agent-reasoning-core")
            )
        )
        should_try_ollama = (
            client_model
            == "llm-routing-ollama"  # always try (will map to flash for complex/below)
            or (
                client_model in ("llm-routing-auto-ollama", "llm-routing-auto-agy-ollama")
                and target_model
                in ("agent-advanced-core", "agent-reasoning-core", "agent-complex-core")
            )
        )

        # --- AGY PROXY ---
        if should_try_agy:
            agy_span_obj = None
            try:
                from agy_proxy import try_agy_proxy

                last_prompt = ""
                for msg in reversed(messages):
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("role") == "user":
                        content = msg.get("content") or ""
                        if isinstance(content, list):
                            content = "".join(
                                block.get("text") or ""
                                for block in content
                                if isinstance(block, dict) and block.get("type") == "text"
                            )
                        last_prompt = str(content)
                        break

                session_id = (
                    body.get("session_id")
                    or body.get("session")
                    or request.headers.get("x-session-id")
                )
                if session_id:
                    session_id = str(session_id)

                if last_prompt:
                    # --- Langfuse child span: agy proxy ---
                    if langfuse_trace_id:
                        lf_agy = get_langfuse()
                        if lf_agy:
                            try:
                                agy_span_obj = lf_agy.start_observation(
                                    trace_context={"trace_id": langfuse_trace_id},
                                    name="agy-proxy",
                                    input=last_prompt[:200],
                                    metadata={"tier": target_model},
                                    level="DEFAULT",
                                )
                            except Exception:
                                pass

                    is_stream_requested = body.get("stream", False)
                    agy_response = await try_agy_proxy(
                        prompt=last_prompt,
                        messages=messages,
                        session_id=session_id,
                        total_timeout=300.0,
                        stream=is_stream_requested,
                        target_tier=target_model,
                        client=get_http_client(),
                        cooldown_persistence=ValkeyCooldownPersistence(),
                    )
                    if agy_response:
                        model_name = agy_response.get("model", "gemini-3.5-flash (via agy)")

                        if "stream" in agy_response:
                            # Real native stream generator
                            async def native_agy_stream_generator(stream_gen, model_name):
                                """Asynchronous generator yielding native OpenAI-compatible streaming chunks from the real agy daemon."""
                                import time
                                created_time = int(time.time())
                                chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                                token_count = 0
                                finalized = False
                                _native_agy_prop = (
                                    _make_prop_ctx(_trace_session_id, _trace_user_id)
                                    or nullcontext()
                                )
                                _native_agy_prop.__enter__()
                                try:
                                    async for token in stream_gen:
                                        if not token:
                                            continue
                                        token_count += 1
                                        chunk_data = {
                                            "id": chunk_id,
                                            "object": "chat.completion.chunk",
                                            "created": created_time,
                                            "model": model_name,
                                            "choices": [
                                                {
                                                    "index": 0,
                                                    "delta": {"content": token},
                                                    "finish_reason": None,
                                                }
                                            ],
                                        }
                                        yield f"data: {json.dumps(chunk_data)}\n\n".encode(
                                            "utf-8"
                                        )

                                    # End of stream chunk
                                    finish_data = {
                                        "id": chunk_id,
                                        "object": "chat.completion.chunk",
                                        "created": created_time,
                                        "model": model_name,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {},
                                                "finish_reason": "stop",
                                            }
                                        ],
                                    }
                                    yield f"data: {json.dumps(finish_data)}\n\n".encode(
                                        "utf-8"
                                    )
                                    yield b"data: [DONE]\n\n"

                                    # Success telemetry
                                    latency_ms = (time.time() - start_time) * 1000.0
                                    stats["total_proxy_time_ms"] += latency_ms
                                    stats["avg_proxy_latency_ms"] = (
                                        stats["total_proxy_time_ms"] / stats["total_requests"]
                                    )
                                    approx_prompt_tokens = estimate_prompt_tokens(body)

                                    record_tool_usage(ToolUsageRecord(
                                        active_tool,
                                        approx_prompt_tokens,
                                        token_count,
                                        model_name,
                                        latency_ms,
                                        route="google_oauth_direct",
                                    ))
                                    logger.info(
                                        f"✅ native agy stream succeeded: {model_name}, {latency_ms:.0f}ms"
                                    )
                                    _end_child_span(agy_span_obj, 
                                        output={
                                            "model": model_name,
                                            "tokens": token_count,
                                        },
                                        metadata={
                                            "latency_ms": latency_ms,
                                            "tier": target_model,
                                        },
                                    )
                                    # Finalize parent trace for native agy stream
                                    _end_parent_obs(parent_obs,
                                        output={"model": model_name, "stream": True,
                                                "tier": target_model, "route": "google_oauth_direct"},
                                        metadata={"latency_ms": latency_ms,
                                                  "completion_tokens": token_count})
                                    _close_prop_ctx(_native_agy_prop)
                                    finalized = True
                                except Exception as stream_err:
                                    logger.error(
                                        f"Error during native agy stream generation: {type(stream_err).__name__}"
                                    )
                                    _end_child_span(agy_span_obj, 
                                        output={"error": type(stream_err).__name__},
                                        metadata={"status": "failed"},
                                    )
                                    # End parent trace on stream error
                                    _end_parent_obs(parent_obs,
                                        output={"error": type(stream_err).__name__,
                                                "route": "google_oauth_direct", "stream": True})
                                    _close_prop_ctx(_native_agy_prop)
                                    finalized = True
                                    raise
                                finally:
                                    if not finalized:
                                        _end_child_span(agy_span_obj,
                                            output={"error": "cancelled"},
                                            metadata={"status": "cancelled"},
                                        )
                                        _end_parent_obs(parent_obs,
                                            output={"error": "cancelled",
                                                    "route": "google_oauth_direct", "stream": True})
                                        _close_prop_ctx(_native_agy_prop)

                            return StreamingResponse(
                                native_agy_stream_generator(
                                    agy_response["stream"], model_name
                                ),
                                media_type="text/event-stream",
                            )
                        else:
                            latency_ms = (time.time() - start_time) * 1000.0
                            stats["total_proxy_time_ms"] += latency_ms
                            stats["avg_proxy_latency_ms"] = (
                                stats["total_proxy_time_ms"] / stats["total_requests"]
                            )
                            usage = agy_response.get("usage") or {}
                            prompt_tokens = usage.get("prompt_tokens") or 0
                            completion_tokens = usage.get("completion_tokens") or 0
                            record_tool_usage(ToolUsageRecord(
                                active_tool,
                                prompt_tokens,
                                completion_tokens,
                                model_name,
                                latency_ms,
                                route="google_oauth_direct",
                            ))
                            logger.info(
                                f"✅ agy proxy succeeded: {model_name}, {latency_ms:.0f}ms"
                            )

                            # Finalize agy span
                            _end_child_span(agy_span_obj, 
                                output={
                                    "model": model_name,
                                    "tokens": completion_tokens,
                                },
                                metadata={
                                    "latency_ms": latency_ms,
                                    "tier": target_model,
                                },
                            )

                            if is_stream_requested:
                                # Robust fallback: simulate stream if we requested stream but got buffered response
                                content = (agy_response.get("choices") or [{}])[0].get(
                                    "message", {}
                                ).get("content") or ""

                                async def agy_stream_generator():
                                    """Asynchronous generator yielding simulated OpenAI-compatible streaming chunks from a static agy response."""
                                    created_time = int(time.time())
                                    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                                    chunk_size = 40
                                    finalized = False
                                    _agy_gen_prop = (
                                        _make_prop_ctx(_trace_session_id, _trace_user_id)
                                        or nullcontext()
                                    )
                                    _agy_gen_prop.__enter__()
                                    try:
                                        for i in range(0, len(content), chunk_size):
                                            chunk_text = content[i : i + chunk_size]
                                            chunk_data = {
                                                "id": chunk_id,
                                                "object": "chat.completion.chunk",
                                                "created": created_time,
                                                "model": model_name,
                                                "choices": [
                                                    {
                                                        "index": 0,
                                                        "delta": {"content": chunk_text},
                                                        "finish_reason": None,
                                                    }
                                                ],
                                            }
                                            yield f"data: {json.dumps(chunk_data)}\n\n".encode(
                                                "utf-8"
                                            )
                                            await asyncio.sleep(0.005)

                                        finish_data = {
                                            "id": chunk_id,
                                            "object": "chat.completion.chunk",
                                            "created": created_time,
                                            "model": model_name,
                                            "choices": [
                                                {
                                                    "index": 0,
                                                    "delta": {},
                                                    "finish_reason": "stop",
                                                }
                                            ],
                                        }
                                        yield f"data: {json.dumps(finish_data)}\n\n".encode(
                                            "utf-8"
                                        )
                                        yield b"data: [DONE]\n\n"
                                        # Finalize parent trace for simulated agy stream
                                        _end_parent_obs(parent_obs,
                                            output={"model": model_name, "stream": True,
                                                    "tier": target_model, "route": "google_oauth_direct"},
                                            metadata={"latency_ms": latency_ms,
                                                      "completion_tokens": len(content) // 4})
                                        _close_prop_ctx(_agy_gen_prop)
                                        finalized = True
                                    except Exception as e:
                                        logger.error(
                                            f"Error during agy stream generation: {type(e).__name__}"
                                        )
                                        _end_parent_obs(parent_obs,
                                            output={"error": type(e).__name__,
                                                    "route": "google_oauth_direct", "stream": True})
                                        _close_prop_ctx(_agy_gen_prop)
                                        finalized = True
                                        raise
                                    finally:
                                        if not finalized:
                                            _end_parent_obs(parent_obs,
                                                output={"error": "cancelled",
                                                        "route": "google_oauth_direct", "stream": True})
                                            _close_prop_ctx(_agy_gen_prop)

                                return StreamingResponse(
                                    agy_stream_generator(), media_type="text/event-stream"
                                )
                            else:
                                # Finalize parent trace for non-streaming agy
                                _end_parent_obs(parent_obs,
                                    output={"model": model_name, "tier": target_model,
                                            "route": "google_oauth_direct"},
                                    metadata={"latency_ms": latency_ms,
                                              "completion_tokens": completion_tokens})
                                _close_prop_ctx(_prop_ctx)
                                _non_streaming_finalized = True
                                return agy_response
                # agy_response was falsy (None) — finalize agy span before falling back
                _end_child_span(agy_span_obj, 
                    output={"error": "no_response"},
                    metadata={"status": "failed"},
                )
                logger.warning("agy proxy returned no response, falling back to LiteLLM")
            except ImportError:
                _end_child_span(agy_span_obj, 
                    output={"error": "module_not_available"},
                    metadata={"status": "skipped"},
                )
                logger.warning("agy_proxy module not available, falling back to LiteLLM")
            except Exception as e:
                _end_child_span(agy_span_obj, 
                    output={"error": type(e).__name__},
                    metadata={"status": "failed"},
                )
                logger.error(f"agy proxy failed: {type(e).__name__}, falling back to LiteLLM")

        if target_model == "llm-routing-agy":
            target_model = "agent-advanced-core"
        original_target_model = target_model

        # --- OLLAMA (via LiteLLM) ---
        # LiteLLM's ollama_chat provider handles the native Ollama API call.
        # We just proxy to LiteLLM with the appropriate model name.
        # LiteLLM's fallback chain handles failures.
        if should_try_ollama:
            if client_model in ("llm-routing-auto-ollama", "llm-routing-auto-agy-ollama"):
                if target_model in ("agent-advanced-core", "agent-reasoning-core"):
                    target_model = "ollama-deepseek-v4-pro"
                elif target_model == "agent-complex-core":
                    target_model = "ollama-deepseek-v4-flash"
            elif client_model == "llm-routing-ollama":
                if target_model in ("agent-advanced-core", "agent-reasoning-core"):
                    target_model = "ollama-deepseek-v4-pro"
                else:
                    target_model = "ollama-deepseek-v4-flash"
            else:
                # Fallback (e.g. if LiteLLM fallback loops back with model: llm-routing-ollama)
                if target_model in ("agent-advanced-core", "agent-reasoning-core"):
                    target_model = "ollama-deepseek-v4-pro"
                else:
                    target_model = "ollama-deepseek-v4-flash"
            logger.info(f"Ollama route: proxying to LiteLLM as model={target_model}")

        async def execute_proxy(model_name: str):
            """Executes a proxy request to a backend model."""
            nonlocal _non_streaming_finalized
            # Resolve backend connection parameters
            backend_conf = backends.get(model_name)
            if not backend_conf:
                logger.error(f"Backend '{model_name}' not found in configuration backends.")
                raise HTTPException(
                    status_code=500, detail=f"Backend {model_name} misconfigured"
                )

            backend_api_base = backend_conf["api_base"]
            backend_api_key = backend_conf["api_key"]
            if backend_api_key == "DYNAMIC_LITELLM_MASTER_KEY_PLACEHOLDER":
                backend_api_key = os.getenv("LITELLM_MASTER_KEY", backend_api_key)

            logger.info(f"Proxying to LiteLLM as model={model_name}")

            # --- Langfuse child span: LiteLLM proxy ---
            litellm_span_obj = None
            if langfuse_trace_id:
                lf_litellm = get_langfuse()
                if lf_litellm:
                    try:
                        litellm_span_obj = lf_litellm.start_observation(
                            trace_context={"trace_id": langfuse_trace_id},
                            name="litellm-proxy",
                            input=model_name,
                            metadata={"model": model_name},
                            level="DEFAULT",
                        )
                    except Exception:
                        pass

            client = get_http_client()
            try:
                headers = {"Authorization": f"Bearer {backend_api_key}"}
                if langfuse_trace_id:
                    headers["X-Langfuse-Trace-Id"] = langfuse_trace_id

                # Handle streaming vs non-streaming proxying (LiteLLM handles fallback internally)
                proxy_start = time.time()

                # --- Pre-screening: clamp max_tokens to fit within downstream model context limits ---
                try:
                    body_to_send = body.copy()
                    body_to_send["model"] = model_name
                    requested_max_tokens = body_to_send.get("max_tokens", 4096)

                    # Tier-aware minimum context length (from actual roster data):
                    # - agent-simple-core: 32K (includes tiny liquid/dolphin models)
                    # - agent-medium-core+: 256K (smallest non-tiny model is nemotron-nano-omni at 256K)
                    # - ollama-deepseek-v4-*: 1M (DeepSeek V4 native context)
                    _tier_min_ctx = {
                        "agent-simple-core": 32768,
                        "ollama-deepseek-v4-pro": 524288,
                        "ollama-deepseek-v4-flash": 524288,
                    }
                    _min_ctx = _tier_min_ctx.get(model_name, 262144)
                    _est_input = estimate_prompt_tokens(body_to_send)
                    _safe_max = _min_ctx - _est_input - 2048  # 2K safety margin
                    if _safe_max < 1024:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Context window exceeded. Estimated input tokens ({_est_input}) plus safety margin (2048) exceeds model context limit ({_min_ctx}).",
                        )
                    if requested_max_tokens > _safe_max:
                        logger.warning(
                            f"⛔ Clamping max_tokens: {requested_max_tokens} → {_safe_max} "
                            f"(est_input={_est_input}, min_ctx={_min_ctx}, tier={model_name})"
                        )
                        body_to_send["max_tokens"] = _safe_max
                except HTTPException:
                    _end_child_span(litellm_span_obj,
                        output={"error": "Context window exceeded"},
                        metadata={"status": "failed"},
                    )
                    raise
                except Exception as e:
                    logger.warning(f"Pre-screening failed (non-fatal): {e}")
                    body_to_send = body.copy()
                    body_to_send["model"] = model_name
                if "metadata" not in body_to_send or not isinstance(
                    body_to_send["metadata"], dict
                ):
                    body_to_send["metadata"] = {}
                else:
                    # Deep-copy to avoid mutating original body's metadata
                    # during fallback retries (shallow copy shares the dict)
                    body_to_send["metadata"] = dict(body_to_send["metadata"])
                body_to_send["metadata"]["trace_name"] = "agent-completion"
                if _trace_session_id:
                    body_to_send["metadata"]["session_id"] = _trace_session_id
                if _trace_user_id:
                    body_to_send["metadata"]["trace_user_id"] = _trace_user_id

                if body.get("stream", False):
                    logger.info(f"Proxying streaming to LiteLLM as model={model_name}")
                    req = client.build_request(
                        "POST",
                        f"{backend_api_base}/chat/completions",
                        json=body_to_send,
                        headers=headers,
                    )
                    r = await client.send(req, stream=True)
                    if r.status_code == 200:

                        async def stream_generator():
                            """Asynchronous generator that yields streaming chunks from LiteLLM completions response and logs usage stats on completion."""
                            import codecs

                            completion_chars = 0
                            request_tokens = estimate_prompt_tokens(body_to_send)
                            sse_buffer = ""
                            decoder = codecs.getincrementaldecoder("utf-8")()
                            finalized = False
                            _litellm_gen_prop = (
                                _make_prop_ctx(_trace_session_id, _trace_user_id)
                                or nullcontext()
                            )
                            _litellm_gen_prop.__enter__()
                            try:
                                async for chunk in r.aiter_bytes():
                                    yield chunk
                                    try:
                                        sse_buffer += decoder.decode(chunk)
                                        while "\n" in sse_buffer:
                                            line, sse_buffer = sse_buffer.split("\n", 1)
                                            line = line.strip()
                                            if line.startswith("data:"):
                                                data_str = line[5:].strip()
                                                if data_str == "[DONE]":
                                                    continue
                                                try:
                                                    data_json = json.loads(data_str)
                                                    choices = data_json.get("choices", [])
                                                    if choices and isinstance(
                                                        choices[0], dict
                                                    ):
                                                        delta = choices[0].get("delta")
                                                        if isinstance(delta, dict):
                                                            content = (
                                                                delta.get("content") or ""
                                                            )
                                                            completion_chars += len(content)
                                                except Exception:
                                                    pass
                                    except Exception:
                                        pass
                                proxy_latency = (time.time() - proxy_start) * 1000.0
                                stats["total_proxy_time_ms"] += proxy_latency
                                stats["avg_proxy_latency_ms"] = (
                                    stats["total_proxy_time_ms"] / stats["total_requests"]
                                )
                                record_tool_usage(ToolUsageRecord(
                                    active_tool,
                                    request_tokens,
                                    completion_chars // 4,
                                    model_name,
                                    proxy_latency,
                                    route="litellm_fallback",
                                ))
                                # Finalize LiteLLM span (streaming path)
                                _end_child_span(litellm_span_obj, 
                                    output={"model": model_name, "stream": True},
                                    metadata={
                                        "latency_ms": proxy_latency,
                                        "tokens": completion_chars // 4,
                                    },
                                )
                                # Finalize parent trace (streaming path)
                                _end_parent_obs(parent_obs,
                                    output={"model": model_name, "stream": True,
                                            "tier": target_model, "route": "litellm_fallback"},
                                    metadata={"latency_ms": proxy_latency,
                                              "completion_tokens": completion_chars // 4})
                                _close_prop_ctx(_litellm_gen_prop)
                                finalized = True
                            except Exception as ex:
                                logger.error(f"Stream error: {ex}")
                                # End child span before parent on stream error (CodeRabbit: missing finalization)
                                _end_child_span(litellm_span_obj,
                                    output={"error": type(ex).__name__},
                                    metadata={"status": "failed"},
                                )
                                # End parent trace on stream error (before any cooldown logic)
                                _end_parent_obs(parent_obs,
                                    output={"error": type(ex).__name__, "route": "litellm_fallback",
                                            "stream": True})
                                _close_prop_ctx(_litellm_gen_prop)
                                finalized = True
                                if model_name.startswith("ollama-"):
                                    global _ollama_cooldown_until
                                    _ollama_cooldown_until = (
                                        time.monotonic() + OLLAMA_COOLDOWN_SECONDS
                                    )
                                    try:
                                        await save_cooldowns_to_valkey()
                                        logger.error(
                                            f"🧊 Ollama failed midway through stream, activating {OLLAMA_COOLDOWN_SECONDS}s cooldown"
                                        )
                                    except Exception as save_err:
                                        logger.warning(
                                            f"Failed to save cooldowns to Valkey: {save_err}"
                                        )
                            finally:
                                if not finalized:
                                    _end_child_span(litellm_span_obj,
                                        output={"error": "cancelled"},
                                        metadata={"status": "cancelled"},
                                    )
                                    _end_parent_obs(parent_obs,
                                        output={"error": "cancelled", "route": "litellm_fallback",
                                                "stream": True})
                                    _close_prop_ctx(_litellm_gen_prop)
                                await r.aclose()

                        return StreamingResponse(
                            stream_generator(), media_type="text/event-stream"
                        )
                    else:
                        error_body = await r.aread() if r else b""
                        logger.warning(
                            f"LiteLLM stream failed ({r.status_code}): {error_body[:300]}"
                        )
                        await r.aclose()
                        # Finalize child span before raising on stream connection failure
                    # parent_obs finalized by outer handler (HTTPException → except block)
                        _end_child_span(litellm_span_obj,
                            output={"status": r.status_code, "error": "litellm_stream_failed"},
                            metadata={"status": "failed"},
                        )
                        raise HTTPException(
                            status_code=r.status_code,
                            detail="LiteLLM upstream request failed",
                        )
                else:
                    logger.info(f"Proxying to LiteLLM as model={model_name}")
                    response = await client.post(
                        f"{backend_api_base}/chat/completions",
                        json=body_to_send,
                        headers=headers,
                    )
                    if response.status_code == 200:
                        proxy_latency = (time.time() - proxy_start) * 1000.0
                        stats["total_proxy_time_ms"] += proxy_latency
                        stats["avg_proxy_latency_ms"] = (
                            stats["total_proxy_time_ms"] / stats["total_requests"]
                        )
                        resp_json = response.json()
                        usage = resp_json.get("usage") or {}
                        prompt_tokens = usage.get(
                            "prompt_tokens"
                        ) or estimate_prompt_tokens(body_to_send)
                        choices = resp_json.get("choices") or []
                        fallback_completion = 0
                        if choices and isinstance(choices[0], dict):
                            msg = choices[0].get("message")
                            if isinstance(msg, dict):
                                fallback_completion = len(msg.get("content") or "") // 4
                        completion_tokens = (
                            usage.get("completion_tokens") or fallback_completion
                        )
                        record_tool_usage(ToolUsageRecord(
                            active_tool,
                            prompt_tokens,
                            completion_tokens,
                            model_name,
                            proxy_latency,
                            route="litellm_fallback",
                        ))
                        # Finalize LiteLLM span (non-streaming path)
                        _end_child_span(litellm_span_obj, 
                            output={
                                "model": model_name,
                                "tokens": completion_tokens,
                            },
                            metadata={"latency_ms": proxy_latency},
                        )
                        # Finalize parent trace (non-streaming path)
                        _end_parent_obs(parent_obs,
                            output={"model": model_name, "tier": target_model,
                                    "route": "litellm_fallback"},
                            metadata={"latency_ms": proxy_latency,
                                      "prompt_tokens": prompt_tokens,
                                      "completion_tokens": completion_tokens})
                        _close_prop_ctx(_prop_ctx)
                        _non_streaming_finalized = True
                        return resp_json
                    else:
                        logger.warning(
                            f"LiteLLM failed ({response.status_code}): {response.text[:300]}"
                        )
                        # Finalize child span before raising on non-200 response
                        _end_child_span(litellm_span_obj,
                            output={"status": response.status_code, "error": "litellm_upstream_failed"},
                            metadata={"status": "failed"},
                        )
                        raise HTTPException(
                            status_code=response.status_code,
                            detail="LiteLLM upstream request failed",
                        )
            except HTTPException:
                raise
            except Exception as exc:
                logger.error(f"httpx call failed: {exc}")
                # Finalize child span before raising on proxy exception
                _end_child_span(litellm_span_obj,
                    output={"error": type(exc).__name__},
                    metadata={"status": "failed"},
                )
                raise HTTPException(
                    status_code=502, detail="Proxy call failed"
                ) from exc

        if should_try_ollama:
            # Sync state from Valkey first
            await sync_cooldowns_from_valkey()

            # --- Router-side Ollama cooldown check ---
            global _ollama_cooldown_until
            now_mono = time.monotonic()
            if now_mono < _ollama_cooldown_until:
                remaining = int(_ollama_cooldown_until - now_mono)
                logger.warning(
                    f"⏳ Ollama cooldown active ({remaining}s remaining), "
                    f"skipping {target_model}"
                )
                if client_model in (
                    "llm-routing-auto-ollama",
                    "llm-routing-auto-agy-ollama",
                ):
                    # Auto mode: silently fall through to the free tier
                    logger.info(
                        f"Auto-mode fallback: {target_model} → {original_target_model} (Ollama cooled down)"
                    )
                    try:
                        return await execute_proxy(original_target_model)
                    except HTTPException:
                        _end_parent_obs(parent_obs,
                            output={"error": "all_backends_failed", "route": "ollama_cooldown_fallback"})
                        _close_prop_ctx(_prop_ctx)
                        _non_streaming_finalized = True
                        raise
                else:
                    # Direct/fallback llm-routing-ollama: return 429 so LiteLLM
                    # skips this model group and moves to openrouter-auto
                    _end_parent_obs(parent_obs,
                        output={"error": "ollama_cooldown", "route": "ollama"})
                    _close_prop_ctx(_prop_ctx)
                    _non_streaming_finalized = True
                    raise HTTPException(
                        status_code=429,
                        detail=f"Ollama backend cooled down ({remaining}s remaining)",
                    )

            try:
                result = await execute_proxy(target_model)
                return result
            except HTTPException as e:
                is_transient = e.status_code in (429, 500, 502, 503, 504)
                if is_transient:
                    # Ollama failure — activate router-side cooldown
                    _ollama_cooldown_until = time.monotonic() + OLLAMA_COOLDOWN_SECONDS
                    await save_cooldowns_to_valkey()
                    logger.error(
                        f"🧊 Ollama failed ({e.status_code}), activating {OLLAMA_COOLDOWN_SECONDS}s cooldown"
                    )
                if client_model in (
                    "llm-routing-auto-ollama",
                    "llm-routing-auto-agy-ollama",
                ):
                    if is_transient:
                        logger.warning(
                            f"Ollama proxy failed ({e.detail}), falling back to free tier {original_target_model}"
                        )
                        try:
                            return await execute_proxy(original_target_model)
                        except HTTPException:
                            _end_parent_obs(parent_obs,
                                output={"error": "all_backends_failed", "route": "ollama_fallback"})
                            _close_prop_ctx(_prop_ctx)
                            _non_streaming_finalized = True
                            raise
                    else:
                        _end_parent_obs(parent_obs,
                            output={"error": f"ollama_non_transient_{e.status_code}", "route": "ollama"})
                        _close_prop_ctx(_prop_ctx)
                        _non_streaming_finalized = True
                        raise e
                else:
                    # Direct/fallback llm-routing-ollama request
                    if is_transient:
                        logger.error(
                            f"Ollama proxy failed ({e.detail}) for direct/fallback request, returning 429"
                        )
                        _end_parent_obs(parent_obs,
                            output={"error": "ollama_rate_limited", "route": "ollama"})
                        _close_prop_ctx(_prop_ctx)
                        _non_streaming_finalized = True
                        raise HTTPException(
                            status_code=429,
                            detail="Ollama backend rate limited/unavailable",
                        ) from e
                    else:
                        _end_parent_obs(parent_obs,
                            output={"error": f"ollama_non_transient_{e.status_code}", "route": "ollama"})
                        _close_prop_ctx(_prop_ctx)
                        _non_streaming_finalized = True
                        raise e
            except Exception as e:
                # Unexpected error (timeouts, connection issues) — also cooldown to prevent hammering
                _ollama_cooldown_until = time.monotonic() + OLLAMA_COOLDOWN_SECONDS
                await save_cooldowns_to_valkey()
                logger.error(
                    f"🧊 Ollama unexpected error ({e}), activating {OLLAMA_COOLDOWN_SECONDS}s cooldown"
                )
                if client_model in (
                    "llm-routing-auto-ollama",
                    "llm-routing-auto-agy-ollama",
                ):
                    logger.warning(
                        f"Ollama proxy error ({e}), falling back to free tier {original_target_model}"
                    )
                    try:
                        return await execute_proxy(original_target_model)
                    except HTTPException:
                        _end_parent_obs(parent_obs,
                            output={"error": "all_backends_failed", "route": "ollama_unexpected_fallback"})
                        _close_prop_ctx(_prop_ctx)
                        _non_streaming_finalized = True
                        raise
                else:
                    _end_parent_obs(parent_obs,
                        output={"error": type(e).__name__, "route": "ollama"})
                    _close_prop_ctx(_prop_ctx)
                    _non_streaming_finalized = True
                    raise HTTPException(
                        status_code=429, detail="Ollama backend rate limited/unavailable"
                    ) from e
        else:
            try:
                return await execute_proxy(target_model)
            except HTTPException:
                _end_parent_obs(parent_obs,
                    output={"error": "all_backends_failed", "route": "default_proxy"})
                _close_prop_ctx(_prop_ctx)
                _non_streaming_finalized = True
                raise
    finally:
        if not _is_streaming and not _non_streaming_finalized:
            _end_parent_obs(parent_obs,
                output={"error": "cancelled", "route": "non_streaming"})
            _prop_ctx = _close_prop_ctx(_prop_ctx)



@app.get("/metrics")
async def metrics():
    """Expose triage and circuit breaker metrics in Prometheus format."""
    await sync_cooldowns_from_valkey()
    breaker = get_breaker()
    breaker_status = breaker.status()

    lines = []
    # Triage request counters
    lines.append("# HELP triage_requests_total Total number of requests processed")
    lines.append("# TYPE triage_requests_total gauge")
    lines.append(f"triage_requests_total {stats['total_requests']}")

    lines.append("# HELP simple_requests_total Number of simple requests")
    lines.append("# TYPE simple_requests_total gauge")
    lines.append(f"simple_requests_total {stats['simple_requests']}")

    lines.append("# HELP medium_requests_total Number of medium requests")
    lines.append("# TYPE medium_requests_total gauge")
    lines.append(f"medium_requests_total {stats.get('medium_requests', 0)}")

    lines.append("# HELP complex_requests_total Number of complex requests")
    lines.append("# TYPE complex_requests_total gauge")
    lines.append(f"complex_requests_total {stats['complex_requests']}")

    lines.append("# HELP reasoning_requests_total Number of reasoning requests")
    lines.append("# TYPE reasoning_requests_total gauge")
    lines.append(f"reasoning_requests_total {stats.get('reasoning_requests', 0)}")

    lines.append("# HELP advanced_requests_total Number of advanced requests")
    lines.append("# TYPE advanced_requests_total gauge")
    lines.append(f"advanced_requests_total {stats.get('advanced_requests', 0)}")

    lines.append("# HELP cache_hits_total Number of triage cache hits")
    lines.append("# TYPE cache_hits_total gauge")
    lines.append(f"cache_hits_total {stats['cache_hits']}")

    # Latency metrics
    lines.append("# HELP avg_triage_latency_ms Average triage latency in milliseconds")
    lines.append("# TYPE avg_triage_latency_ms gauge")
    lines.append(f"avg_triage_latency_ms {stats['avg_triage_latency_ms']}")

    lines.append("# HELP avg_proxy_latency_ms Average proxy latency in milliseconds")
    lines.append("# TYPE avg_proxy_latency_ms gauge")
    lines.append(f"avg_proxy_latency_ms {stats['avg_proxy_latency_ms']}")

    # Token metrics
    lines.append("# HELP prompt_tokens_total Total prompt tokens processed")
    lines.append("# TYPE prompt_tokens_total counter")
    lines.append(f"prompt_tokens_total {stats['prompt_tokens']}")

    lines.append("# HELP completion_tokens_total Total completion tokens processed")
    lines.append("# TYPE completion_tokens_total counter")
    lines.append(f"completion_tokens_total {stats['completion_tokens']}")

    # Circuit breaker metrics — dual breaker (google + vendor)
    google = breaker_status["google"]
    vendor = breaker_status["vendor"]
    lines.append(
        "# HELP circuit_breaker_google_tier Google breaker cooldown tier (0=open, 3=max)"
    )
    lines.append("# TYPE circuit_breaker_google_tier gauge")
    lines.append(f"circuit_breaker_google_tier {google['tier']}")
    lines.append(
        "# HELP circuit_breaker_vendor_tier Vendor breaker cooldown tier (0=open, 3=max)"
    )
    lines.append("# TYPE circuit_breaker_vendor_tier gauge")
    lines.append(f"circuit_breaker_vendor_tier {vendor['tier']}")
    lines.append(
        "# HELP circuit_breaker_agy_allowed Whether EITHER breaker allows agy (backward-compat)"
    )
    lines.append("# TYPE circuit_breaker_agy_allowed gauge")
    lines.append(f"circuit_breaker_agy_allowed {int(breaker.is_allowed_peek())}")
    lines.append("# HELP circuit_breaker_total_trips Total trips across both breakers")
    lines.append("# TYPE circuit_breaker_total_trips counter")
    lines.append(
        f"circuit_breaker_total_trips {google['total_trips'] + vendor['total_trips']}"
    )

    # Ollama router-side cooldown metrics
    _now_mono = time.monotonic()
    _ollama_remaining = max(0.0, _ollama_cooldown_until - _now_mono)
    lines.append(
        "# HELP ollama_cooldown_active Whether Ollama is in router-side cooldown (1=active)"
    )
    lines.append("# TYPE ollama_cooldown_active gauge")
    lines.append(f"ollama_cooldown_active {int(_ollama_remaining > 0)}")
    lines.append(
        "# HELP ollama_cooldown_remaining_seconds Seconds remaining in Ollama cooldown"
    )
    lines.append("# TYPE ollama_cooldown_remaining_seconds gauge")
    lines.append(f"ollama_cooldown_remaining_seconds {_ollama_remaining:.0f}")

    return Response(content="\n".join(lines), media_type="text/plain; version=0.0.4")


# Source badge helper: generates a colored inline source tag
def src_badge(label, color):
    """Generate inline HTML span styled as a colored status/category badge."""
    return f"<span style='font-size: 9px; padding: 2px 7px; border-radius: 4px; background: {color}18; color: {color}; border: 1px solid {color}44; font-weight: 700; letter-spacing: 0.5px; vertical-align: middle; margin-right: 8px;'>{label}</span>"


async def get_dashboard_data():
    """Fetch all metrics and pre-compute HTML snippets for the dashboard."""
    # Run ALL independent I/O concurrently with protective timeouts
    (
        _,  # sync_cooldowns_from_valkey
        valkey_status,
        litellm_status,
        llama_server_status,
        langfuse_status,
        oauth_status,
        best_free_model,
        goose_sessions,
        llamacpp,
    ) = await asyncio.gather(
        asyncio.wait_for(sync_cooldowns_from_valkey(), timeout=2.0),
        check_tcp_port("127.0.0.1", _valkey_port()),
        check_http_endpoint(f"http://127.0.0.1:{os.getenv('LITELLM_PORT') or '4000'}/"),
        asyncio.wait_for(_check_llama_health(), timeout=3.0),
        check_http_endpoint(f"http://127.0.0.1:{os.getenv('LANGFUSE_WEB_PORT') or '3001'}"),
        get_gemini_oauth_status(),
        asyncio.wait_for(get_best_free_model(), timeout=5.0),
        asyncio.to_thread(get_goose_sessions),
        asyncio.wait_for(get_llamacpp_metrics(), timeout=5.0),
        return_exceptions=True
    )

    # Coerce exceptions to safe defaults if any task failed/timed out, and log failures
    if isinstance(valkey_status, Exception):
        logger.warning(f"Valkey health check failed: {valkey_status}")
        valkey_status = False

    if isinstance(litellm_status, Exception):
        logger.warning(f"LiteLLM health check failed: {litellm_status}")
        litellm_status = False

    if isinstance(llama_server_status, Exception):
        logger.warning(f"Llama-server health check failed: {llama_server_status}")
        llama_server_status = False

    if isinstance(langfuse_status, Exception):
        logger.warning(f"Langfuse health check failed: {langfuse_status}")
        langfuse_status = False

    if isinstance(oauth_status, Exception):
        logger.warning(f"Gemini OAuth status check failed: {oauth_status}")
        oauth_status = {"status": "error", "detail": "Check failed", "expiry_ms": 0}

    if isinstance(best_free_model, Exception):
        logger.warning(f"Best free model fetch failed: {best_free_model}")
        best_free_model = {"id": "error", "name": "Error fetching model", "score": 0.0}

    if isinstance(goose_sessions, Exception):
        logger.error(f"Failed to query goose sessions asynchronously: {goose_sessions}")
        goose_sessions = []

    if isinstance(llamacpp, Exception):
        logger.warning(f"Failed to fetch llama.cpp metrics: {llamacpp}")
        llamacpp = {"models": [], "slots": [], "build": "unknown"}

    # Pre-compute oauth_banner_html to avoid nested f-string and JavaScript bracket escaping issues
    oauth_banner_html = ""
    if oauth_status["status"] == "expired":
        oauth_banner_html = f"""
        <div class="oauth-banner">
            <div class="oauth-banner-inner oauth-banner-expired">
                <div style="display: flex; align-items: center; gap: 12px;">
                    <span style="font-size: 22px;">⚠️</span>
                    <div>
                        <div style="font-weight: 700; font-size: 15px; margin-bottom: 2px;">Gemini OAuth Token Expired</div>
                        <div style="opacity: 0.8; font-size: 13px;">{oauth_status["detail"]}. The agy proxy Tier 1 (Gemini) will timeout on every request, adding ~120s latency.</div>
                    </div>
                </div>
                <div class="oauth-banner-cmd" onclick="navigator.clipboard.writeText('agy auth login').then(() => {{ const t = this.querySelector('.copied-tooltip'); t.classList.add('show'); setTimeout(() => t.classList.remove('show'), 1500); }})">
                    <span class="copied-tooltip">Copied!</span>
                    $ agy auth login
                </div>
            </div>
        </div>
        """
    elif oauth_status["status"] in ("missing", "error"):
        oauth_banner_html = f"""
        <div class="oauth-banner">
            <div class="oauth-banner-inner oauth-banner-missing">
                <div style="display: flex; align-items: center; gap: 12px;">
                    <span style="font-size: 22px;">🔑</span>
                    <div>
                        <div style="font-weight: 700; font-size: 15px; margin-bottom: 2px;">Gemini OAuth Not Configured</div>
                        <div style="opacity: 0.8; font-size: 13px;">{oauth_status["detail"]}. Run the command to authenticate.</div>
                    </div>
                </div>
                <div class="oauth-banner-cmd" onclick="navigator.clipboard.writeText('agy auth login').then(() => {{ const t = this.querySelector('.copied-tooltip'); t.classList.add('show'); setTimeout(() => t.classList.remove('show'), 1500); }})">
                    <span class="copied-tooltip">Copied!</span>
                    $ agy auth login
                </div>
            </div>
        </div>
        """
    else:
        oauth_banner_html = f"""
        <div class="oauth-banner">
            <div class="oauth-banner-inner oauth-banner-valid">
                <div style="display: flex; align-items: center; gap: 10px;">
                    <span style="font-size: 18px;">✅</span>
                    <span style="font-weight: 600;">Gemini OAuth Active</span>
                    <span style="opacity: 0.7; font-size: 13px;">— {oauth_status["detail"]}</span>
                </div>
            </div>
        </div>
        """

    # 3. Calculative metrics — 5-tier triage table
    tier_data = [
        {
            "tier": "agent-simple-core",
            "count": stats.get("simple_requests", 0),
            "color": "#34d399",
        },
        {
            "tier": "agent-medium-core",
            "count": stats.get("medium_requests", 0),
            "color": "#fbbf24",
        },
        {
            "tier": "agent-complex-core",
            "count": stats.get("complex_requests", 0),
            "color": "#a78bfa",
        },
        {
            "tier": "agent-reasoning-core",
            "count": stats.get("reasoning_requests", 0),
            "color": "#60a5fa",
        },
        {
            "tier": "agent-advanced-core",
            "count": stats.get("advanced_requests", 0),
            "color": "#f472b6",
        },
    ]
    total_tier = sum(t["count"] for t in tier_data)
    for t in tier_data:
        t["ratio"] = (t["count"] / total_tier * 100.0) if total_tier > 0 else 0.0

    # Build tier table rows
    tier_table_rows = ""
    for t in tier_data:
        tier_table_rows += f"""
        <tr>
            <td style="padding:8px 12px;font-size:13px;font-weight:600;font-family:monospace;color:{t["color"]};">
                <span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:{t["color"]};margin-right:8px;box-shadow:0 0 4px {t["color"]}aa;"></span>
                {t["tier"]}
            </td>
            <td style="padding:8px 12px;text-align:right;font-size:13px;font-weight:700;">{t["count"]}</td>
            <td style="padding:8px 12px;text-align:right;font-size:12px;opacity:0.6;">{t["ratio"]:.1f}%</td>
        </tr>"""
    tier_table_html = f"""
    <table style="width:100%;border-collapse:collapse;">
        <thead>
            <tr style="border-bottom:1px solid rgba(255,255,255,0.06);">
                <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;opacity:0.5;font-weight:600;letter-spacing:0.5px;">Tier</th>
                <th style="padding:8px 12px;text-align:right;font-size:11px;text-transform:uppercase;opacity:0.5;font-weight:600;letter-spacing:0.5px;">Requests</th>
                <th style="padding:8px 12px;text-align:right;font-size:11px;text-transform:uppercase;opacity:0.5;font-weight:600;letter-spacing:0.5px;">Share</th>
            </tr>
        </thead>
        <tbody>
            {tier_table_rows}
        </tbody>
    </table>"""

    # 4. Generate dynamic conic-gradient CSS background for the Pie Chart
    pie_gradient = get_pie_chart_gradient()
    total_tool_tokens = sum(stats["tool_tokens"].values())

    # 5. Generate tool tokens HTML & Pie Chart Legend
    tool_tokens_html = ""
    pie_legend_html = ""
    max_tool_val = max(stats["tool_tokens"].values()) if max(stats["tool_tokens"].values()) > 0 else 1
    
    for tool_name, token_count in stats["tool_tokens"].items():
        pct = (token_count / max_tool_val) * 100.0
        overall_pct = (token_count / total_tool_tokens * 100.0) if total_tool_tokens > 0 else 0.0
        color = TOOL_COLORS.get(tool_name, "#94a3b8")
        # Horizontal meters
        tool_tokens_html += f"""
        <div style="margin-bottom: 20px;">
            <div style="display: flex; justify-content: space-between; margin-bottom: 6px; font-size: 14px;">
                <span style="font-weight: 600; text-transform: capitalize;">🛠️ {tool_name}</span>
                <span style="opacity: 0.8; font-weight: bold;">{token_count:,} tokens ({overall_pct:.1f}%)</span>
            </div>
            <div style="height: 10px; background: rgba(255,255,255,0.05); border-radius: 10px; overflow: hidden;">
                <div style="width: {pct}%; height: 100%; background: linear-gradient(90deg, {color}, {color}aa); border-radius: 10px; transition: width 0.5s ease;"></div>
            </div>
        </div>
        """

        # Circular Legend
        pie_legend_html += f"""
        <div style="display: flex; align-items: center; gap: 8px; font-size: 13px;">
            <span style="width: 12px; height: 12px; border-radius: 50%; background: {color}; display: inline-block; box-shadow: 0 0 6px {color}aa;"></span>
            <span style="text-transform: capitalize; font-weight: 600;">{tool_name}:</span>
            <span style="opacity: 0.7;">{overall_pct:.1f}%</span>
        </div>
        """

    # 6. Generate timeline HTML with route badges
    timeline_html = ""
    if not stats["timeline"]:
        timeline_html = "<div style='opacity: 0.5; font-size: 14px; text-align: center; padding: 20px;'>Waiting for active tool executions...</div>"
    else:
        for ev in reversed(stats["timeline"]):
            route_label = ev.get("route", "litellm_fallback")
            route_color = (
                "#fbbf24" if route_label == "google_oauth_direct" else "#818cf8"
            )
            route_short = (
                "GOOGLE" if route_label == "google_oauth_direct" else "LITELLM"
            )
            timeline_html += f"""
            <div style="display: flex; gap: 15px; margin-bottom: 15px; border-left: 2px solid rgba(255,255,255,0.1); padding-left: 20px; position: relative;">
                <div style="width: 10px; height: 10px; background: {route_color}; border-radius: 50%; position: absolute; left: -6px; top: 6px; box-shadow: 0 0 8px {route_color};"></div>
                <div style="flex-grow: 1;">
                    <div style="display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 2px;">
                        <span style="font-weight: 600; text-transform: uppercase; color: #a5b4fc;">🔧 {ev["tool"]} <span style="font-size: 9px; padding: 1px 5px; border-radius: 4px; background: {route_color}22; color: {route_color}; border: 1px solid {route_color}44; margin-left: 6px; vertical-align: middle;">{route_short}</span></span>
                        <span style="opacity: 0.5; font-family: monospace;">{ev["timestamp"]}</span>
                    </div>
                    <div style="font-size: 14px; opacity: 0.9;">
                        Processed <strong>{ev["tokens"]:,} tokens</strong> on <span style="color: #c084fc;">{ev["model"]}</span>
                    </div>
                    <div style="font-size: 12px; opacity: 0.5; margin-top: 2px;">
                        Latency: {ev["latency_ms"]} ms
                    </div>
                </div>
            </div>
            """

    # 7. Generate Goose Sessions HTML
    goose_html = ""
    if not goose_sessions:
        goose_html = """
        <div style="background: rgba(255,255,255,0.02); border-radius: 12px; padding: 20px; text-align: center; border: 1px solid rgba(255,255,255,0.05); font-size: 14px; opacity: 0.6;">
            ⚠️ No active Goose session database detected at mountpoint.
        </div>
        """
    else:
        for idx, sess in enumerate(goose_sessions):
            is_active = idx == 0
            badge_style = (
                "background: rgba(129, 140, 248, 0.15); color: #c084fc; border: 1px solid rgba(129, 140, 248, 0.3);"
                if is_active
                else "background: rgba(255,255,255,0.03); color: #fff; border: 1px solid rgba(255,255,255,0.05);"
            )
            active_label = (
                "<span style='font-size: 10px; background: #10b981; color: #fff; padding: 2px 6px; border-radius: 4px; margin-right: 8px; font-weight: bold;'>ACTIVE</span>"
                if is_active
                else ""
            )

            desc = sess.get("description") or sess.get("name") or "Interactive session"
            tokens = sess.get("accumulated_total_tokens", 0) or 0

            goose_html += f"""
            <div style="background: rgba(255, 255, 255, 0.02); border: 1px solid rgba(255, 255, 255, 0.05); border-radius: 12px; padding: 15px; margin-bottom: 12px; display: flex; flex-direction: column; gap: 8px;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div style="display: flex; align-items: center;">
                        {active_label}
                        <span style="font-weight: 600; font-size: 15px;">Session {sess["id"]}</span>
                    </div>
                    <span style="font-size: 12px; padding: 3px 8px; border-radius: 20px; {badge_style}">{sess.get("goose_mode", "auto").upper()}</span>
                </div>
                <div style="font-size: 13px; opacity: 0.7; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                    {desc}
                </div>
                <div style="display: flex; justify-content: space-between; font-size: 11px; opacity: 0.5; margin-top: 4px;">
                    <span>📅 {sess["updated_at"]}</span>
                    <span style="font-weight: bold; color: #a5b4fc;">{tokens:,} total tokens</span>
                </div>
            </div>
            """

    # 8. Routing Paths pie chart & legend
    routing_paths = stats.get(
        "routing_paths", {"google_oauth_direct": 0, "litellm_fallback": 0}
    )
    total_routed = sum(routing_paths.values())
    routing_pie_gradient = "background: rgba(255, 255, 255, 0.05);"
    routing_legend_html = ""
    routing_colors = {"google_oauth_direct": "#fbbf24", "litellm_fallback": "#818cf8"}
    routing_labels = {
        "google_oauth_direct": "Google OAuth Direct",
        "litellm_fallback": "LiteLLM Fallback",
    }
    if total_routed > 0:
        current_angle = 0.0
        route_grad_parts = []
        for rname, rcount in routing_paths.items():
            rpct = (rcount / total_routed) * 100.0
            next_angle = current_angle + rpct
            rcolor = routing_colors.get(rname, "#94a3b8")
            route_grad_parts.append(f"{rcolor} {current_angle:.1f}% {next_angle:.1f}%")
            routing_legend_html += f"""
            <div style="display: flex; align-items: center; gap: 8px; font-size: 13px;">
                <span style="width: 12px; height: 12px; border-radius: 50%; background: {rcolor}; display: inline-block; box-shadow: 0 0 6px {rcolor}aa;"></span>
                <span style="font-weight: 600;">{routing_labels.get(rname, rname)}:</span>
                <span style="opacity: 0.7;">{rcount} ({rpct:.1f}%)</span>
            </div>
            """
            current_angle = next_angle
        routing_pie_gradient = (
            f"background: conic-gradient({', '.join(route_grad_parts)});"
        )

    # 9. Model Usage — canonical source is Langfuse traces (replaces duplicated in-memory counter)
    # See router trace → LiteLLM trace linkage via X-Langfuse-Trace-Id header.

    # Persistent aggregated tokens
    p_tokens = stats.get("prompt_tokens", 0)
    c_tokens = stats.get("completion_tokens", 0)
    t_tokens = p_tokens + c_tokens

    # 10. Pre-compute llama.cpp HTML cards
    llamacpp_models_html = ""
    if llamacpp["models"]:
        for m in llamacpp["models"]:
            status_style = (
                "background: rgba(16,185,129,0.12); color: #34d399; border: 1px solid rgba(16,185,129,0.25);"
                if m["status"] == "loaded"
                else "background: rgba(255,255,255,0.04); color: rgba(255,255,255,0.4); border: 1px solid rgba(255,255,255,0.08);"
            )
            params_str = (
                f"<span>\U0001f9e0 {m['n_params'] / 1e9:.1f}B params</span>"
                if m["n_params"]
                else ""
            )
            ctx_str = (
                f"<span>\U0001f4d0 ctx {m['n_ctx']:,}</span>" if m["n_ctx"] else ""
            )
            size_str = (
                f"<span>\U0001f4be {m['size_bytes'] / 1e6:.0f} MB</span>"
                if m["size_bytes"]
                else ""
            )
            llamacpp_models_html += f"""
            <div style="background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.05); border-radius: 12px; padding: 14px 18px; margin-bottom: 10px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                    <span style="font-weight: 700; font-size: 14px; font-family: monospace;">{m["id"]}</span>
                    <span style="font-size: 10px; padding: 2px 8px; border-radius: 20px; font-weight: 700; letter-spacing: 0.5px; {status_style}">{m["status"].upper()}</span>
                </div>
                <div style="display: flex; gap: 16px; font-size: 11px; opacity: 0.6;">
                    {params_str}{ctx_str}{size_str}
                </div>
            </div>
            """
    else:
        llamacpp_models_html = '<div style="opacity: 0.5; font-size: 13px; text-align: center; padding: 15px;">No models detected</div>'

    llamacpp_slots_html = ""
    if llamacpp["slots"]:
        slot_items = ""
        for sl in llamacpp["slots"]:
            dot_style = (
                "background: #34d399; box-shadow: 0 0 8px #34d399;"
                if sl["is_processing"]
                else "background: rgba(255,255,255,0.15);"
            )
            slot_items += f"""
            <div style="background: rgba(255,255,255,0.015); border: 1px solid rgba(255,255,255,0.04); border-radius: 10px; padding: 10px 14px; position: relative; overflow: hidden;">
                <div style="position: absolute; top: 0; right: 0; width: 8px; height: 8px; margin: 8px; border-radius: 50%; {dot_style}"></div>
                <div style="font-size: 13px; font-weight: 700; margin-bottom: 4px;">Slot {sl["id"]}</div>
                <div style="font-size: 11px; opacity: 0.6; display: flex; flex-direction: column; gap: 2px;">
                    <span>Prompt: {sl["n_prompt_processed"]} tok</span>
                    <span>Decoded: {sl["n_decoded"]} tok</span>
                </div>
            </div>
            """
        llamacpp_slots_html = f"""
        <div style="margin-top: 14px; border-top: 1px solid rgba(255,255,255,0.05); padding-top: 14px;">
            <div style="font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 1px; opacity: 0.5; margin-bottom: 10px;">Inference Slots</div>
            <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px;">
                {slot_items}
            </div>
        </div>
        """

    return {
        "valkey_status": valkey_status,
        "litellm_status": litellm_status,
        "llama_server_status": llama_server_status,
        "langfuse_status": langfuse_status,
        "oauth_banner_html": oauth_banner_html,
        "best_free_model": best_free_model,
        "tier_table_html": tier_table_html,
        "pie_gradient": pie_gradient,
        "total_tool_tokens": total_tool_tokens,
        "tool_tokens_html": tool_tokens_html,
        "pie_legend_html": pie_legend_html,
        "timeline_html": timeline_html,
        "goose_html": goose_html,
        "routing_pie_gradient": routing_pie_gradient,
        "routing_legend_html": routing_legend_html,
        "p_tokens": p_tokens,
        "c_tokens": c_tokens,
        "t_tokens": t_tokens,
        "llamacpp_models_html": llamacpp_models_html,
        "llamacpp_slots_html": llamacpp_slots_html,
        "llamacpp_build": llamacpp["build"],
        "avg_triage_latency_ms": stats["avg_triage_latency_ms"],
        "avg_proxy_latency_ms": stats["avg_proxy_latency_ms"],
        "cache_hits": stats["cache_hits"],
        "total_requests": stats["total_requests"],
        "last_triage_decision": stats["last_triage_decision"],
    }


@app.get("/api/dashboard-stats")
async def get_dashboard_stats():
    """Return dashboard metrics and pre-computed HTML as JSON for asynchronous UI updates."""
    return await get_dashboard_data()


def resolve_external_urls(request: Request) -> tuple[str, str, str]:
    """Resolve and validate the base URLs for Langfuse, LiteLLM, and Llama.cpp."""
    # 1. Try to load centralized base URL from config/env
    base_url_env = os.getenv("PUBLIC_BASE_URL") or os.getenv("BASEURL") or os.getenv("BASE_URL")
    if base_url_env:
        if "://" not in base_url_env:
            parsed = urlparse(f"https://{base_url_env}")
        else:
            parsed = urlparse(base_url_env)
        external_host = parsed.hostname or "localhost"
        external_netloc = parsed.netloc or "localhost"
        external_scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"
    else:
        external_host = request.base_url.hostname or "localhost"
        external_netloc = request.base_url.netloc or "localhost"
        external_scheme = request.url.scheme if request.url.scheme in ("http", "https") else "https"

    domain = os.getenv("ROUTING_DOMAIN") or "vendeuvre.lan"

    # Basic sanity-check on external_host, but don't over-restrict valid hostnames;
    # fall back to the request base URL rather than silently forcing localhost.
    if not isinstance(external_host, str) or not re.match(r"^[a-zA-Z0-9.-:]+$", external_host):
        logger.warning(
            "Unexpected external_host %r, falling back to request.base_url.hostname (%r)",
            external_host,
            request.base_url.hostname,
        )
        external_host = request.base_url.hostname or "localhost"

    # Relax external_netloc validation: use urlparse so IPv6 literals, IDN/punycode,
    # and reverse-proxy-modified netlocs are supported. Log and fall back instead of
    # silently forcing localhost when invalid.
    if isinstance(external_netloc, str):
        parsed_netloc = urlparse(f"{external_scheme}://{external_netloc}")
        if not parsed_netloc.hostname:
            logger.warning(
                "Invalid external_netloc %r, falling back to request.base_url.netloc (%r)",
                external_netloc,
                request.base_url.netloc,
            )
            external_netloc = request.base_url.netloc or "localhost"
    else:
        logger.warning(
            "Non-string external_netloc %r, falling back to request.base_url.netloc (%r)",
            external_netloc,
            request.base_url.netloc,
        )
        external_netloc = request.base_url.netloc or "localhost"

    # Enforce strict domain validation to prevent loose substring match bypasses (e.g., attacker-vendeuvre.lan)
    is_valid_external = external_host == domain or external_host.endswith("." + domain)
    is_valid_base = request.base_url.hostname == domain or (request.base_url.hostname or "").endswith("." + domain)

    if is_valid_external:
        # Centralized base URL path under subdomain/reverse proxy
        return (
            f"{external_scheme}://{external_netloc}/llm-routing/langfuse",
            f"{external_scheme}://{external_netloc}/llm-routing/litellm/ui",
            f"{external_scheme}://{external_netloc}/llm-routing/llama/"
        )
    elif is_valid_base:
        parsed_netloc = urlparse(f"{external_scheme}://{request.url.netloc}")
        netloc = request.url.netloc if parsed_netloc.hostname else "localhost"
        base = f"{external_scheme}://{netloc}"
        return (
            f"{base}/llm-routing/langfuse",
            f"{base}/llm-routing/litellm/ui",
            f"{base}/llm-routing/llama/"
        )
    else:
        # Local development fallback: derive schemes, ports, and paths dynamically from configuration constants
        parsed_lf = urlparse(LANGFUSE_HOST)
        parsed_ll = urlparse(LITELLM_URL)
        parsed_lm = urlparse(LLAMA_SERVER_URL)

        lf_scheme = parsed_lf.scheme or "http"
        ll_scheme = parsed_ll.scheme or "http"
        lm_scheme = parsed_lm.scheme or "http"

        lf_port = f":{parsed_lf.port}" if parsed_lf.port else ""
        ll_port = f":{parsed_ll.port}" if parsed_ll.port else ""
        lm_port = f":{parsed_lm.port}" if parsed_lm.port else ""

        lf_path = parsed_lf.path or ""
        ll_path = parsed_ll.path or "/ui"
        if not ll_path.endswith("/ui") and not ll_path.endswith("/ui/"):
            ll_path = ll_path.rstrip("/") + "/ui"
        lm_path = parsed_lm.path or ""

        host_formatted = f"[{external_host}]" if ":" in external_host else external_host

        return (
            f"{lf_scheme}://{host_formatted}{lf_port}{lf_path}",
            f"{ll_scheme}://{host_formatted}{ll_port}{ll_path}",
            f"{lm_scheme}://{host_formatted}{lm_port}{lm_path}"
        )


@app.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    """Render the router main dashboard HTML showing system metrics, health checks, and recent token usage."""
    langfuse_url, litellm_url, llama_url = resolve_external_urls(request)

    data = await get_dashboard_data()

    # Unpack data for the f-string template
    valkey_status = data["valkey_status"]
    litellm_status = data["litellm_status"]
    llama_server_status = data["llama_server_status"]
    langfuse_status = data["langfuse_status"]
    oauth_banner_html = data["oauth_banner_html"]
    best_free_model = data["best_free_model"]
    tier_table_html = data["tier_table_html"]
    pie_gradient = data["pie_gradient"]
    tool_tokens_html = data["tool_tokens_html"]
    pie_legend_html = data["pie_legend_html"]
    timeline_html = data["timeline_html"]
    goose_html = data["goose_html"]
    routing_pie_gradient = data["routing_pie_gradient"]
    routing_legend_html = data["routing_legend_html"]
    p_tokens = data["p_tokens"]
    c_tokens = data["c_tokens"]
    t_tokens = data["t_tokens"]
    llamacpp_models_html = data["llamacpp_models_html"]
    llamacpp_slots_html = data["llamacpp_slots_html"]
    avg_triage_latency_ms = data["avg_triage_latency_ms"]
    avg_proxy_latency_ms = data["avg_proxy_latency_ms"]
    cache_hits = data["cache_hits"]
    total_requests = data["total_requests"]
    last_triage_decision = data["last_triage_decision"]

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>LLM Triage Gateway - Control Center</title>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
        <style>
            :root {{
                --slate-900: #0f172a;
                --indigo-950: #1e1b4b;
                --emerald-500: #10b981;
                --rose-500: #f43f5e;
                --text-main: #f8fafc;
                --glass-bg: rgba(255, 255, 255, 0.03);
                --glass-border: rgba(255, 255, 255, 0.08);
            }}

            .oauth-banner {{
                width: 100%;
                max-width: 1400px;
                margin: 0 auto -10px auto;
                padding: 0 20px;
            }}

            .oauth-banner-inner {{
                border-radius: 12px;
                padding: 16px 24px;
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 16px;
                font-size: 14px;
                backdrop-filter: blur(12px);
                -webkit-backdrop-filter: blur(12px);
                animation: bannerPulse 3s ease-in-out infinite;
            }}

            .oauth-banner-expired {{
                background: rgba(244, 63, 94, 0.12);
                border: 1px solid rgba(244, 63, 94, 0.35);
                color: #fda4af;
            }}

            .oauth-banner-valid {{
                background: rgba(16, 185, 129, 0.08);
                border: 1px solid rgba(16, 185, 129, 0.2);
                color: #6ee7b7;
                animation: none;
            }}

            .oauth-banner-missing {{
                background: rgba(251, 191, 36, 0.1);
                border: 1px solid rgba(251, 191, 36, 0.3);
                color: #fde68a;
            }}

            .oauth-banner-cmd {{
                font-family: monospace;
                background: rgba(0, 0, 0, 0.3);
                padding: 6px 14px;
                border-radius: 8px;
                font-weight: 700;
                letter-spacing: 0.5px;
                white-space: nowrap;
                cursor: pointer;
                transition: background 0.2s;
                position: relative;
            }}

            .oauth-banner-cmd:hover {{
                background: rgba(0, 0, 0, 0.5);
            }}

            .oauth-banner-cmd .copied-tooltip {{
                position: absolute;
                top: -28px;
                left: 50%;
                transform: translateX(-50%);
                background: #10b981;
                color: #fff;
                padding: 3px 10px;
                border-radius: 6px;
                font-size: 11px;
                opacity: 0;
                pointer-events: none;
                transition: opacity 0.3s;
            }}

            .oauth-banner-cmd .copied-tooltip.show {{
                opacity: 1;
            }}

            @keyframes bannerPulse {{
                0%, 100% {{ opacity: 1; }}
                50% {{ opacity: 0.85; }}
            }}

            * {{
                box-sizing: border-box;
                margin: 0;
                padding: 0;
            }}

            body {{
                font-family: 'Outfit', sans-serif;
                background: linear-gradient(135deg, var(--slate-900), var(--indigo-950));
                color: var(--text-main);
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                justify-content: space-between;
                overflow-x: hidden;
            }}

            header {{
                width: 100%;
                max-width: 1400px;
                margin: 0 auto;
                padding: 30px 20px;
                display: flex;
                justify-content: space-between;
                align-items: center;
            }}

            .logo-area {{
                display: flex;
                align-items: center;
                gap: 15px;
            }}

            .logo-dot {{
                width: 15px;
                height: 15px;
                border-radius: 50%;
                background: linear-gradient(45deg, #818cf8, #a78bfa);
                box-shadow: 0 0 15px #818cf8;
            }}

            .logo-text {{
                font-size: 24px;
                font-weight: 800;
                background: linear-gradient(45deg, #a5b4fc, #c084fc);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }}

            .dashboard-title {{
                font-size: 14px;
                letter-spacing: 2px;
                text-transform: uppercase;
                opacity: 0.6;
            }}

            main {{
                width: 100%;
                max-width: 1400px;
                margin: 0 auto;
                padding: 0 20px 50px 20px;
                flex-grow: 1;
                display: grid;
                grid-template-columns: 2fr 1fr;
                gap: 30px;
            }}

            @media (max-width: 1000px) {{
                main {{
                    grid-template-columns: 1fr;
                }}
            }}

            .glass-card {{
                background: var(--glass-bg);
                backdrop-filter: blur(20px);
                border: 1px solid var(--glass-border);
                border-radius: 24px;
                padding: 30px;
                box-shadow: 0 20px 50px rgba(0, 0, 0, 0.4);
                transition: transform 0.3s ease, border-color 0.3s ease;
                margin-bottom: 30px;
            }}

            .glass-card:hover {{
                border-color: rgba(255, 255, 255, 0.15);
            }}

            .status-container {{
                display: flex;
                flex-direction: column;
                gap: 16px;
            }}

            .section-title {{
                font-size: 20px;
                font-weight: 600;
                margin-bottom: 20px;
                color: #e2e8f0;
                display: flex;
                justify-content: space-between;
                align-items: center;
                border-bottom: 1px solid rgba(255,255,255,0.05);
                padding-bottom: 12px;
            }}

            .service-row {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 12px 20px;
                background: rgba(255, 255, 255, 0.01);
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.04);
            }}

            .service-info {{
                display: flex;
                align-items: center;
                gap: 15px;
            }}

            .service-name {{
                font-weight: 600;
                font-size: 15px;
            }}

            .service-port {{
                font-size: 12px;
                opacity: 0.5;
                font-family: monospace;
            }}

            .badge {{
                display: flex;
                align-items: center;
                gap: 8px;
                font-size: 11px;
                font-weight: 600;
                padding: 5px 12px;
                border-radius: 50px;
                text-transform: uppercase;
                letter-spacing: 1px;
            }}

            .badge-online {{
                background: rgba(16, 185, 129, 0.1);
                color: var(--emerald-500);
                border: 1px solid rgba(16, 185, 129, 0.2);
            }}

            .badge-offline {{
                background: rgba(244, 63, 94, 0.1);
                color: var(--rose-500);
                border: 1px solid rgba(244, 63, 94, 0.2);
            }}

            .pulse-dot {{
                width: 8px;
                height: 8px;
                border-radius: 50%;
                display: inline-block;
            }}

            .badge-online .pulse-dot {{
                background: var(--emerald-500);
                box-shadow: 0 0 10px var(--emerald-500);
                animation: pulse 2s infinite;
            }}

            .badge-offline .pulse-dot {{
                background: var(--rose-500);
                box-shadow: 0 0 10px var(--rose-500);
            }}

            @keyframes pulse {{
                0% {{ transform: scale(0.95); opacity: 0.8; }}
                50% {{ transform: scale(1.1); opacity: 1; }}
                100% {{ transform: scale(0.95); opacity: 0.8; }}
            }}

            .metrics-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                gap: 20px;
                margin-bottom: 30px;
            }}

            .metric-box {{
                background: rgba(255, 255, 255, 0.01);
                border: 1px solid rgba(255, 255, 255, 0.04);
                border-radius: 16px;
                padding: 20px;
                display: flex;
                flex-direction: column;
                gap: 8px;
            }}

            .metric-value {{
                font-size: 28px;
                font-weight: 800;
                color: #fff;
            }}

            .metric-label {{
                font-size: 12px;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 1px;
                opacity: 0.5;
            }}

            .ratio-container {{
                display: flex;
                height: 10px;
                border-radius: 50px;
                overflow: hidden;
                margin-top: 15px;
                background: rgba(255, 255, 255, 0.05);
            }}

            .ratio-legend {{
                display: flex;
                justify-content: space-between;
                font-size: 12px;
                margin-top: 8px;
                opacity: 0.7;
            }}

            .btn-group {{
                display: flex;
                flex-direction: column;
                gap: 12px;
                margin-top: 15px;
            }}

            .btn {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 14px 20px;
                background: rgba(255, 255, 255, 0.02);
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 12px;
                color: #fff;
                text-decoration: none;
                font-weight: 600;
                transition: all 0.3s ease;
                font-size: 14px;
            }}

            .btn:hover {{
                background: rgba(255, 255, 255, 0.06);
                border-color: rgba(129, 140, 248, 0.3);
                transform: translateX(4px);
            }}

            .btn-arrow {{
                opacity: 0.5;
                font-size: 16px;
                transition: transform 0.3s ease;
            }}

            .btn:hover .btn-arrow {{
                transform: translateX(3px);
                opacity: 1;
            }}

            /* CSS Pie Chart styles */
            .pie-chart {{
                width: 150px;
                height: 150px;
                border-radius: 50%;
                {pie_gradient}
                box-shadow: 0 0 30px rgba(0, 0, 0, 0.4);
                position: relative;
                flex-shrink: 0;
            }}
            .pie-chart::after {{
                content: "";
                position: absolute;
                width: 70px;
                height: 70px;
                background: #111827; /* Matches dashboard glass background inner */
                border-radius: 50%;
                top: 40px;
                left: 40px;
                box-shadow: inset 0 0 10px rgba(0, 0, 0, 0.8);
            }}

            footer {{
                width: 100%;
                text-align: center;
                padding: 30px;
                font-size: 12px;
                opacity: 0.4;
                letter-spacing: 1px;
            }}
        </style>
        <script>
            async function refreshDashboard() {{
                try {{
                    const basePath = window.location.pathname.endsWith('/') ? '../' : './';
                    const res = await fetch(basePath + "api/dashboard-stats");
                    if (!res.ok) throw new Error(`HTTP error! status: ${{res.status}}`);
                    const data = await res.json();

                    // 1. Update infrastructure status indicators
                    const updateStatus = (id, isOnline) => {{
                        const el = document.getElementById(id);
                        if (el) {{
                            el.className = isOnline ? "badge badge-online" : "badge badge-offline";
                            el.innerHTML = `<span class="pulse-dot"></span>${{isOnline ? "Online" : "Offline"}}`;
                        }}
                    }};
                    updateStatus("litellm-status", data.litellm_status);
                    updateStatus("valkey-status", data.valkey_status);
                    updateStatus("llama-server-status", data.llama_server_status);
                    updateStatus("langfuse-status", data.langfuse_status);

                    // 2. Update metrics grid
                    document.getElementById("total-requests").textContent = data.total_requests;
                    document.getElementById("last-triage-decision").textContent = data.last_triage_decision;
                    document.getElementById("avg-triage-latency").textContent = data.avg_triage_latency_ms.toFixed(1) + " ms";
                    document.getElementById("avg-proxy-latency").textContent = data.avg_proxy_latency_ms.toFixed(1) + " ms";
                    document.getElementById("cache-hits").textContent = data.cache_hits;

                    // 3. Update token counts
                    document.getElementById("p-tokens").textContent = data.p_tokens.toLocaleString();
                    document.getElementById("c-tokens").textContent = data.c_tokens.toLocaleString();
                    document.getElementById("t-tokens").textContent = data.t_tokens.toLocaleString();

                    // 4. Update dynamic HTML blocks
                    document.getElementById("oauth-banner-container").innerHTML = data.oauth_banner_html;
                    document.getElementById("tier-table-container").innerHTML = data.tier_table_html;
                    document.getElementById("pie-legend-container").innerHTML = data.pie_legend_html;
                    document.getElementById("routing-legend-container").innerHTML = data.routing_legend_html || "<div style='opacity: 0.5; font-size: 13px;'>No routing data yet</div>";
                    document.getElementById("tool-tokens-container").innerHTML = data.tool_tokens_html;
                    document.getElementById("timeline-container").innerHTML = data.timeline_html;
                    document.getElementById("goose-sessions-container").innerHTML = data.goose_html;
                    document.getElementById("llamacpp-models-container").innerHTML = data.llamacpp_models_html;
                    document.getElementById("llamacpp-slots-container").innerHTML = data.llamacpp_slots_html;

                    // 5. Update Frontier Free Model widget
                    const bestFreeModelContainer = document.getElementById("best-free-model-container");
                    if (bestFreeModelContainer) {{
                        const m = data.best_free_model;
                        const statusLabel = (!m.is_fallback) ? "LIVE" : "FALLBACK";
                        bestFreeModelContainer.innerHTML = `
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                                <span style="font-weight: 800; font-size: 16px; color: #fff;">${{m.name}}</span>
                                <span style="font-size: 13px; font-weight: 800; padding: 4px 10px; border-radius: 20px; background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.25);">⚡ ${{m.score.toFixed(1)}}</span>
                            </div>
                            <div style="font-size: 12px; font-family: monospace; opacity: 0.6; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-bottom: 8px;">
                                ID: ${{m.id}}
                            </div>
                            <div style="display: flex; justify-content: space-between; font-size: 11px; opacity: 0.5;">
                                <span>📐 context ${{m.context_length.toLocaleString()}} tok</span>
                                <span style="color: #34d399; font-weight: bold;">${{statusLabel}}</span>
                            </div>
                        `;
                    }}

                    // 6. Update Llama.cpp Build version
                    const llamacppBuild = document.getElementById("llamacpp-build");
                    if (llamacppBuild) {{
                        llamacppBuild.textContent = "build " + data.llamacpp_build;
                    }}

                    // 7. Update pie chart gradients
                    const toolPie = document.getElementById("tool-token-pie-chart");
                    if (toolPie && data.pie_gradient) {{
                        // data.pie_gradient is like "background: conic-gradient(...)"
                        toolPie.style.cssText += data.pie_gradient;
                    }}
                    const routingPie = document.getElementById("routing-path-pie-chart");
                    if (routingPie && data.routing_pie_gradient) {{
                        routingPie.style.cssText += data.routing_pie_gradient;
                    }}

                }} catch (e) {{
                    console.error("Dashboard fetch failed: ", e);
                }}
            }}

            // Initialize on load and set periodic polling
            window.addEventListener("DOMContentLoaded", () => {{
                const basePath = window.location.pathname.endsWith('/') ? '../' : './';
                const visLink = document.getElementById("visualizer-link");
                if (visLink) {{
                    visLink.href = basePath + "visualizer";
                }}
                refreshDashboard();
            }});
            setInterval(refreshDashboard, 3000);
        </script>
    </head>
    <body>
        <header>
            <div class="logo-area">
                <div class="logo-dot"></div>
                <div class="logo-text">Antigravity Gateway</div>
            </div>
            <div class="dashboard-title">System Control Center</div>
            <div style="margin-top:8px;font-size:12px;opacity:0.6;">
                <a id="visualizer-link" href="visualizer" style="color:#818cf8;text-decoration:none;">📊 Dataset Visualizer</a>
            </div>
        </header>

        <div id="oauth-banner-container">
            {oauth_banner_html}
        </div>

        <main>
            <!-- LEFT COLUMN: LIVE TELEMETRY, METERS, PIES & TIMELINES -->
            <div>
                <!-- Analytics Card -->
                <div class="glass-card">
                    <div class="section-title">
                        <span>{src_badge("ROUTER", "#818cf8")} Gateway Performance Telemetry</span>
                        <span style="font-size: 12px; opacity: 0.5; font-weight: normal;">Persistent telemetry</span>
                    </div>

                    <div class="metrics-grid">
                        <div class="metric-box">
                            <span class="metric-value" id="total-requests">{total_requests}</span>
                            <span class="metric-label">Total API Calls</span>
                        </div>
                        <div class="metric-box">
                            <span class="metric-value" id="last-triage-decision" style="color: #c084fc; font-size: 20px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">{last_triage_decision}</span>
                            <span class="metric-label">Last Triage Split</span>
                        </div>
                        <div class="metric-box">
                            <span class="metric-value" id="avg-triage-latency">{avg_triage_latency_ms:.1f} ms</span>
                            <span class="metric-label">Avg Triage Time</span>
                        </div>
                        <div class="metric-box">
                            <span class="metric-value" id="avg-proxy-latency">{avg_proxy_latency_ms:.1f} ms</span>
                            <span class="metric-label">Avg Proxy Time</span>
                        </div>
                        <div class="metric-box">
                            <span class="metric-value" id="cache-hits" style="color: #34d399;">{cache_hits}</span>
                            <span class="metric-label">Triage Cache Hits</span>
                        </div>
                    </div>

                    <div style="background: rgba(255,255,255,0.01); border: 1px solid rgba(255,255,255,0.02); padding: 25px; border-radius: 20px;">
                        <div style="font-size: 13px; font-weight: 600; margin-bottom: 12px;">{src_badge("ROUTER", "#818cf8")} Triage Routing Split</div>
                        <div id="tier-table-container">
                            {tier_table_html}
                        </div>
                    </div>
                </div>

                <!-- Token Distribution & Circular Tool Pies Card -->
                <div class="glass-card">
                    <div class="section-title">
                        <span>{src_badge("ROUTER", "#818cf8")} Tool Token Distribution</span>
                        <span style="font-size: 12px; opacity: 0.5; font-weight: normal;">Live conic-gradient pie</span>
                    </div>
                    
                    <div style="display: flex; gap: 40px; align-items: center; margin-bottom: 30px; flex-wrap: wrap;">
                        <div class="pie-chart" id="tool-token-pie-chart" style="{pie_gradient}"></div>
                        <div style="display: flex; flex-direction: column; gap: 12px; flex-grow: 1; min-width: 200px;">
                            <h4 style="font-size: 14px; text-transform: uppercase; letter-spacing: 1px; opacity: 0.6; margin-bottom: 5px;">Active Tool Split %</h4>
                            <div id="pie-legend-container">
                                {pie_legend_html}
                            </div>
                        </div>
                    </div>

                    <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; border-top: 1px solid rgba(255,255,255,0.05); padding-top: 20px; text-align: center;">
                        <div>
                            <div class="metric-value" id="p-tokens" style="font-size: 20px; font-weight: 800; color: #60a5fa;">{p_tokens:,}</div>
                            <div style="font-size: 11px; text-transform: uppercase; opacity: 0.5; margin-top: 4px; font-weight: 600; letter-spacing: 0.5px;">Prompt Tokens</div>
                        </div>
                        <div>
                            <div class="metric-value" id="c-tokens" style="font-size: 20px; font-weight: 800; color: #a78bfa;">{c_tokens:,}</div>
                            <div style="font-size: 11px; text-transform: uppercase; opacity: 0.5; margin-top: 4px; font-weight: 600; letter-spacing: 0.5px;">Completion Tokens</div>
                        </div>
                        <div>
                            <div class="metric-value" id="t-tokens" style="font-size: 20px; font-weight: 800; color: #34d399;">{t_tokens:,}</div>
                            <div style="font-size: 11px; text-transform: uppercase; opacity: 0.5; margin-top: 4px; font-weight: 600; letter-spacing: 0.5px;">Combined Total</div>
                        </div>
                    </div>
                </div>

                <!-- Routing Path Distribution Pie -->
                <div class="glass-card">
                    <div class="section-title">
                        <span>{src_badge("ROUTER", "#818cf8")} Routing Path Distribution</span>
                        <span style="font-size: 12px; opacity: 0.5; font-weight: normal;">% requests per path</span>
                    </div>
                    <div style="display: flex; gap: 40px; align-items: center; flex-wrap: wrap;">
                        <div id="routing-path-pie-chart" style="width: 130px; height: 130px; border-radius: 50%; {routing_pie_gradient} box-shadow: 0 0 25px rgba(0,0,0,0.4); position: relative; flex-shrink: 0;">
                            <div style="position: absolute; width: 60px; height: 60px; background: #111827; border-radius: 50%; top: 35px; left: 35px; box-shadow: inset 0 0 10px rgba(0,0,0,0.8);"></div>
                        </div>
                        <div id="routing-legend-container" style="display: flex; flex-direction: column; gap: 12px; flex-grow: 1; min-width: 180px;">
                            {routing_legend_html if routing_legend_html else "<div style='opacity: 0.5; font-size: 13px;'>No routing data yet</div>"}
                        </div>
                    </div>
                </div>

                <!-- Final Model Usage: canonically tracked in Langfuse -->
                <div class="glass-card">
                    <div class="section-title">
                        <span>{src_badge("LITELLM", "#34d399")} Model Usage</span>
                        <span style="font-size: 12px; opacity: 0.5; font-weight: normal;">Full traces in Langfuse</span>
                    </div>
                    <div style="text-align: center; padding: 25px 20px;">
                        <p style="opacity: 0.7; margin-bottom: 14px; font-size: 14px;">Per-model usage, token consumption & cost are tracked with full trace detail in Langfuse.</p>
                        <a href="{langfuse_url}" target="_blank" style="display: inline-block; padding: 8px 18px; background: rgba(232,121,249,0.12); color: #e879f9; border: 1px solid rgba(232,121,249,0.25); border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 13px;">Open Langfuse Observability →</a>
                    </div>
                </div>

                <!-- Live Meters for Tool Tokens Card -->
                <div class="glass-card">
                    <div class="section-title">
                        <span>{src_badge("GOOSE", "#fbbf24")} Live Tool Token Meters</span>
                        <span style="font-size: 12px; opacity: 0.5; font-weight: normal;">Token meters per extension tool</span>
                    </div>
                    <div id="tool-tokens-container">
                        {tool_tokens_html}
                    </div>
                </div>

                <!-- Timelines Card -->
                <div class="glass-card" style="margin-bottom: 0;">
                    <div class="section-title">
                        <span>{src_badge("ROUTER", "#818cf8")} Request Timeline</span>
                        <span style="font-size: 12px; opacity: 0.5; font-weight: normal;">Recent completions cascade</span>
                    </div>
                    <div id="timeline-container" style="max-height: 400px; overflow-y: auto; padding-right: 5px;">
                        {timeline_html}
                    </div>
                </div>
            </div>

            <!-- RIGHT COLUMN: INFRASTRUCTURE & ACTIVE GOOSE SESSIONS -->
            <div style="display: flex; flex-direction: column;">
                <!-- Frontier Free Model widget -->
                <div class="glass-card" style="background: rgba(16, 185, 129, 0.03); border-color: rgba(16, 185, 129, 0.15); margin-bottom: 30px;">
                    <div class="section-title" style="margin-bottom: 10px; border-bottom: 1px solid rgba(16, 185, 129, 0.15); padding-bottom: 12px;">
                        <span>{src_badge("INTELLECT", "#34d399")} Frontier Free Model</span>
                        <span style="font-size: 11px; opacity: 0.4; font-weight: normal; font-family: monospace;">agentic index score</span>
                    </div>
                    <div id="best-free-model-container" style="background: rgba(255, 255, 255, 0.01); border: 1px solid rgba(255, 255, 255, 0.04); border-radius: 12px; padding: 16px 20px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <span style="font-weight: 800; font-size: 16px; color: #fff;">{best_free_model["name"]}</span>
                            <span style="font-size: 13px; font-weight: 800; padding: 4px 10px; border-radius: 20px; background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.25);">⚡ {best_free_model["score"]:.1f}</span>
                        </div>
                        <div style="font-size: 12px; font-family: monospace; opacity: 0.6; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-bottom: 8px;">
                            ID: {best_free_model["id"]}
                        </div>
                        <div style="display: flex; justify-content: space-between; font-size: 11px; opacity: 0.5;">
                            <span>📐 context {best_free_model["context_length"]:,} tok</span>
                            <span style="color: #34d399; font-weight: bold;">{"LIVE" if not best_free_model.get("is_fallback") else "FALLBACK"}</span>
                        </div>
                    </div>
                </div>

                <!-- Infrastructure nodes card -->
                <div class="glass-card status-container">
                    <div class="section-title" style="margin-bottom: 10px;">{src_badge("ROUTER", "#818cf8")} Infrastructure Nodes</div>
                    
                    <div class="service-row">
                        <div class="service-info">
                            <span class="service-name">Triage Router</span>
                            <span class="service-port">:{os.getenv('ROUTER_PORT') or '5000'}</span>
                        </div>
                        <span class="badge badge-online"><span class="pulse-dot"></span>Online</span>
                    </div>

                    <div class="service-row">
                        <div class="service-info">
                            <span class="service-name">LiteLLM Proxy</span>
                            <span class="service-port">:{os.getenv('LITELLM_PORT') or '4000'}</span>
                        </div>
                        <span id="litellm-status" class="badge {"badge-online" if litellm_status else "badge-offline"}">
                            <span class="pulse-dot"></span>{"Online" if litellm_status else "Offline"}
                        </span>
                    </div>

                    <div class="service-row">
                        <div class="service-info">
                            <span class="service-name">Valkey Cache</span>
                            <span class="service-port">:{_valkey_port()}</span>
                        </div>
                        <span id="valkey-status" class="badge {"badge-online" if valkey_status else "badge-offline"}">
                            <span class="pulse-dot"></span>{"Online" if valkey_status else "Offline"}
                        </span>
                    </div>

                    <div class="service-row">
                        <div class="service-info">
                            <span class="service-name">Llama-Server</span>
                            <span class="service-port">:8080</span>
                        </div>
                        <span id="llama-server-status" class="badge {"badge-online" if llama_server_status else "badge-offline"}">
                            <span class="pulse-dot"></span>{"Online" if llama_server_status else "Offline"}
                        </span>
                    </div>

                    <div class="service-row">
                        <div class="service-info">
                            <span class="service-name">Langfuse Traces</span>
                            <span class="service-port">:{os.getenv('LANGFUSE_WEB_PORT') or '3001'}</span>
                        </div>
                        <span id="langfuse-status" class="badge {"badge-online" if langfuse_status else "badge-offline"}">
                            <span class="pulse-dot"></span>{"Online" if langfuse_status else "Offline"}
                        </span>
                    </div>
                </div>

                <!-- Llama.cpp Metrics Card -->
                <div class="glass-card">
                    <div class="section-title" style="margin-bottom: 10px;">
                        <span>{src_badge("LLAMA.CPP", "#fb923c")} Engine Metrics</span>
                        <span id="llamacpp-build" style="font-size: 11px; opacity: 0.4; font-weight: normal; font-family: monospace;">build {data["llamacpp_build"]}</span>
                    </div>
                    <div id="llamacpp-models-container">
                        {llamacpp_models_html}
                    </div>
                    <div id="llamacpp-slots-container">
                        {llamacpp_slots_html}
                    </div>
                </div>

                <!-- Goose active sessions and status card -->
                <div class="glass-card">
                    <div class="section-title" style="margin-bottom: 10px;">{src_badge("GOOSE", "#fbbf24")} Session Directory</div>
                    <div id="goose-sessions-container" style="max-height: 420px; overflow-y: auto; padding-right: 5px;">
                        {goose_html}
                    </div>
                </div>

                <!-- Quick console links card -->
                <div class="glass-card status-container">
                    <div class="section-title" style="margin-bottom: 10px;">Quick Console Links</div>
                    <div class="btn-group">
                        <!-- Goose Dashboard local -->
                        <a href="https://t.me/SheepBot?start=goose" target="_blank" class="btn" style="background: rgba(251, 191, 36, 0.05); border-color: rgba(251, 191, 36, 0.2);">
                            <span>{src_badge("GOOSE", "#fbbf24")} 🦢 Goose Telegram Bot</span>
                            <span class="btn-arrow">→</span>
                        </a>
                    </div>
                    <div class="btn-group">
                        <a href="{langfuse_url}" target="_blank" class="btn">
                            <span>{src_badge("LANGFUSE", "#e879f9")} Observability UI</span>
                            <span class="btn-arrow">→</span>
                        </a>
                        <a href="{litellm_url}" target="_blank" class="btn">
                            <span>{src_badge("LITELLM", "#34d399")} Admin UI</span>
                            <span class="btn-arrow">→</span>
                        </a>
                        <a href="{llama_url}" target="_blank" class="btn">
                            <span>{src_badge("LLAMA.CPP", "#fb923c")} Server Router UI</span>
                            <span class="btn-arrow">→</span>
                        </a>
                    </div>
                </div>
            </div>
        </main>

        <footer>
            LLM Triage Gateway Control Center &copy; 2026. Made with Antigravity.
        </footer>
    </body>
    </html>
    """
    return html_content


# --- Static files (visualizer, data files) ---
STATIC_DIR = Path(__file__).resolve().parent / "static"
DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/data", StaticFiles(directory=str(DATA_DIR)), name="data")


@app.get("/visualizer", response_class=HTMLResponse)
async def get_visualizer():
    """Serve the dataset visualizer for human review."""
    vis_path = STATIC_DIR / "visualizer.html"
    if vis_path.exists():
        content = await asyncio.to_thread(vis_path.read_text, encoding="utf-8")
        return HTMLResponse(content)
    return HTMLResponse("<h2>Visualizer not found</h2>", status_code=404)


VALID_TIERS = {"agent-simple-core", "agent-medium-core", "agent-complex-core", "agent-reasoning-core", "agent-advanced-core"}
MAX_ANNOTATION_KEY_LENGTH = 128
MAX_ANNOTATION_ITEM_BYTES = 4096

class AnnotationItem(BaseModel):
    """Pydantic model representing a single human dataset review annotation."""
    model_config = ConfigDict(extra="forbid")

    tier: Union[int, str, None] = None
    note: Optional[str] = Field(default=None, max_length=1000)
    ts: Optional[str] = Field(default=None, max_length=100)

    @field_validator("tier")
    @classmethod
    def validate_tier(cls, v):
        """Validate the tier field of an AnnotationItem."""
        if v is None:
            return v
        if isinstance(v, int):
            if v < 0 or v > 4:
                raise ValueError(f"Invalid tier index {v}: must be between 0 and 4")
        elif isinstance(v, str):
            if v not in VALID_TIERS and v != "?":
                raise ValueError(f"Invalid tier string '{v}'")
        else:
            raise ValueError("Tier must be int, str, or null")
        return v

class AnnotationPayload(RootModel):
    """Pydantic model representing a payload of multiple annotations."""
    root: Dict[str, AnnotationItem]

    @model_validator(mode="after")
    def validate_payload(self) -> "AnnotationPayload":
        """Validate the entire annotation payload for size and key constraints."""
        data = self.root
        if len(data) > 1000:
            raise ValueError("Payload size limit exceeded: maximum of 1000 annotations allowed per request.")
        for k, item in data.items():
            if len(k) > MAX_ANNOTATION_KEY_LENGTH:
                raise ValueError(f"Invalid payload key '{k}': key is too long.")
            is_valid_key = k.isdigit() or (
                k.startswith("h") and len(k) > 1 and all(c in "0123456789abcdef" for c in k[1:].lower())
            )
            if not is_valid_key:
                raise ValueError(f"Invalid payload key '{k}': keys must be numeric strings or stable hash keys (e.g., 'h12345abc').")
            if len(item.model_dump_json().encode("utf-8")) > MAX_ANNOTATION_ITEM_BYTES:
                raise ValueError(f"Annotation '{k}' exceeds the maximum serialized size.")
        return self
# NOTE: annotations_lock (asyncio.Lock) only provides concurrency protection within
# a single Python process. In multi-worker uvicorn deployments, concurrent requests
# across different workers can still race. Eventual consistency is maintained via
# the atomic file-replace mechanism, which is acceptable for this dashboard feature.
annotations_lock = asyncio.Lock()


_annotations_cache = {}


async def _read_annotations_async(path) -> dict:
    """Read annotations from disk asynchronously with caching."""
    import copy

    # Do not swallow OSError if file doesn't exist to preserve original behavior.
    # The caller (save_annotations) handles the exception when reading existing annotations.
    current_mtime = await asyncio.to_thread(os.path.getmtime, path)

    cache_entry = _annotations_cache.get(path)

    if cache_entry is None or current_mtime != cache_entry["mtime"]:
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            # Read asynchronously, but parse in a thread pool to avoid blocking event loop
            content = await f.read()
            data = await asyncio.to_thread(json.loads, content)
            _annotations_cache[path] = {"mtime": current_mtime, "data": data}

    return copy.deepcopy(_annotations_cache[path]["data"])


@app.post("/dashboard/save-annotations")
async def save_annotations(payload: AnnotationPayload):
    """Save human review annotations to disk."""

    try:
        data = payload.root
        ann_path = DATA_DIR / "annotations.json"
        existing = {}
        async with annotations_lock:
            if ann_path.exists():
                try:
                    existing = await _read_annotations_async(str(ann_path))
                except Exception as read_err:
                    logger.warning(
                        f"Could not read existing annotations: {read_err}. Overwriting."
                    )

            # Merge new annotations into existing
            for k, item in data.items():
                # For partial updates, merge only fields provided in the request
                update_data = item.model_dump(exclude_unset=True)
                if k in existing and isinstance(existing[k], dict):
                    existing[k].update(update_data)
                else:
                    existing[k] = item.model_dump()
            await _atomic_write_json_async(str(ann_path), existing)
            _annotations_cache.pop(str(ann_path), None)

        return JSONResponse({"status": "ok", "saved": len(data)})
    except Exception as e:
        logger.error(f"Failed to save annotations: {e}")
        raise HTTPException(status_code=500, detail="Failed to save annotations")


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting LLM Triage Router on {host}:{port}...")
    uvicorn.run(app, host=host, port=port)
