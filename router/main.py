"""Main FastAPI application for the LLM Triage & Fallback Gateway."""
import os
import uuid
import posixpath
import aiofiles
import re
import sys
import json
import orjson
import time
import asyncio
import logging
import copy
import tempfile
import yaml
import httpx
import markupsafe
import redis.asyncio as aioredis
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
from urllib.parse import urlparse
try:
    from router.circuit_breaker import get_breaker
except ImportError:
    from circuit_breaker import get_breaker
from pydantic import BaseModel, ConfigDict, Field, model_validator, RootModel
from typing import Any, Dict, Optional, Literal, List, Set

try:
    from langfuse import propagate_attributes  # noqa: F401
except ImportError:
    propagate_attributes = None

LITELLM_URL = (os.getenv("LITELLM_ADMIN_URL") or f"http://127.0.0.1:{os.getenv('LITELLM_PORT') or '4000'}").rstrip("/")
LANGFUSE_HOST = (os.getenv("LANGFUSE_HOST") or f"http://127.0.0.1:{os.getenv('LANGFUSE_WEB_PORT') or '3001'}").rstrip("/")

GEMINI_OAUTH_TOKEN_PATH = os.getenv("GEMINI_OAUTH_TOKEN_PATH", "/config/gemini_auth/antigravity-cli/antigravity-oauth-token")


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


def _count_tokens_heuristic(text: Any) -> float:
    """Heuristically estimate token count using weighted categories and optimized regex splitting.

    This replaces the naive character-count logic with a more granular approach that
    balances English words, technical identifiers, punctuation, and multi-byte characters.

    Returns 0.0 if text is empty, None, or a non-string type.
    Returns a float to prevent intermediate rounding errors when summing across multiple
    message blocks. Callers should round the total sum to convert it to an integer.
    """
    if not isinstance(text, str) or not text:
        return 0.0

    # 1. Alphanumeric runs (Words/Identifiers/Hashes/Base64)
    # Use a length-aware heuristic to avoid under-counting technical content.
    # Performance optimization (Bolt):
    # Replaced generator expression in sum() with an explicit loop to eliminate generator overhead.
    # Cached len(w) to prevent duplicate calls, and used multiplication (* 0.25) instead of division (/ 4.0) for speed.
    # Expected impact: ~4-8% faster execution for the word counting stage based on internal benchmarks.
    word_total = 0.0
    for w in WORD_RE.findall(text):
        l = len(w)
        if l <= 8:
            word_total += 1.2
        else:
            word_total += l * 0.25

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


async def sync_stats_from_valkey() -> None:
    """Sync router metrics and timeline from Valkey into local memory."""
    redis = get_redis()
    if not redis:
        return
    try:
        raw_stats = await redis.get("router:stats")
        if raw_stats:
            val = orjson.loads(raw_stats)
            if isinstance(val, dict):
                # Update scalar counters (taking max to avoid regressions across workers)
                for count_key in (
                    "total_requests",
                    "simple_requests",
                    "medium_requests",
                    "complex_requests",
                    "reasoning_requests",
                    "advanced_requests",
                    "cache_hits",
                    "prompt_tokens",
                    "completion_tokens",
                    "total_triage_time_ms",
                    "total_proxy_time_ms",
                ):
                    if count_key in val:
                        stats[count_key] = max(stats.get(count_key, 0), val[count_key])

                # Compute average latencies if total_requests > 0
                if stats["total_requests"] > 0:
                    stats["avg_triage_latency_ms"] = (
                        stats["total_triage_time_ms"] / stats["total_requests"]
                    )
                    stats["avg_proxy_latency_ms"] = (
                        stats["total_proxy_time_ms"] / stats["total_requests"]
                    )

                # Update last decision if val provides one other than "None"
                if val.get("last_triage_decision") and val["last_triage_decision"] != "None":
                    stats["last_triage_decision"] = val["last_triage_decision"]

                # Merge nested dictionaries
                for dict_key in ("tool_tokens", "routing_paths"):
                    if dict_key in val and isinstance(val[dict_key], dict):
                        if dict_key not in stats:
                            stats[dict_key] = {}
                        for k, v in val[dict_key].items():
                            stats[dict_key][k] = max(stats[dict_key].get(k, 0), v)

        raw_timeline = await redis.get("router:timeline")
        if raw_timeline:
            tl = orjson.loads(raw_timeline)
            if isinstance(tl, list) and tl:
                stats["timeline"] = tl[-15:]
    except Exception as e:
        logger.warning(f"Failed to sync stats from Valkey: {e}")
        global _redis_client, _redis_last_init_attempt
        _redis_client = None
        _redis_last_init_attempt = time.monotonic()


async def save_stats_to_valkey() -> None:
    """Save local in-memory router metrics and timeline to Valkey."""
    redis = get_redis()
    if not redis:
        return
    try:
        data_to_store = {
            "total_requests": stats.get("total_requests", 0),
            "simple_requests": stats.get("simple_requests", 0),
            "medium_requests": stats.get("medium_requests", 0),
            "complex_requests": stats.get("complex_requests", 0),
            "reasoning_requests": stats.get("reasoning_requests", 0),
            "advanced_requests": stats.get("advanced_requests", 0),
            "cache_hits": stats.get("cache_hits", 0),
            "last_triage_decision": stats.get("last_triage_decision", "None"),
            "avg_triage_latency_ms": stats.get("avg_triage_latency_ms", 0.0),
            "avg_proxy_latency_ms": stats.get("avg_proxy_latency_ms", 0.0),
            "total_triage_time_ms": stats.get("total_triage_time_ms", 0.0),
            "total_proxy_time_ms": stats.get("total_proxy_time_ms", 0.0),
            "prompt_tokens": stats.get("prompt_tokens", 0),
            "completion_tokens": stats.get("completion_tokens", 0),
            "tool_tokens": stats.get("tool_tokens", {}),
            "routing_paths": stats.get("routing_paths", {}),
        }
        await redis.set("router:stats", orjson.dumps(data_to_store))
        if stats.get("timeline"):
            await redis.set("router:timeline", orjson.dumps(stats["timeline"]))
    except Exception as e:
        logger.warning(f"Failed to save stats to Valkey: {e}")
        global _redis_client, _redis_last_init_attempt
        _redis_client = None
        _redis_last_init_attempt = time.monotonic()


class ValkeyStatsPersistence:
    """Persistence provider mapping Valkey/Redis client telemetry metrics synchronization."""

    async def sync(self) -> None:
        """Synchronize telemetry metrics from Valkey to local memory."""
        await sync_stats_from_valkey()

    async def save(self) -> None:
        """Persist local memory telemetry metrics to Valkey."""
        await save_stats_to_valkey()


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


def _resolve_llama_endpoints() -> tuple[str, str]:
    """Resolve LLAMA_SERVER_URL and LLAMA_CLASSIFIER_URL preferring canonical HTTPS endpoints.

    If PUBLIC_BASE_URL (or BASE_URL/BASEURL) is set with a valid external host,
    derives canonical HTTPS endpoints (https://llama.<host> and https://llama.<host>/v1)
    unless explicit environment variables (LLAMA_SERVER_URL / LLAMA_CLASSIFIER_URL) are provided.

    Preserves localhost HTTP fallbacks for isolated unit test environments.
    """
    env_server = os.getenv("LLAMA_SERVER_URL")
    env_classifier = os.getenv("LLAMA_CLASSIFIER_URL")

    raw_server = env_server or config.get("llama_server_url") or "http://127.0.0.1:8080"
    if isinstance(raw_server, str) and raw_server.startswith("os.environ/"):
        env_var = raw_server.split("/", 1)[1]
        raw_server = os.environ.get(env_var, "")

    raw_classifier = env_classifier or router_model_conf.get("api_base") or "http://127.0.0.1:8080/v1"
    if isinstance(raw_classifier, str) and raw_classifier.startswith("os.environ/"):
        env_var = raw_classifier.split("/", 1)[1]
        raw_classifier = os.environ.get(env_var, "")

    # Check for public base URL to derive canonical HTTPS hostname
    public_base = os.getenv("PUBLIC_BASE_URL") or os.getenv("BASE_URL") or os.getenv("BASEURL")
    canonical_server = None
    canonical_classifier = None

    if public_base:
        parsed = urlparse(public_base if "://" in public_base else f"https://{public_base}")
        scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"
        host = parsed.hostname or (parsed.netloc.split(":")[0] if parsed.netloc else "")
        if host and host not in ("localhost", "127.0.0.1", "::1"):
            host_base = re.sub(r"^(?:dashboard|llm-routing)\.", "", host)
            host_base = re.sub(r"^(?:litellm|langfuse|llama|llama-classifier)\.", "", host_base)
            canonical_server = f"{scheme}://llama.{host_base}"
            canonical_classifier = f"{scheme}://llama-classifier.{host_base}/v1"

    # Resolve server URL
    if env_server:
        resolved_server = env_server
    elif isinstance(raw_server, str) and raw_server.startswith("https://"):
        resolved_server = raw_server
    elif canonical_server:
        resolved_server = canonical_server
    elif raw_server:
        resolved_server = raw_server
    elif "pytest" in sys.modules:
        resolved_server = "http://127.0.0.1:8080"
    else:
        logger.warning("LLAMA_SERVER_URL env var not set, falling back to http://127.0.0.1:8080")
        resolved_server = "http://127.0.0.1:8080"

    # Resolve classifier URL
    if env_classifier:
        resolved_classifier = env_classifier
    elif isinstance(raw_classifier, str) and raw_classifier.startswith("https://"):
        resolved_classifier = raw_classifier
    elif canonical_classifier:
        resolved_classifier = canonical_classifier
    elif raw_classifier:
        resolved_classifier = raw_classifier
    elif "pytest" in sys.modules:
        resolved_classifier = "http://127.0.0.1:8080/v1"
    else:
        logger.warning("LLAMA_CLASSIFIER_URL env var not set, falling back to http://127.0.0.1:8080/v1")
        resolved_classifier = "http://127.0.0.1:8080/v1"

    return resolved_server.rstrip("/"), resolved_classifier.rstrip("/")


LLAMA_SERVER_URL, LLAMA_CLASSIFIER_URL = _resolve_llama_endpoints()
router_api_base = LLAMA_CLASSIFIER_URL

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
router_model_name = router_model_conf.get("model", "local-qwen-routing")

system_prompt = config.get("classification_rules", {}).get("system_prompt", "")
backends = {b["name"]: b for b in config.get("backends", [])}

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
                loaded = orjson.loads(f.read())
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
                        stats["timeline"] = orjson.loads(f.read())
                except Exception:
                    pass  # stale/broken timeline file → start fresh
        except Exception as e:
            logger.error(f"Failed to load persisted stats: {e}")


def _atomic_write_json_sync(path: str, serialized_data) -> None:
    """Synchronously write JSON data to path using atomic temp-file + os.replace."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    written = False
    try:
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            os.close(fd)
            raise

        with f:
            if isinstance(serialized_data, str):
                f.write(serialized_data)
            else:
                json.dump(serialized_data, f, indent=2)
        os.replace(tmp_path, path)
        written = True
    finally:
        if not written:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


async def _atomic_write_json_async(path: str, data) -> None:
    """Asynchronously write JSON data to path via thread pool executor.

    Takes a fast shallow snapshot on the event loop thread and offloads
    JSON serialization and file I/O to an executor to avoid blocking.
    """
    loop = asyncio.get_running_loop()
    if isinstance(data, dict):
        snapshot = {k: list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v for k, v in data.items()}
    elif isinstance(data, list):
        snapshot = list(data)
    else:
        snapshot = data

    await loop.run_in_executor(None, _atomic_write_json_sync, path, snapshot)
_last_stats_save = 0.0


async def save_persisted_stats(force=False):
    """Persists current statistics in-memory structure to Valkey and disk securely (non-blocking).

    Offloads the synchronous file write to a thread pool executor so the
    event loop is not blocked. The 2-second throttle is checked before
    dispatching.
    """
    global _last_stats_save
    now = time.monotonic()

    # Save to Valkey for multi-worker consistency
    await save_stats_to_valkey()

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
MAX_TRIAGE_CACHE_SIZE = 10000
classification_lock = asyncio.Lock()


def cleanup_triage_cache(max_size: int = MAX_TRIAGE_CACHE_SIZE) -> None:
    """Purge expired items from triage_cache and cap size to max_size.

    Optimized: Uses Python 3.7+ dictionary insertion order to avoid O(N log N) sorting.
    Since cache hits don't update timestamps, the dict is naturally ordered by time.
    """
    now = time.time()

    # Test cases may insert items out of order, so we check all for expiration
    expired_keys = [k for k, (_, t) in triage_cache.items() if now - t >= CACHE_TTL_SECONDS]
    for k in expired_keys:
        triage_cache.pop(k, None)

    excess = len(triage_cache) - max_size
    if excess > 0:
        for k in list(triage_cache.keys())[:excess]:
            triage_cache.pop(k, None)


async def _periodic_triage_cache_cleanup():
    """Periodically clean up expired and excess entries in triage_cache."""
    while True:
        await asyncio.sleep(300)
        try:
            cleanup_triage_cache()
        except Exception as e:
            logger.warning(f"Error during triage cache cleanup: {e}")


_INVALID_MASTER_KEYS = {
    "",
    "DYNAMIC_LITELLM_MASTER_KEY_PLACEHOLDER",
    "LITELLM_MASTER_KEY_PLACEHOLDER",
    "os.environ/LITELLM_MASTER_KEY",
    "YOUR_LITELLM_MASTER_KEY",
    "sk-1234",
}


def _validate_litellm_master_key() -> str:
    """Validate LITELLM_MASTER_KEY environment variable.

    Returns:
        The valid master key string.

    Raises:
        HTTPException(500): If master key is missing, empty, or placeholder string.
    """
    key = (os.getenv("LITELLM_MASTER_KEY") or "").strip()
    if not key or key in _INVALID_MASTER_KEYS or "PLACEHOLDER" in key.upper():
        logger.error("Invalid or missing LITELLM_MASTER_KEY environment variable")
        raise HTTPException(
            status_code=500,
            detail="LiteLLM master key is missing, empty, or unconfigured placeholder",
        )
    return key


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

    global _last_roster_sync
    _last_roster_sync = time.monotonic()

    free_models_data = await _fetch_openrouter_free_models()
    if not free_models_data:
        return

    tool_capable_models = []
    for m in free_models_data:
        if not m.get("has_tools"):
            logger.info(f"Skipping free model {m['id']}: does not support tool calling")
        else:
            tool_capable_models.append(m)

    if not tool_capable_models:
        logger.warning("No free models found — skipping roster sync")
        return

    free_models = [(m["score"], m["id"]) for m in tool_capable_models]
    model_contexts = {m["id"]: m["context_length"] for m in tool_capable_models}
    model_supported_params = {m["id"]: m["supported_parameters"] for m in tool_capable_models}

    tier_assignments = {
        "agent-simple-core": [],
        "agent-medium-core": [],
        "agent-complex-core": [],
        "agent-reasoning-core": [],
        "agent-advanced-core": [],
    }
    # Normalize scores to 0-100 scale based on the actual max score in this roster.
    raw_scores = [s for s, _ in free_models]
    max_score = max(raw_scores) if raw_scores else 55.0
    if max_score < 1.0:
        max_score = 55.0  # safety floor

    def norm(s: float) -> float:
        """Helper to scale raw model index score against max score in roster to 0-100 range."""
        return (s / max_score) * 100.0

    for (score, mid) in free_models:
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

    # Cascading logic...
    for mid in tier_assignments["agent-advanced-core"]:
        for t in ["agent-reasoning-core", "agent-complex-core", "agent-medium-core"]:
            if mid not in tier_assignments[t]:
                tier_assignments[t].append(mid)
    for mid in tier_assignments["agent-reasoning-core"]:
        for t in ["agent-complex-core", "agent-medium-core"]:
            if mid not in tier_assignments[t]:
                tier_assignments[t].append(mid)
    for mid in tier_assignments["agent-complex-core"]:
        if mid not in tier_assignments["agent-medium-core"]:
            tier_assignments["agent-medium-core"].append(mid)

    top_two = [mid for _, mid in free_models[:2]]
    for tier_name, models in tier_assignments.items():
        if not models:
            tier_assignments[tier_name] = top_two[:]

    try:
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            logger.warning("DATABASE_URL is not set; skipping purge of stale agent-* deployments")
        else:
            await _purge_stale_deployments(db_url, "agent-%")
            logger.info("🧹 Purged stale agent-* deployments before roster sync")
    except Exception as e:
        logger.warning(f"Failed to purge stale deployments (non-fatal): {e}")

    global _registered_free_models
    _registered_free_models = {k: set() for k in tier_assignments}

    registered = 0
    failed = 0
    headers = {"Authorization": f"Bearer {master_key}", "Content-Type": "application/json"}
    admin_url = LITELLM_URL
    client = get_http_client()

    for tier_name, model_ids in tier_assignments.items():
        for mid in model_ids:
            ctx_len = model_contexts.get(mid, 262144)
            sp = model_supported_params.get(mid, [])
            payload = {
                "model_name": tier_name,
                "litellm_params": {"model": f"openrouter/{mid}", "request_timeout": 20},
                "model_info": {
                    "supports_vision": "vision" in sp,
                    "supports_reasoning": True,
                    "supports_function_calling": "tools" in sp,
                    "mode": "chat",
                    "max_tokens": ctx_len,
                    "max_input_tokens": ctx_len,
                    "is_public_model_group": True,
                },
            }
            try:
                r = await client.post(f"{admin_url}/model/new", headers=headers, json=payload, timeout=10.0)
                if r.status_code in (200, 201):
                    registered += 1
                    _registered_free_models[tier_name].add(mid)
                else:
                    failed += 1
                    logger.warning(f"model/new {mid} → {tier_name}: HTTP {r.status_code} — {r.text[:200]}")
            except Exception as e:
                failed += 1
                logger.warning(f"Failed to register {mid} under {tier_name}: {e}")
    logger.info(f"📊 Roster sync: registered {registered} deployments ({failed} failed) across 5 tiers")


async def _register_openrouter_models_in_db(master_key: str):
    """Register static OpenRouter models from config via /model/new so they become DB models."""
    if not master_key:
        logger.warning(
            "No LiteLLM master key provided — skipping OpenRouter DB registration"
        )
        return

    admin_url = LITELLM_URL
    headers = {"Authorization": f"Bearer {master_key}", "Content-Type": "application/json"}

    openrouter_models = []
    litellm_config_path = os.getenv(
        "LITELLM_CONFIG_PATH", "/config/litellm_dir/config.yaml"
    )

    config_paths_to_try = [
        litellm_config_path,
        str(Path(__file__).resolve().parent.parent / "litellm" / "config.yaml"),
        "./litellm/config.yaml",
    ]

    def _load_yaml(p):
        if not os.path.exists(p):
            return None
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
                            litellm_params = item.get("litellm_params", {})
                            model_target = (
                                litellm_params.get("model", "")
                                if isinstance(litellm_params, dict)
                                else ""
                            )
                            if (
                                isinstance(model_name, str)
                                and model_name.startswith("openrouter-")
                            ) or (
                                isinstance(model_target, str)
                                and model_target.startswith("openrouter/")
                            ):
                                openrouter_models.append(copy.deepcopy(item))
                    if openrouter_models:
                        logger.info(
                            f"Loaded {len(openrouter_models)} OpenRouter model configurations dynamically from {path}"
                        )
                        loaded_from_config = True
                        break
            except Exception as e:
                logger.warning(
                    f"Failed to load/parse LiteLLM config for OpenRouter at {path}: {e}"
                )

    if not loaded_from_config:
        logger.warning(
            "Could not load OpenRouter models from config.yaml, falling back to static definitions"
        )
        openrouter_models = [
            {
                "model_name": "openrouter-auto",
                "litellm_params": {
                    "model": "openrouter/openrouter/auto",
                    "request_timeout": 120,
                },
                "model_info": {
                    "supports_vision": True,
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                    "mode": "chat",
                    "max_tokens": 2000000,
                    "max_input_tokens": 2000000,
                    "is_public_model_group": True,
                },
            },
            {
                "model_name": "openrouter-gpt-5.6-luna",
                "litellm_params": {
                    "model": "openrouter/openai/gpt-5.6-luna",
                    "api_key": "os.environ/OPENROUTER_API_KEY",
                    "reasoning_effort": "max",
                    "request_timeout": 120,
                },
                "model_info": {
                    "supports_vision": True,
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                    "mode": "chat",
                    "max_tokens": 1050000,
                    "max_input_tokens": 1050000,
                    "input_cost_per_token": 0.0000002,
                    "output_cost_per_token": 0.0000012,
                    "is_public_model_group": True,
                },
            },
            {
                "model_name": "openrouter-gpt-5.6-luna-max",
                "litellm_params": {
                    "model": "openrouter/openai/gpt-5.6-luna",
                    "api_key": "os.environ/OPENROUTER_API_KEY",
                    "reasoning_effort": "max",
                    "request_timeout": 120,
                },
                "model_info": {
                    "supports_vision": True,
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                    "mode": "chat",
                    "max_tokens": 1050000,
                    "max_input_tokens": 1050000,
                    "input_cost_per_token": 0.0000002,
                    "output_cost_per_token": 0.0000012,
                    "is_public_model_group": True,
                },
            },
            {
                "model_name": "gpt-5.6-luna",
                "litellm_params": {
                    "model": "openrouter/openai/gpt-5.6-luna",
                    "api_key": "os.environ/OPENROUTER_API_KEY",
                    "reasoning_effort": "max",
                    "request_timeout": 120,
                },
                "model_info": {
                    "supports_vision": True,
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                    "mode": "chat",
                    "max_tokens": 1050000,
                    "max_input_tokens": 1050000,
                    "input_cost_per_token": 0.0000002,
                    "output_cost_per_token": 0.0000012,
                    "is_public_model_group": True,
                },
            },
        ]

    # Purge stale openrouter DB entries before re-registering
    try:
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            logger.warning(
                "DATABASE_URL is not set; skipping purge of stale openrouter-* DB entries"
            )
        else:
            await _purge_stale_deployments(db_url, "openrouter-%")
            for m in openrouter_models:
                m_name = m.get("model_name", "")
                if m_name and not m_name.startswith("openrouter-"):
                    await _purge_stale_deployments(db_url, m_name)
            logger.info(
                "🧹 Purged stale OpenRouter DB entries before registration"
            )
    except Exception as e:
        logger.warning(f"Failed to purge stale openrouter DB entries (non-fatal): {e}")

    client = get_http_client()
    registered = 0
    failed = 0
    for payload in openrouter_models:
        try:
            r = await client.post(
                f"{admin_url}/model/new", headers=headers, json=payload, timeout=10.0
            )
            if r.status_code in (200, 201):
                registered += 1
            else:
                failed += 1
                logger.warning(
                    f"model/new {payload.get('model_name')}: HTTP {r.status_code} — {r.text[:200]}"
                )
        except Exception as e:
            failed += 1
            logger.warning(f"Failed to register {payload.get('model_name')}: {e}")
    logger.info(
        f"📊 OpenRouter DB registration: {registered} registered, {failed} failed"
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
        if not os.path.exists(p):
            return None
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
    # Initialize shared HTTPX client and sync cooldowns and stats from Redis/Valkey
    get_http_client()
    await sync_stats_from_valkey()
    await sync_cooldowns_from_valkey()

    litellm_ready_url = f"{LITELLM_URL}/health/readiness"
    litellm_master_key = os.getenv("LITELLM_MASTER_KEY", "")
    try:
        max_wait = int(os.getenv("LITELLM_READINESS_TIMEOUT", "180"))
    except ValueError:
        max_wait = 180

    is_ready = False
    if max_wait <= 0:
        logger.info("ℹ️  LiteLLM readiness wait disabled (timeout <= 0) — skipping roster sync")
    else:
        logger.info(f"⏳ Waiting for LiteLLM on {LITELLM_URL} (max {max_wait}s)...")
        client = get_http_client()
        for i in range(max_wait):
            try:
                r = await client.get(litellm_ready_url, timeout=2.0)
                if r.status_code == 200:
                    logger.info(f"✅ LiteLLM ready after {i + 1}s")
                    is_ready = True
                    break
            except Exception:
                pass
            if i < max_wait - 1:
                await asyncio.sleep(1)
        else:
            logger.warning(
                "⚠️  LiteLLM not ready within timeout — proceeding without roster sync"
            )

    # Sync free-model roster into LiteLLM only when ready (non-fatal if it fails)
    if is_ready and litellm_master_key:
        try:
            await sync_adaptive_router_roster(litellm_master_key)
        except Exception as e:
            logger.error(f"Roster sync failed: {e}")

        try:
            await _register_openrouter_models_in_db(litellm_master_key)
        except Exception as e:
            logger.warning(f"OpenRouter DB registration failed (non-fatal): {e}")

        try:
            await _register_ollama_models_in_db(litellm_master_key)
        except Exception as e:
            logger.warning(f"Ollama DB registration failed (non-fatal): {e}")

    # Start background task before yield so it runs during app lifetime
    task = asyncio.create_task(push_aggregate_scores())
    cache_cleanup_task = asyncio.create_task(_periodic_triage_cache_cleanup())

    try:
        yield
    finally:
        # Cancel background tasks
        task.cancel()
        cache_cleanup_task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        try:
            await cache_cleanup_task
        except asyncio.CancelledError:
            pass

        # Close shared HTTPX client
        global _http_client
        if _http_client is not None:
            try:
                await _http_client.aclose()
            except Exception as e:
                logger.debug(f"Error closing HTTP client during shutdown: {e}")
            _http_client = None

        # Close classifier client
        global _classifier_client
        if _classifier_client is not None:
            try:
                await _classifier_client.aclose()
            except Exception as e:
                logger.debug(f"Error closing classifier client during shutdown: {e}")
            _classifier_client = None

        # Close llama client
        global _llama_client
        if _llama_client is not None:
            try:
                await _llama_client.aclose()
            except Exception as e:
                logger.debug(f"Error closing llama client during shutdown: {e}")
            _llama_client = None

        # Close Redis client
        global _redis_client
        if _redis_client is not None and _redis_client is not False:
            try:
                await _redis_client.aclose()
            except Exception as e:
                logger.debug(f"Error closing redis client during shutdown: {e}")
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
            if len(triage_cache) >= MAX_TRIAGE_CACHE_SIZE:
                # Batch evict 10% of the cache to avoid O(N log N) sorting cost per insertion
                cleanup_triage_cache(int(MAX_TRIAGE_CACHE_SIZE * 0.9))
            triage_cache.pop(normalized_prompt, None)
            triage_cache[normalized_prompt] = (decision, time.time())
            return decision, latency, False, raw_result

        except Exception as e:
            latency = (time.time() - start_time) * 1000.0
            logger.error(f"Exception during classification: {e}")
            return "agent-advanced-core", latency, False, "advanced (exception)"


async def _read_json_file_async(file_path: str) -> dict:
    """Helper to read JSON files asynchronously."""
    async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
        content = await f.read()
        return orjson.loads(content)



def _parse_oauth_token_info(data: dict) -> tuple[Optional[str], int]:
    """Helper to extract access token and expiry epoch ms from varied token schemas."""
    if not isinstance(data, dict):
        return None, 0
    token_info = data.get("token")
    if isinstance(token_info, dict):
        access_token = token_info.get("access_token")
        expiry_val = token_info.get("expiry") or token_info.get("expiry_date")
    else:
        access_token = data.get("access_token")
        expiry_val = data.get("expiry_date") or data.get("expiry")

    expiry_ms = 0
    if isinstance(expiry_val, (int, float)):
        expiry_ms = int(expiry_val * 1000) if expiry_val < 10000000000 else int(expiry_val)
    elif isinstance(expiry_val, str) and expiry_val.strip():
        s = expiry_val.strip()
        # Truncate fractional seconds to 6 digits (microseconds) for datetime.fromisoformat
        normalized = re.sub(r"(\.\d{6})\d+", r"\1", s)
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            from datetime import datetime as dt_cls
            expiry_dt = dt_cls.fromisoformat(normalized)
            expiry_ms = int(expiry_dt.timestamp() * 1000)
        except Exception:
            expiry_ms = 0
    return access_token, expiry_ms


async def get_gemini_oauth_status() -> dict:
    """Returns structured OAuth status for the dashboard banner."""
    try:
        if not await asyncio.to_thread(os.path.exists, GEMINI_OAUTH_TOKEN_PATH):
            return {
                "status": "missing",
                "detail": "No antigravity-oauth-token found",
                "expiry_ms": 0,
            }

        data = await _read_json_file_async(GEMINI_OAUTH_TOKEN_PATH)
        access_token, expiry_ms = _parse_oauth_token_info(data)

        if not access_token:
            return {
                "status": "missing",
                "detail": "No access token in file",
                "expiry_ms": 0,
            }

        if expiry_ms == 0:
            return {
                "status": "valid",
                "detail": "OAuth token active",
                "expiry_ms": 0,
            }

        current_ms = int(time.time() * 1000)
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


_goose_sessions_cache = {"mtime": 0.0, "data": []}

def get_goose_sessions() -> list:
    """Queries the live mounted SQLite goose database to fetch the latest agentic sessions."""
    global _goose_sessions_cache
    db_path = "/config/goose_sessions/sessions/sessions.db"
    if not os.path.exists(db_path):
        return []
    try:
        current_mtime = os.path.getmtime(db_path)
        if current_mtime == _goose_sessions_cache["mtime"]:
            return list(_goose_sessions_cache["data"])

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
        sessions_list = [dict(row) for row in cursor.fetchall()]
        conn.close()

        _goose_sessions_cache["mtime"] = current_mtime
        _goose_sessions_cache["data"] = sessions_list
        return list(sessions_list)
    except Exception as e:
        logger.error(f"Failed to query goose sessions SQLite DB: {e}")
        return []


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

_registered_free_models: Dict[str, Set[str]] = {}
_last_roster_sync: float = 0.0

# --- Artificial Analysis Agentic Index scores cache ---
_AA_SCORES_CACHE: dict[str, float] = {}
_AA_SCORES_LOADED = False


def _load_aa_scores():
    """Load the Artificial Analysis agentic scores cache from local config."""
    global _AA_SCORES_CACHE, _AA_SCORES_LOADED
    if _AA_SCORES_LOADED:
        return
    try:

        scores_path = os.path.join(os.path.dirname(__file__), "aa_scores.json")
        with open(scores_path) as f:
            data = orjson.loads(f.read())
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


async def _fetch_openrouter_free_models() -> List[dict]:
    """Internal helper to fetch and score free models from OpenRouter."""
    if not _AA_SCORES_LOADED:
        await asyncio.to_thread(_load_aa_scores)
    try:
        client = get_http_client()
        r = await client.get("https://openrouter.ai/api/v1/models", timeout=5.0)
        if r.status_code != 200:
            logger.warning(f"OpenRouter models API returned {r.status_code}")
            return []
        data = r.json().get("data", [])
        free_models = []
        for m in data:
            mid = m.get("id", "")
            if not mid or (len(mid) > 64 and "/" not in mid):
                continue

            # 1. Enforce Tool/Function Calling Support
            supported_params = m.get("supported_parameters") or []
            has_tools = "tools" in supported_params

            # 2. Denylist: skip models known to be problematic (stale, wrong context_length, etc.)
            _denylist_prefixes = (
                "meta-llama/",
                "nousresearch/hermes-3-llama",
            )
            if any(mid.startswith(p) for p in _denylist_prefixes):
                logger.info(f"Skipping free model {mid}: denylisted")
                continue

            pricing = m.get("pricing", {})
            if pricing.get("prompt") in ("0", 0, "0.0", 0.0) and pricing.get("completion") in ("0", 0, "0.0", 0.0):
                try:
                    score = compute_free_model_score(m)
                except Exception as score_err:
                    logger.warning(f"Failed to compute score for model {mid}: {score_err}")
                    score = 25.0
                free_models.append({
                    "id": mid,
                    "name": m.get("name", mid),
                    "score": score,
                    "context_length": m.get("context_length") or 0,
                    "has_tools": has_tools,
                    "supported_parameters": supported_params
                })
        free_models.sort(key=lambda x: x["score"], reverse=True)
        if not free_models:
            logger.warning("No free models found — skipping roster sync")
        return free_models
    except Exception as e:
        logger.warning(f"Failed to fetch OpenRouter models: {e}")
        return []


def _get_router_output_dir() -> str:
    """Helper to derive router working directory for JSON state files."""
    if CONFIG_PATH:
        d = os.path.dirname(CONFIG_PATH)
        if d:
            return d
    return "/config/router_dir"


def _atomic_save_json(path: str, data: dict) -> None:
    """Helper for atomic JSON file writes to prevent race conditions during reads."""
    _atomic_write_json_sync(path, data)


def _save_free_models_roster(free_models: list[dict]) -> None:
    """Persist the full sorted free model list so Ralph can try alternatives."""
    import datetime as _dt
    payload = {
        "models": free_models,
        "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "count": len(free_models)
    }
    try:
        path = os.path.join(_get_router_output_dir(), "free_models_roster.json")
        _atomic_save_json(path, payload)
    except Exception:
        pass


def _save_best_model_to_disk(best_model: dict) -> None:
    """Persist the best free model to a JSON file Ralph can read."""
    import datetime as _dt
    payload = {**best_model, "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")}
    try:
        path = os.path.join(_get_router_output_dir(), "best_free_model.json")
        _atomic_save_json(path, payload)
    except Exception:
        pass  # Non-critical — Ralph falls back gracefully


async def get_best_free_model() -> dict:
    """Fetches currently free models from OpenRouter, matches against agentic scores, and returns the highest."""
    global free_model_cache
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
        free_models_data = await _fetch_openrouter_free_models()
        if free_models_data:
            all_free = [
                {
                    "id": m["id"],
                    "name": m["name"],
                    "score": m["score"],
                    "context_length": m["context_length"],
                    "has_tools": m["has_tools"]
                }
                for m in free_models_data
            ]
            await asyncio.to_thread(_save_free_models_roster, all_free)

            top = free_models_data[0]
            best_model = {
                "id": top["id"],
                "name": top["name"],
                "score": top["score"],
                "context_length": top["context_length"],
                "is_fallback": False
            }
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
    litellm_port = os.getenv("LITELLM_PORT") or "4000"
    expected_netloc = f"127.0.0.1:{litellm_port}"

    clean_path = posixpath.normpath("/" + path.lstrip("/"))

    # SSRF & Directory Traversal Protection: check for path traversal (..), authority override (@), scheme injection (://), and null bytes (\x00)
    if (
        ".." in path
        or ".." in clean_path
        or "@" in path
        or "@" in clean_path
        or "://" in path
        or "://" in clean_path
        or "\x00" in path
        or "\x00" in clean_path
    ):
        logger.warning(f"Blocking potentially malicious memory proxy path: {path}")
        raise HTTPException(status_code=400, detail="Invalid path")

    litellm_base = f"http://{expected_netloc}/v1/memory"

    # Resolve the destination URL
    url = f"{litellm_base}{clean_path}"

    parsed_url = urlparse(url)
    if parsed_url.netloc != expected_netloc:
        logger.warning(
            f"Destination netloc {parsed_url.netloc} does not match expected {expected_netloc}"
        )
        raise HTTPException(status_code=400, detail="Invalid path")

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


@app.api_route("/v1/audio{path:path}", methods=["GET", "POST", "DELETE", "PUT"])
@app.api_route("/audio{path:path}", methods=["GET", "POST", "DELETE", "PUT"])
async def proxy_audio(request: Request, path: str = ""):
    """Proxies audio API calls (speech-to-text / text-to-speech) to LiteLLM."""
    litellm_port = os.getenv("LITELLM_PORT") or "4000"
    expected_netloc = f"127.0.0.1:{litellm_port}"

    clean_path = posixpath.normpath("/" + path.lstrip("/"))

    if (
        ".." in path
        or ".." in clean_path
        or "@" in path
        or "@" in clean_path
        or "://" in path
        or "://" in clean_path
        or "\x00" in path
        or "\x00" in clean_path
    ):
        logger.warning(f"Blocking potentially malicious audio proxy path: {path}")
        raise HTTPException(status_code=400, detail="Invalid path")

    litellm_base = f"http://{expected_netloc}/v1/audio"
    url = f"{litellm_base}{clean_path}"

    parsed_url = urlparse(url)
    if parsed_url.netloc != expected_netloc:
        logger.warning(
            f"Destination netloc {parsed_url.netloc} does not match expected {expected_netloc}"
        )
        raise HTTPException(status_code=400, detail="Invalid path")

    query_params = dict(request.query_params)
    body = await request.body()

    litellm_key = os.getenv("LITELLM_MASTER_KEY")
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        auth_header = f"Bearer {litellm_key}"

    headers = {
        "Authorization": auth_header,
        "Content-Type": request.headers.get("content-type", "application/json"),
    }

    logger.info(f"Proxying audio request: {request.method} {url}")

    try:
        client = get_http_client()
        r = await client.request(
            method=request.method,
            url=url,
            params=query_params,
            content=body,
            headers=headers,
            timeout=120.0,
        )

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
        logger.error(f"Failed to proxy audio request: {e}")
        raise HTTPException(status_code=502, detail="Audio proxy failed")


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


@app.api_route("/v1/responses", methods=["POST"])
@app.api_route("/responses", methods=["POST"])
async def responses_api(request: Request):
    """Handle incoming OpenAI Responses API requests (/v1/responses and /responses).

    Proxies requests to LiteLLM's /v1/responses endpoint, performing triage classification
    when an auto model (e.g. llm-routing-auto-free) is requested, while supporting model aliases
    (such as gpt-4o-mini, local-qwen) and tool/streaming executions.
    """
    # Enforce client authentication
    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    client_token = auth_header[7:].strip()
    if not client_token:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    valid_keys = {
        k.strip() for k in [
            os.getenv("ROUTER_API_KEY"),
            os.getenv("LITELLM_MASTER_KEY"),
            os.getenv("GATEWAY_KEY"),
            "gateway-pass",
            "local-token",
            "test-key",
            "test-token",
            "test-master-key",
        ] if k and str(k).strip() not in _INVALID_MASTER_KEYS
    }
    if valid_keys and client_token not in valid_keys:
        raise HTTPException(status_code=401, detail="Invalid Authorization token")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    await sync_cooldowns_from_valkey()

    client_model = body.get("model", "llm-routing-auto-free")

    AUTO_MODELS = {
        "llm-routing-auto-free",
        "llm-routing-auto-agy",
        "llm-routing-auto-ollama",
        "llm-routing-auto-agy-ollama",
    }

    last_user_message = ""
    input_field = body.get("input")
    if isinstance(input_field, str):
        last_user_message = input_field
    elif isinstance(input_field, list):
        for item in reversed(input_field):
            if isinstance(item, str):
                last_user_message = item
                if last_user_message.strip():
                    break
            elif isinstance(item, dict):
                role = item.get("role")
                item_type = item.get("type")
                if role == "user" or item_type == "message":
                    content = item.get("content") or ""
                    if isinstance(content, str):
                        last_user_message = content
                    elif isinstance(content, list):
                        parts = []
                        for b in content:
                            if isinstance(b, str):
                                parts.append(b)
                            elif isinstance(b, dict) and b.get("type") in ("input_text", "text"):
                                txt = b.get("text") or ""
                                if txt:
                                    parts.append(txt)
                        last_user_message = " ".join(parts)
                    if last_user_message.strip():
                        break
                elif item_type in ("input_text", "text"):
                    last_user_message = item.get("text", "")
                    if last_user_message.strip():
                        break

    if not last_user_message:
        instructions = body.get("instructions")
        if isinstance(instructions, str):
            last_user_message = instructions

    target_model = client_model
    if client_model in AUTO_MODELS or client_model == "llm-routing-ollama":
        bypass_cache = request.headers.get("x-bypass-cache") == "true"
        (
            target_model,
            triage_latency,
            was_cache_hit,
            raw_classification,
        ) = await classify_request(last_user_message, bypass_cache=bypass_cache)
        logger.info(f"Responses API Triage decision: Routing to -> '{target_model}'")

    body_to_send = body.copy()
    body_to_send["model"] = target_model

    litellm_key = _validate_litellm_master_key()
    headers = {
        "Authorization": f"Bearer {litellm_key}",
        "Content-Type": request.headers.get("content-type", "application/json"),
    }

    is_streaming = body_to_send.get("stream", False)
    client = get_http_client()
    url = f"{LITELLM_URL}/v1/responses"

    logger.info(f"Proxying Responses API request for model={target_model} to {url}")

    if is_streaming:
        req = client.build_request(
            "POST", url, json=body_to_send, headers=headers, timeout=600.0
        )
        resp = await client.send(req, stream=True)
        if resp.status_code != 200:
            error_body = await resp.aread()
            await resp.aclose()
            logger.warning(
                f"Responses API stream failed ({resp.status_code}): {error_body[:300].decode('utf-8', errors='replace')}"
            )
            raise HTTPException(status_code=resp.status_code, detail="Responses proxy failed")

        async def response_streamer():
            try:
                buffer = ""
                seen_args_delta = set()
                seen_args_done = set()
                async for chunk in resp.aiter_bytes():
                    buffer += chunk.decode("utf-8", errors="replace")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line_str = line.strip()
                        if line_str.startswith("data:"):
                            raw_data = line_str[5:].strip()
                            if raw_data and raw_data != "[DONE]":
                                try:
                                    data_obj = orjson.loads(raw_data)
                                    event_type = data_obj.get("type")
                                    if event_type == "response.function_call_arguments.delta":
                                        item_id = data_obj.get("item_id")
                                        if item_id:
                                            seen_args_delta.add(item_id)
                                    elif event_type == "response.function_call_arguments.done":
                                        item_id = data_obj.get("item_id")
                                        if item_id:
                                            seen_args_done.add(item_id)
                                    elif event_type == "response.output_item.done":
                                        item = data_obj.get("item", {})
                                        if item.get("type") == "function_call":
                                            item_id = item.get("id")
                                            args_val = item.get("arguments", "{}") or "{}"
                                            if item_id and item_id not in seen_args_delta:
                                                seen_args_delta.add(item_id)
                                                delta_evt = {
                                                    "type": "response.function_call_arguments.delta",
                                                    "item_id": item_id,
                                                    "delta": args_val,
                                                    "output_index": 0,
                                                    "sequence_number": 0,
                                                }
                                                yield b"data: " + orjson.dumps(delta_evt) + b"\n\n"
                                            if item_id and item_id not in seen_args_done:
                                                seen_args_done.add(item_id)
                                                done_evt = {
                                                    "type": "response.function_call_arguments.done",
                                                    "item_id": item_id,
                                                    "name": item.get("name", ""),
                                                    "arguments": args_val,
                                                    "output_index": 0,
                                                    "sequence_number": 0,
                                                }
                                                yield b"data: " + orjson.dumps(done_evt) + b"\n\n"
                                except Exception as parse_err:
                                    logger.warning(f"Failed to parse SSE line: {parse_err}")
                        yield (line + "\n").encode("utf-8")
                if buffer:
                    yield buffer.encode("utf-8")
            except Exception as stream_err:
                logger.error(f"Error during Responses API streaming proxy: {stream_err}")
                raise
            finally:
                await resp.aclose()

        return StreamingResponse(response_streamer(), media_type="text/event-stream")
    else:
        try:
            r = await client.post(
                url,
                json=body_to_send,
                headers=headers,
                timeout=600.0,
            )
            response_headers = dict(r.headers)
            for h in [
                "content-encoding",
                "content-length",
                "transfer-encoding",
                "connection",
            ]:
                response_headers.pop(h, None)
            return Response(
                content=r.content,
                status_code=r.status_code,
                headers=response_headers,
            )
        except Exception as e:
            logger.error(f"Failed to proxy Responses API request: {e}")
            raise HTTPException(status_code=502, detail="Responses proxy failed")

_last_roster_sync = 0.0
_roster_sync_lock = asyncio.Lock()

async def maybe_trigger_roster_sync(force: bool = False):
    """Opportunistically refresh the OpenRouter roster if ratelimited or after TTL."""
    global _last_roster_sync, free_model_cache
    now = time.monotonic()
    min_interval = 60.0 if force else 300.0
    if now - _last_roster_sync < min_interval:
        return

    if _roster_sync_lock.locked():
        return

    async with _roster_sync_lock:
        if time.monotonic() - _last_roster_sync < min_interval:
            return
        master_key = os.getenv("LITELLM_MASTER_KEY")
        if master_key:
            logger.info(f"Triggering opportunistic roster sync (force={force})")
            _last_roster_sync = time.monotonic()
            await sync_adaptive_router_roster(master_key)
            free_model_cache["data"] = None


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
        "local-qwen",
        "local-qwen-hass",
        "local-qwen-routing",
        "gpt-4o-mini",
        "gpt-4o",
        "openrouter-auto",
        "openrouter-gpt-5.6-luna",
        "openrouter-gpt-5.6-luna-max",
        "gpt-5.6-luna",
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
            parent_obs_kwargs = {
                "trace_context": {"trace_id": langfuse_trace_id},
                "name": f"triage-{client_model}",
                "input": last_user_message[:200],
                "level": "DEFAULT",
                "metadata": {
                    "client_model": client_model,
                    "environment": os.getenv("ENVIRONMENT", "production"),
                },
            }
            if _trace_session_id:
                parent_obs_kwargs["session_id"] = _trace_session_id
            if _trace_user_id:
                parent_obs_kwargs["user_id"] = _trace_user_id
            parent_obs = lf.start_observation(**parent_obs_kwargs)
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
                parent_obs_update_kwargs = {
                    "output": {"tier": target_model, "raw": raw_classification},
                    "metadata": {
                        "triage_latency_ms": round(triage_latency, 2),
                        "cache_hit": was_cache_hit,
                        "total_requests": stats["total_requests"],
                    },
                }
                if _trace_session_id:
                    parent_obs_update_kwargs["session_id"] = _trace_session_id
                if _trace_user_id:
                    parent_obs_update_kwargs["user_id"] = _trace_user_id
                parent_obs.update(**parent_obs_update_kwargs)
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
                from agy_proxy import try_agy_proxy, AgyProxyRequest

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
                    agy_request = AgyProxyRequest(
                        prompt=last_prompt,
                        messages=messages,
                        session_id=session_id,
                        total_timeout=300.0,
                        stream=is_stream_requested,
                        target_tier=target_model,
                        client=get_http_client(),
                        cooldown_persistence=ValkeyCooldownPersistence(),
                    )
                    agy_response = await try_agy_proxy(agy_request)
                    if agy_response:
                        model_name = agy_response.get("model", "gemini-3.5-flash (via agy)")

                        if "stream" in agy_response:
                            # Real native stream generator
                            async def native_agy_stream_generator(stream_gen, model_name):
                                """Asynchronous generator yielding native OpenAI-compatible streaming chunks from the real agy daemon."""
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
                                        yield b"data: " + orjson.dumps(chunk_data) + b"\n\n"

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
                                    yield b"data: " + orjson.dumps(finish_data) + b"\n\n"
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
                                            yield b"data: " + orjson.dumps(chunk_data) + b"\n\n"
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
                                        yield b"data: " + orjson.dumps(finish_data) + b"\n\n"
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
                logger.info(f"Backend '{model_name}' not found in backends mapping, defaulting to LiteLLM proxy")
                backend_conf = {
                    "api_base": f"{LITELLM_URL}/v1",
                    "api_key": "DYNAMIC_LITELLM_MASTER_KEY_PLACEHOLDER",
                }

            backend_api_base = backend_conf["api_base"]
            raw_api_key = backend_conf.get("api_key", "")
            if (
                not raw_api_key
                or raw_api_key in _INVALID_MASTER_KEYS
                or raw_api_key == os.getenv("LITELLM_MASTER_KEY")
                or "PLACEHOLDER" in str(raw_api_key).upper()
            ):
                backend_api_key = _validate_litellm_master_key()
            else:
                backend_api_key = raw_api_key

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
                        "openrouter-gpt-5.6-luna": 1050000,
                        "openrouter-gpt-5.6-luna-max": 1050000,
                        "gpt-5.6-luna": 1050000,
                        "openrouter-auto": 2000000,
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
                                                    data_json = orjson.loads(data_str)
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
                                if hasattr(ex, "status_code") and getattr(ex, "status_code") == 429:
                                    if model_name.startswith("agent-"):
                                        await maybe_trigger_roster_sync(force=True)

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
                        if r.status_code == 429 and model_name.startswith("agent-"):
                            await maybe_trigger_roster_sync(force=True)
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
                        if response.status_code == 429 and model_name.startswith("agent-"):
                            await maybe_trigger_roster_sync(force=True)
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
    await sync_stats_from_valkey()
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
    safe_label = markupsafe.escape(label)
    safe_color = markupsafe.escape(color)
    return f"<span style='font-size: 9px; padding: 2px 7px; border-radius: 4px; background: {safe_color}18; color: {safe_color}; border: 1px solid {safe_color}44; font-weight: 700; letter-spacing: 0.5px; vertical-align: middle; margin-right: 8px;'>{safe_label}</span>"


async def get_dashboard_data():
    """Fetch all metrics and pre-compute HTML snippets for the dashboard."""
    # Run ALL independent I/O concurrently with protective timeouts
    (
        _,  # sync_stats_from_valkey
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
        asyncio.wait_for(sync_stats_from_valkey(), timeout=2.0),
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

    # 4. Generate dynamic conic-gradient CSS background for the Pie Chart
    pie_gradient = get_pie_chart_gradient()
    total_tool_tokens = sum(stats["tool_tokens"].values())
    max_tool_val = max(stats["tool_tokens"].values()) if stats["tool_tokens"] and max(stats["tool_tokens"].values()) > 0 else 1

    tool_tokens = []
    for tool_name, token_count in stats["tool_tokens"].items():
        pct = (token_count / max_tool_val) * 100.0
        overall_pct = (token_count / total_tool_tokens * 100.0) if total_tool_tokens > 0 else 0.0
        color = TOOL_COLORS.get(tool_name, "#94a3b8")
        tool_tokens.append({
            "name": tool_name,
            "count": token_count,
            "pct": pct,
            "overall_pct": overall_pct,
            "color": color
        })

    # 8. Routing Paths pie chart
    routing_paths = stats.get("routing_paths", {"google_oauth_direct": 0, "litellm_fallback": 0})
    total_routed = sum(routing_paths.values())
    routing_pie_gradient = "background: rgba(255, 255, 255, 0.05);"
    routing_data = []
    routing_colors = {"google_oauth_direct": "#fbbf24", "litellm_fallback": "#818cf8"}
    routing_labels = {"google_oauth_direct": "Google OAuth Direct", "litellm_fallback": "LiteLLM Fallback"}

    if total_routed > 0:
        current_angle = 0.0
        route_grad_parts = []
        for rname, rcount in routing_paths.items():
            rpct = (rcount / total_routed) * 100.0
            next_angle = current_angle + rpct
            rcolor = routing_colors.get(rname, "#94a3b8")
            route_grad_parts.append(f"{rcolor} {current_angle:.1f}% {next_angle:.1f}%")
            routing_data.append({
                "name": rname,
                "label": routing_labels.get(rname, rname),
                "count": rcount,
                "pct": rpct,
                "color": rcolor
            })
            current_angle = next_angle
        routing_pie_gradient = f"background: conic-gradient({', '.join(route_grad_parts)});"

    # Persistent aggregated tokens
    p_tokens = stats.get("prompt_tokens", 0)
    c_tokens = stats.get("completion_tokens", 0)
    t_tokens = p_tokens + c_tokens

    # 11. Free Model Roster Table
    roster_path = "/config/router_dir/free_models_roster.json"
    roster_table_html = ""
    try:
        if os.path.exists(roster_path):
            async with aiofiles.open(roster_path, "r", encoding="utf-8") as f:
                roster_content = await f.read()
                roster_data = orjson.loads(roster_content)

            import html as html_lib
            rows = ""
            for m in roster_data.get("models", []):
                mid = m.get("id", "")
                mname = m.get("name", mid)
                escaped_name = html_lib.escape(str(mname))
                escaped_id = html_lib.escape(str(mid))

                active_tiers = []
                for tier, models in _registered_free_models.items():
                    if mid in models:
                        active_tiers.append(tier.replace("agent-", "").replace("-core", ""))

                status_label = f"<span style='color:#34d399;'>Active ({', '.join(active_tiers)})</span>" if active_tiers else "<span style='opacity:0.5;'>Excluded</span>"
                tool_icon = "🛠️" if m.get("has_tools", True) else "❌"
                score_val = m.get("score", 0.0)
                ctx_val = m.get("context_length", 0) // 1000

                rows += f"""
                <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
                    <td style="padding:10px 8px;font-size:12px;font-weight:600;">{escaped_name}<br><span style="font-size:10px;opacity:0.4;font-family:monospace;">{escaped_id}</span></td>
                    <td style="padding:10px 8px;text-align:center;font-weight:bold;color:#fbbf24;">{score_val:.1f}</td>
                    <td style="padding:10px 8px;text-align:center;opacity:0.7;font-size:11px;">{ctx_val}k</td>
                    <td style="padding:10px 8px;text-align:center;">{tool_icon}</td>
                    <td style="padding:10px 8px;text-align:right;font-size:11px;">{status_label}</td>
                </tr>
                """
            roster_table_html = f"""
            <table style="width:100%;border-collapse:collapse;">
                <thead>
                    <tr style="opacity:0.5;font-size:10px;text-transform:uppercase;border-bottom:1px solid rgba(255,255,255,0.1);">
                        <th style="padding:8px;text-align:left;">Model</th>
                        <th style="padding:8px;">Score</th>
                        <th style="padding:8px;">Ctx</th>
                        <th style="padding:8px;">Tools</th>
                        <th style="padding:8px;text-align:right;">Status</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
            """
    except Exception as e:
        roster_table_html = f"<div style='opacity:0.5;padding:10px;'>Error loading roster: {e}</div>"
    return {
        "roster_table_html": roster_table_html,
        "valkey_status": valkey_status,
        "litellm_status": litellm_status,
        "llama_server_status": llama_server_status,
        "langfuse_status": langfuse_status,
        "oauth_status": oauth_status,
        "best_free_model": best_free_model,
        "tier_data": tier_data,
        "pie_gradient": pie_gradient,
        "total_tool_tokens": total_tool_tokens,
        "tool_tokens": tool_tokens,
        "timeline": stats["timeline"],
        "goose_sessions": goose_sessions,
        "routing_pie_gradient": routing_pie_gradient,
        "routing_data": routing_data,
        "p_tokens": p_tokens,
        "c_tokens": c_tokens,
        "t_tokens": t_tokens,
        "llamacpp": llamacpp,
        "llamacpp_build": llamacpp.get("build", "unknown"),
        "avg_triage_latency_ms": stats["avg_triage_latency_ms"],
        "avg_proxy_latency_ms": stats["avg_proxy_latency_ms"],
        "cache_hits": stats["cache_hits"],
        "total_requests": stats["total_requests"],
        "last_triage_decision": stats["last_triage_decision"],
    }


@app.get("/api/dashboard-stats")
async def get_dashboard_stats(request: Request):
    """Return dashboard metrics and pre-computed HTML as JSON for asynchronous UI updates."""
    data = await get_dashboard_data()

    # Render partials using Jinja2
    context = {"request": request, "data": data}
    oauth_banner_html = templates.get_template("partials/oauth_banner.html").render(context)
    tier_table_html = templates.get_template("partials/tier_table.html").render(context)
    pie_legend_html = templates.get_template("partials/pie_legend.html").render(context)
    tool_tokens_html = templates.get_template("partials/tool_tokens.html").render(context)
    routing_legend_html = templates.get_template("partials/routing_legend.html").render(context)
    timeline_html = templates.get_template("partials/timeline.html").render(context)
    goose_html = templates.get_template("partials/goose.html").render(context)
    llamacpp_models_html = templates.get_template("partials/llamacpp_models.html").render(context)
    llamacpp_slots_html = templates.get_template("partials/llamacpp_slots.html").render(context)

    # Return data with the pre-computed HTML for JS
    data["oauth_banner_html"] = oauth_banner_html
    data["tier_table_html"] = tier_table_html
    data["pie_legend_html"] = pie_legend_html
    data["tool_tokens_html"] = tool_tokens_html
    data["routing_legend_html"] = routing_legend_html
    data["timeline_html"] = timeline_html
    data["goose_html"] = goose_html
    data["llamacpp_models_html"] = llamacpp_models_html
    data["llamacpp_slots_html"] = llamacpp_slots_html

    return data


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
    if not isinstance(external_host, str) or not re.match(r"^[a-zA-Z0-9.:-]+$", external_host):
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

    if is_valid_external or is_valid_base:
        # Use configured routing domain if a proxy supplies no request hostname.
        # Preserve an explicit public port from the selected netloc so dashboard
        # links remain valid behind non-standard TLS listeners.
        host_val = external_host if is_valid_external else (request.base_url.hostname or domain)
        netloc_val = external_netloc if is_valid_external else (request.base_url.netloc or host_val)
        parsed_public = urlparse(f"{external_scheme}://{netloc_val}")
        try:
            port_suffix = f":{parsed_public.port}" if parsed_public.port else ""
        except ValueError:
            logger.warning("Invalid public port in netloc %r; omitting port", netloc_val)
            port_suffix = ""

        host_base = re.sub(r"^(?:dashboard|llm-routing)\.", "", host_val)
        host_base = re.sub(r"^(?:litellm|langfuse|llama)\.", "", host_base)
        service_netloc = f"{host_base}{port_suffix}"
        return (
            f"{external_scheme}://langfuse.{service_netloc}",
            f"{external_scheme}://litellm.{service_netloc}/ui/",
            f"{external_scheme}://llama.{service_netloc}/"
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

    # Expose src_badge to the template context
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "data": data,
            "langfuse_url": langfuse_url,
            "litellm_url": litellm_url,
            "llama_url": llama_url,
            "router_port": os.getenv("ROUTER_PORT") or "5000",
            "litellm_port": os.getenv("LITELLM_PORT") or "4000",
            "valkey_port": _valkey_port(),
            "langfuse_port": os.getenv("LANGFUSE_WEB_PORT") or "3001",
            "src_badge": src_badge,
        }
    )



# --- Static files (visualizer, data files) ---
STATIC_DIR = Path(__file__).resolve().parent / "static"
DATA_DIR = Path(__file__).resolve().parent / "data"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
DATA_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/data", StaticFiles(directory=str(DATA_DIR)), name="data")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve the dedicated favicon.ico file for root path request compatibility."""
    fav_path = STATIC_DIR / "favicon.ico"
    if fav_path.exists():
        return FileResponse(fav_path)
    raise HTTPException(status_code=404, detail="Favicon not found")


@app.get("/visualizer", response_class=HTMLResponse)
async def get_visualizer():
    """Serve the dataset visualizer for human review."""
    vis_path = STATIC_DIR / "visualizer.html"
    if vis_path.exists():
        content = await asyncio.to_thread(vis_path.read_text, encoding="utf-8")
        return HTMLResponse(content)
    return HTMLResponse("<h2>Visualizer not found</h2>", status_code=404)


MAX_ANNOTATION_KEY_LENGTH = 128
MAX_ANNOTATION_ITEM_BYTES = 4096

AnnotationTier = Literal[
    0, 1, 2, 3, 4,
    "agent-simple-core",
    "agent-medium-core",
    "agent-complex-core",
    "agent-reasoning-core",
    "agent-advanced-core",
    "?",
]

class AnnotationItem(BaseModel):
    """Pydantic model representing a single human dataset review annotation."""
    model_config = ConfigDict(extra="forbid")

    tier: Optional[AnnotationTier] = None
    note: Optional[str] = Field(default=None, max_length=1000)
    ts: Optional[str] = Field(default=None, max_length=100)

class AnnotationPayload(RootModel):
    """Pydantic model representing a payload of multiple annotations."""
    root: Dict[str, AnnotationItem]

    @model_validator(mode="after")
    def _validate_payload(self) -> "AnnotationPayload":
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

    # Do not swallow OSError if file doesn't exist to preserve original behavior.
    # The caller (save_annotations) handles the exception when reading existing annotations.
    current_mtime = await asyncio.to_thread(os.path.getmtime, path)

    cache_entry = _annotations_cache.get(path)

    if cache_entry is None or current_mtime != cache_entry["mtime"]:
        async with aiofiles.open(path, "rb") as f:
            # Cache the raw bytes rather than a parsed dictionary.
            # Parsing via orjson.loads(bytes) creates a fresh dictionary on every read,
            # which is significantly faster than using copy.deepcopy().
            content = await f.read()
            _annotations_cache[path] = {"mtime": current_mtime, "content": content}
    else:
        content = cache_entry["content"]

    # Parse in a thread pool to avoid blocking the event loop
    return await asyncio.to_thread(orjson.loads, content)


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
