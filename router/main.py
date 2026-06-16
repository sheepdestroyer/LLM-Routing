import os
import sys
import json
import time
import socket
import asyncio
import logging
import yaml
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from circuit_breaker import get_breaker

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
                host=os.getenv("LANGFUSE_HOST", "http://127.0.0.1:3001"),
                release="llm-triage-router-v1",
            )
            logger.info("Langfuse client initialized")
        except Exception as e:
            logger.warning(f"Langfuse client initialization failed: {e} — traces disabled")
            _langfuse_client = False  # sentinel to avoid retry
    return _langfuse_client if _langfuse_client is not False else None

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
                {"name": "simple_ratio_pct", "value": stats.get("simple_requests", 0) / total * 100},
                {"name": "medium_ratio_pct", "value": stats.get("medium_requests", 0) / total * 100},
                {"name": "complex_ratio_pct", "value": stats.get("complex_requests", 0) / total * 100},
                {"name": "reasoning_ratio_pct", "value": stats.get("reasoning_requests", 0) / total * 100},
                {"name": "advanced_ratio_pct", "value": stats.get("advanced_requests", 0) / total * 100},
                {"name": "cache_hit_rate_pct", "value": stats["cache_hits"] / total * 100},
                {"name": "avg_triage_latency_ms", "value": stats["avg_triage_latency_ms"]},
                {"name": "avg_proxy_latency_ms", "value": stats["avg_proxy_latency_ms"]},
                {"name": "total_requests", "value": float(total)},
                {"name": "circuit_breaker_google_tier", "value": float(router.google.tier)},
                {"name": "circuit_breaker_vendor_tier", "value": float(router.vendor.tier)},
                {"name": "google_oauth_direct_ratio_pct", "value": stats["routing_paths"]["google_oauth_direct"] / total * 100},
            ]
            for s in scores:
                lf.create_score(name=s["name"], value=s["value"])
            lf.flush()
            logger.info(f"Pushed {len(scores)} aggregate scores to Langfuse")
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
router_api_base = router_model_conf.get("api_base", "http://127.0.0.1:8080/v1")
router_api_key = router_model_conf.get("api_key", "local-token")
router_model_name = router_model_conf.get("model", "qwen-0.8b-routing")

system_prompt = config.get("classification_rules", {}).get("system_prompt", "")
backends = {b["name"]: b for b in config.get("backends", [])}

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
    "tool_tokens": {
        "tree": 0,
        "shell": 0,
        "write": 0,
        "view": 0,
        "other": 0
    },
    "routing_paths": {
        "google_oauth_direct": 0,
        "litellm_fallback": 0
    },
    "timeline": []
}

STATS_JSON_PATH = "/config/router_dir/router_stats.json"

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
            timeline_path = os.path.join(os.path.dirname(CONFIG_PATH), "router_timeline.json")
            if os.path.exists(timeline_path):
                try:
                    with open(timeline_path, "r") as f:
                        stats["timeline"] = json.load(f)
                except Exception:
                    pass  # stale/broken timeline file → start fresh
        except Exception as e:
            logger.error(f"Failed to load persisted stats: {e}")

def save_persisted_stats():
    """Persists current statistics in-memory structure to disk securely."""
    try:
        os.makedirs(os.path.dirname(STATS_JSON_PATH), exist_ok=True)
        with open(STATS_JSON_PATH, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to persist stats to disk: {e}")

# Load initial stats from persistent storage
load_persisted_stats()

# Triage Decision Cache (In-Memory dictionary mapping normalized prompt -> (classification, timestamp))
triage_cache = {}
CACHE_TTL_SECONDS = 86400  # Decisions cached for 24 hours
classification_lock = asyncio.Lock()

async def sync_adaptive_router_roster(master_key: str):
    """Fetch free OpenRouter models and register them as deployments in LiteLLM."""
    if not master_key:
        logger.warning("No LITELLM_MASTER_KEY — skipping roster sync")
        return
    headers = {"Authorization": f"Bearer {master_key}", "Content-Type": "application/json"}
    admin_url = "http://127.0.0.1:4000"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("https://openrouter.ai/api/v1/models")
            if r.status_code != 200:
                logger.warning(f"OpenRouter models API returned {r.status_code}")
                return
            all_models = r.json().get("data", [])
    except Exception as e:
        logger.warning(f"Failed to fetch OpenRouter models: {e}")
        return
    free_models = []
    for m in all_models:
        mid = m.get("id", "")
        # Skip internal OpenRouter encoded IDs that LiteLLM can't map to a provider
        if not mid or (len(mid) > 64 and "/" not in mid):
            continue

        # 1. Enforce Tool/Function Calling Support
        supported_params = m.get("supported_parameters", [])
        if "tools" not in supported_params:
            logger.info(f"🚫 Skipping {mid} — Model does not support tool calling.")
            continue

        pricing = m.get("pricing", {})
        if pricing.get("prompt") in ("0", 0, "0.0", 0.0) and pricing.get("completion") in ("0", 0, "0.0", 0.0):
            try:
                score = compute_free_model_score(m)
            except Exception:
                score = 25.0  # conservative fallback for unparseable models
            free_models.append((score, mid))
    free_models.sort(reverse=True)
    if not free_models:
        logger.warning("No free models found — skipping roster sync")
        return
    tier_assignments = {
        "agent-simple-core": [], "agent-medium-core": [],
        "agent-complex-core": [], "agent-reasoning-core": [],
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
        return (s / max_score) * 100.0
    for score, mid in free_models:  # include all models — top 2 are also assigned to their correct tier
        n = norm(score)
        if n >= 80: tier_assignments["agent-advanced-core"].append(mid)
        elif n >= 75: tier_assignments["agent-reasoning-core"].append(mid)
        elif n >= 68: tier_assignments["agent-complex-core"].append(mid)
        elif n >= 60: tier_assignments["agent-medium-core"].append(mid)
        else: tier_assignments["agent-simple-core"].append(mid)
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
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Purge all existing agent-* deployments before re-registering.
        # Without this, every roster sync accumulates stale deployments (4,591+
        # in 24h), bloating the DB and slowing LiteLLM startup. Each sync now
        # starts clean — delete all, then register only the current roster.
        try:
            db_url = os.getenv("DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/postgres")
            import asyncpg
            conn = await asyncpg.connect(db_url)
            await conn.execute(
                'DELETE FROM "LiteLLM_ProxyModelTable" WHERE model_name LIKE $1',
                'agent-%'
            )
            await conn.close()
            logger.info("🧹 Purged stale agent-* deployments before roster sync")
        except Exception as e:
            logger.warning(f"Failed to purge stale deployments (non-fatal): {e}")

        registered = 0
        failed = 0
        for tier_name, model_ids in tier_assignments.items():
            for mid in model_ids:
                payload = {
                    "model_name": tier_name,
                    "litellm_params": {"model": f"openrouter/{mid}", "request_timeout": 120}
                }
                try:
                    r = await client.post(f"{admin_url}/model/new", headers=headers, json=payload)
                    if r.status_code in (200, 201):
                        registered += 1
                    else:
                        failed += 1
                        logger.warning(f"model/new {mid} → {tier_name}: HTTP {r.status_code} — {r.text[:200]}")
                except Exception as e:
                    failed += 1
                    logger.warning(f"Failed to register {mid} under {tier_name}: {e}")
        logger.info(f"📊 Roster sync: registered {registered} deployments ({failed} failed) across 5 tiers — {sum(len(v) for v in tier_assignments.values())} attempted")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: wait for LiteLLM readiness, then sync free-model roster."""
    litellm_ready_url = "http://127.0.0.1:4000/health/readiness"
    litellm_master_key = os.getenv("LITELLM_MASTER_KEY", "")
    max_wait = 180
    logger.info(f"⏳ Waiting for LiteLLM on :4000 (max {max_wait}s)...")
    for i in range(max_wait):
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(litellm_ready_url)
                if r.status_code == 200:
                    logger.info(f"✅ LiteLLM ready after {i+1}s")
                    break
        except Exception:
            pass
        await asyncio.sleep(1)
    else:
        logger.warning("⚠️  LiteLLM not ready within timeout — proceeding without roster sync")
        yield
        return
    # Sync free-model roster into LiteLLM (separate try so sync failure doesn't loop)
    try:
        await sync_adaptive_router_roster(litellm_master_key)
    except Exception as e:
        logger.error(f"Roster sync failed: {e}")
    yield
    # Start aggregate score-push background task (runs for server lifetime)
    asyncio.create_task(push_aggregate_scores())

app = FastAPI(title="LLM Triage Router", lifespan=lifespan)

async def check_tcp_port(ip: str, port: int) -> bool:
    """Verifies if a TCP port is open locally."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        result = sock.connect_ex((ip, port))
        sock.close()
        return result == 0
    except Exception:
        return False

async def check_http_endpoint(url: str) -> bool:
    """Verifies if an HTTP endpoint is responsive."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(url)
            return r.status_code < 500
    except Exception:
        return False

async def classify_request(prompt: str, bypass_cache: bool = False) -> tuple[str, float, bool, str]:
    """Queries the local fast Qwen instance to classify request complexity with TTL caching."""
    global triage_cache, stats
    
    # Normalize the prompt text for cache mapping
    normalized_prompt = prompt.strip().lower()
    
    # 1. Check in-memory TTL cache (outside lock)
    if not bypass_cache and normalized_prompt in triage_cache:
        cached_decision, cached_time = triage_cache[normalized_prompt]
        if time.time() - cached_time < CACHE_TTL_SECONDS:
            logger.info(f"⚡ Triage Cache Hit for prompt: '{normalized_prompt[:50]}...' -> routed to '{cached_decision}'")
            stats["cache_hits"] = stats.get("cache_hits", 0) + 1
            save_persisted_stats()
            return cached_decision, 0.0, True, cached_decision  # was_cache_hit=True
            
    start_time = time.time()
    
    # 2. Query llama-server sequentially using a lock to prevent concurrent slot conflicts
    async with classification_lock:
        # Check cache again just in case a concurrent request finished and cached it while we waited
        if not bypass_cache and normalized_prompt in triage_cache:
            cached_decision, cached_time = triage_cache[normalized_prompt]
            if time.time() - cached_time < CACHE_TTL_SECONDS:
                logger.info(f"⚡ Triage Cache Hit (post-queue) for prompt: '{normalized_prompt[:50]}...' -> routed to '{cached_decision}'")
                stats["cache_hits"] = stats.get("cache_hits", 0) + 1
                save_persisted_stats()
                return cached_decision, 0.0, True, cached_decision
                
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                payload = {
                    "model": router_model_name,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.0,
                    "max_tokens": 15,
                    "grammar": 'root ::= "agent-simple-core" | "agent-medium-core" | "agent-complex-core" | "agent-reasoning-core" | "agent-advanced-core"'
                }
                headers = {"Authorization": f"Bearer {router_api_key}"}
                
                logger.info(f"Classifying intent via {router_api_base} using model {router_model_name}...")
                response = await client.post(
                    f"{router_api_base}/chat/completions",
                    json=payload,
                    headers=headers
                )
                
                latency = (time.time() - start_time) * 1000.0
                
                if response.status_code != 200:
                    logger.error(f"Classification failed with status {response.status_code}: {response.text}")
                    return "agent-advanced-core", latency, False, "advanced (fallback)"
                    
                result = response.json()
                message_obj = result["choices"][0]["message"]
                content = message_obj.get("content") or ""
                content_clean = content.strip()
                raw_result = content_clean if content_clean else "advanced (empty)"
                logger.info(f"Raw classifier response: '{raw_result}'")
                
                # 5-tier grammar parsing (was 3-tier, missed medium + advanced)
                valid_tiers = {
                    "agent-simple-core", "agent-medium-core", "agent-complex-core",
                    "agent-reasoning-core", "agent-advanced-core"
                }
                if content_clean in valid_tiers:
                    decision = content_clean
                else:
                    decision = "agent-advanced-core"
                    
                # Store in cache
                triage_cache[normalized_prompt] = (decision, time.time())
                return decision, latency, False, raw_result
                    
        except Exception as e:
            latency = (time.time() - start_time) * 1000.0
            logger.error(f"Exception during classification: {e}")
            return "agent-advanced-core", latency, False, "advanced (exception)"

def get_live_gemini_oauth_token() -> str | None:
    try:
        creds_path = "/config/gemini_auth/oauth_creds.json"
        if os.path.exists(creds_path):
            with open(creds_path, "r") as f:
                data = json.load(f)
                access_token = data.get("access_token")
                expiry_ms = data.get("expiry_date", 0)
                # Convert current time to milliseconds
                current_ms = int(time.time() * 1000)
                if access_token and current_ms < expiry_ms:
                    logger.info("🔑 Found valid, unexpired Gemini OAuth token from host!")
                    return access_token
                else:
                    # agy CLI uses the OS system keyring (GNOME Keyring), not this
                    # stale disk file. The file being expired is expected — don't warn.
                    logger.debug("Gemini OAuth token on disk is expired — agy uses system keyring instead.")
    except Exception as e:
        logger.error(f"Failed to read live OAuth token: {e}")
    return None

def get_gemini_oauth_status() -> dict:
    """Returns structured OAuth status for the dashboard banner."""
    creds_path = "/config/gemini_auth/oauth_creds.json"
    try:
        if not os.path.exists(creds_path):
            return {"status": "missing", "detail": "No oauth_creds.json found", "expiry_ms": 0}
        with open(creds_path, "r") as f:
            data = json.load(f)
        access_token = data.get("access_token")
        expiry_ms = data.get("expiry_date", 0)
        current_ms = int(time.time() * 1000)
        if not access_token:
            return {"status": "missing", "detail": "No access token in file", "expiry_ms": 0}
        diff_sec = (expiry_ms - current_ms) / 1000.0
        if diff_sec > 0:
            # Token is valid — compute human-readable remaining time
            if diff_sec < 60:
                remaining = f"{int(diff_sec)}s"
            elif diff_sec < 3600:
                remaining = f"{int(diff_sec // 60)}m {int(diff_sec % 60)}s"
            else:
                remaining = f"{int(diff_sec // 3600)}h {int((diff_sec % 3600) // 60)}m"
            return {"status": "valid", "detail": f"Expires in {remaining}", "expiry_ms": expiry_ms}
        else:
            # Token is expired — compute human-readable elapsed time
            elapsed = abs(diff_sec)
            if elapsed < 3600:
                ago = f"{int(elapsed // 60)} minutes ago"
            elif elapsed < 86400:
                ago = f"{int(elapsed // 3600)} hours ago"
            else:
                ago = f"{int(elapsed // 86400)} days ago"
            return {"status": "expired", "detail": f"Expired {ago}", "expiry_ms": expiry_ms}
    except Exception as e:
        return {"status": "error", "detail": str(e), "expiry_ms": 0}

def map_tool_to_category(tool_name: str) -> str:
    """Groups low-level developer tool names into the five high-level dashboard metrics."""
    name = tool_name.lower().strip()
    if "__" in name:
        name = name.split("__")[-1]
    
    if "tree" in name or "list_dir" in name or "list-dir" in name:
        return "tree"
    elif "shell" in name or "command" in name or "cmd" in name or "execute" in name or "run" in name:
        return "shell"
    elif "write" in name or "edit" in name or "create" in name or "patch" in name or "replace" in name or "save" in name:
        return "write"
    elif "view" in name or "read" in name or "cat" in name or "grep" in name or "search" in name or "find" in name:
        return "view"
    return "other"

def detect_active_tool(body: dict) -> str:
    """Inspects request payload messages to identify which developer tool is currently being invoked."""
    messages = body.get("messages", [])
    
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        role = msg.get("role")
        if role in ("tool", "function"):
            name = msg.get("name")
            if not name:
                # Look backwards for the assistant tool request that holds the matching id
                tool_call_id = msg.get("tool_call_id")
                if tool_call_id:
                    for prev_msg in reversed(messages[:idx]):
                        if prev_msg.get("role") == "assistant":
                            tcalls = prev_msg.get("tool_calls") or []
                            for tc in tcalls:
                                if tc.get("id") == tool_call_id:
                                    name = tc.get("function", {}).get("name")
                                    break
                        if name:
                            break
            name = name or "other"
            return map_tool_to_category(name)
            
        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls and isinstance(tool_calls, list):
                for tc in tool_calls:
                    name = tc.get("function", {}).get("name") or "other"
                    return map_tool_to_category(name)
                    
    # Fallback to keyphrase scanning in the user message
    for msg in reversed(messages):
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

def record_tool_usage(tool_name: str, prompt_tokens: int, completion_tokens: int, model: str, latency_ms: float, route: str = "litellm_fallback"):
    """Accumulates token counts in memory for active tools and tracks request timelines."""
    if tool_name == "none":
        tool_name = "other"
    
    total = prompt_tokens + completion_tokens
    stats["tool_tokens"][tool_name] = stats["tool_tokens"].get(tool_name, 0) + total
    
    # Save global prompt/completion metrics
    stats["prompt_tokens"] = stats.get("prompt_tokens", 0) + prompt_tokens
    stats["completion_tokens"] = stats.get("completion_tokens", 0) + completion_tokens
    
    # Track routing path distribution
    if "routing_paths" not in stats:
        stats["routing_paths"] = {"google_oauth_direct": 0, "litellm_fallback": 0}
    stats["routing_paths"][route] = stats["routing_paths"].get(route, 0) + 1
    
    # Append to timeline event stack (in-memory ring buffer + persistent disk backup)
    event = {
        "timestamp": time.strftime("%H:%M:%S"),
        "tool": tool_name,
        "model": model,
        "route": route,
        "tokens": total,
        "latency_ms": int(latency_ms)
    }
    stats["timeline"].append(event)
    if len(stats["timeline"]) > 15:
        stats["timeline"].pop(0)
    save_persisted_stats()
    try:
        timeline_path = os.path.join(os.path.dirname(CONFIG_PATH), "router_timeline.json")
        with open(timeline_path, "w") as f:
            json.dump(stats["timeline"], f)
    except Exception:
        pass  # disk persistence failure is non-fatal

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
        async with httpx.AsyncClient(timeout=3.0) as client:
            # Fetch model list
            r = await client.get("http://127.0.0.1:8080/v1/models")
            if r.status_code == 200:
                data = r.json()
                for m in data.get("data", []):
                    meta = m.get("meta", {})
                    status_obj = m.get("status", {})
                    result["models"].append({
                        "id": m.get("id", "?"),
                        "status": status_obj.get("value", "unknown"),
                        "n_params": meta.get("n_params"),
                        "n_ctx": meta.get("n_ctx"),
                        "size_bytes": meta.get("size"),
                        "n_embd": meta.get("n_embd"),
                    })
            # Fetch props for build info
            r2 = await client.get("http://127.0.0.1:8080/props")
            if r2.status_code == 200:
                props = r2.json()
                result["build"] = props.get("build_info", "unknown")
            # Fetch slots for the loaded model, falling back to the first available model if all are unloaded
            loaded = [m["id"] for m in result["models"] if m["status"] == "loaded"]
            slot_model = loaded[0] if loaded else (result["models"][0]["id"] if result["models"] else None)
            if slot_model:
                r3 = await client.get(f"http://127.0.0.1:8080/slots?model={slot_model}")
                if r3.status_code == 200:
                    slots_data = r3.json()
                    for s in slots_data:
                        next_tok = s.get("next_token", [{}])
                        decoded = next_tok[0].get("n_decoded", 0) if next_tok else 0
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
free_model_cache = {
    "data": None,
    "last_fetched": 0.0
}
FREE_MODEL_CACHE_TTL = 3600  # Refresh cache every 1 hour

# --- Artificial Analysis Agentic Index scores cache ---
_AA_SCORES_CACHE: dict[str, float] = {}
_AA_SCORES_LOADED = False

def _load_aa_scores():
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
            logger.info(f"📊 Loaded {len(_AA_SCORES_CACHE)} AA agentic index scores from {scores_path}")
    except Exception as e:
        logger.warning(f"Could not load AA scores cache: {e}")
        _AA_SCORES_LOADED = True  # don't retry

def compute_free_model_score(m: dict) -> float:
    """Return AA agentic index score, or a low default for unknown models."""
    _load_aa_scores()
    mid = m.get("id", "")
    return _AA_SCORES_CACHE.get(mid, 25.0)

def _save_free_models_roster(free_models: list[dict]) -> None:
    """Persist the full sorted free model list so Ralph can try alternatives."""
    import json as _json
    import datetime as _dt
    payload = {
        "models": free_models,
        "updated_at": _dt.datetime.utcnow().isoformat() + "Z",
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
    payload = {**best_model, "updated_at": _dt.datetime.utcnow().isoformat() + "Z"}
    try:
        with open("/config/router_dir/best_free_model.json", "w") as f:
            _json.dump(payload, f, indent=2)
    except Exception:
        pass  # Non-critical — Ralph falls back gracefully


async def get_best_free_model() -> dict:
    """Fetches currently free models from OpenRouter, matches against agentic scores, and returns the highest."""
    global free_model_cache
    now = time.time()
    
    # Check if cache is still valid
    if free_model_cache["data"] and (now - free_model_cache["last_fetched"] < FREE_MODEL_CACHE_TTL):
        _save_best_model_to_disk(free_model_cache["data"])
        return free_model_cache["data"]
        
    fallback_best = {
        "id": "moonshotai/kimi-k2.6:free",
        "name": "MoonshotAI: Kimi K2.6 (free)",
        "score": 82.5,
        "context_length": 131072,
        "is_fallback": True
    }
    
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get("https://openrouter.ai/api/v1/models")
            if r.status_code == 200:
                data = r.json().get("data", [])
                best_model = None
                max_score = -1.0
                all_free = []
                
                for m in data:
                    mid = m.get("id", "")
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
                _save_free_models_roster(all_free)
                if best_model:
                    free_model_cache["data"] = best_model
                    free_model_cache["last_fetched"] = now
                    logger.info(f"🏆 Top free agentic model resolved: {best_model['id']} with score {best_model['score']}")
                    _save_best_model_to_disk(best_model)
                    return best_model
    except Exception as e:
        logger.warning(f"Failed to query live OpenRouter models API for Agentic Index: {e}")
    
    _save_best_model_to_disk(fallback_best)
    return fallback_best

def get_pie_chart_gradient() -> str:
    """Computes a CSS conic-gradient representing the dynamic token distribution across developer tools."""
    total_tokens = sum(stats["tool_tokens"].values())
    if total_tokens == 0:
        return "background: rgba(255, 255, 255, 0.05);"
    
    current_angle = 0.0
    gradient_parts = []
    
    tool_colors = {
        "tree": "#34d399",   # Green
        "shell": "#fbbf24",  # Amber
        "write": "#a78bfa",  # Violet
        "view": "#60a5fa",   # Blue
        "other": "#f472b6"   # Pink
    }
    
    for tool, tokens in stats["tool_tokens"].items():
        if tokens > 0:
            pct = (tokens / total_tokens) * 100.0
            next_angle = current_angle + pct
            color = tool_colors.get(tool, "#94a3b8")
            gradient_parts.append(f"{color} {current_angle:.1f}% {next_angle:.1f}%")
            current_angle = next_angle
            
    if not gradient_parts:
        return "background: rgba(255, 255, 255, 0.05);"
        
    return f"background: conic-gradient({', '.join(gradient_parts)});"

@app.api_route("/v1/memory{path:path}", methods=["GET", "POST", "DELETE", "PUT"])
async def proxy_memory(request: Request, path: str = ""):
    """Proxies memory API calls to the LiteLLM gateway on port 4000."""
    litellm_base = "http://127.0.0.1:4000/v1/memory"
    
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
        "Content-Type": request.headers.get("content-type", "application/json")
    }
    
    logger.info(f"Proxying memory request: {request.method} {url} with params {query_params}")
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.request(
                method=request.method,
                url=url,
                params=query_params,
                content=body,
                headers=headers
            )
            
            # Return response matching status and headers
            response_headers = dict(r.headers)
            # Exclude standard headers that FastAPI/uvicorn will manage
            for h in ["content-encoding", "content-length", "transfer-encoding", "connection"]:
                response_headers.pop(h, None)
                
            return Response(
                content=r.content,
                status_code=r.status_code,
                headers=response_headers
            )
    except Exception as e:
        logger.error(f"Failed to proxy memory request: {e}")
        raise HTTPException(status_code=502, detail=f"Memory proxy failed: {e}")

@app.get("/v1/models")
async def proxy_models():
    """Proxy /v1/models to LiteLLM, injecting llm-routing-auto-free as the first entry."""
    litellm_key = os.getenv("LITELLM_MASTER_KEY")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            auth_header = "Bearer " + (litellm_key or "")
            r = await client.get(
                "http://127.0.0.1:4000/v1/models",
                headers={"Authorization": auth_header}
            )
            data = r.json()
            # Inject llm-routing-* models at the top of the list.
            # Auto models (classifier pipeline) first, then direct models.
            # context_length: 262144 (256K) — the downstream models (gemini-3.5-flash,
            # claude-opus-4.6, deepseek-v4-pro/flash) all support 256K+ context.
            routing_models = [
                {"id": "llm-routing-auto-free",         "object": "model", "created": 0, "owned_by": "llm-routing", "context_length": 262144},
                {"id": "llm-routing-auto-agy",          "object": "model", "created": 0, "owned_by": "llm-routing", "context_length": 262144},
                {"id": "llm-routing-auto-ollama",       "object": "model", "created": 0, "owned_by": "llm-routing", "context_length": 262144},
                {"id": "llm-routing-auto-agy-ollama",   "object": "model", "created": 0, "owned_by": "llm-routing", "context_length": 262144},
                {"id": "llm-routing-agy",               "object": "model", "created": 0, "owned_by": "llm-routing", "context_length": 262144},
                {"id": "llm-routing-ollama",            "object": "model", "created": 0, "owned_by": "llm-routing", "context_length": 262144},
            ]
            for entry in reversed(routing_models):
                data["data"].insert(0, entry)
            return JSONResponse(content=data, status_code=r.status_code)
    except Exception as e:
        logger.error(f"Failed to proxy /v1/models: {e}")
        raise HTTPException(status_code=502, detail=f"Model proxy failed: {e}")

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    global stats
    start_time = time.time()
    
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="Empty messages list")

    # Detect current active developer tool from request body
    active_tool = detect_active_tool(body)

    # Extract last user message for complexity triage
    last_user_message = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_user_message = msg.get("content", "")
            break

    # Known tier names that can be routed directly (bypass classifier)
    DIRECT_TIERS = {
        "agent-simple-core", "agent-medium-core",
        "agent-complex-core", "agent-reasoning-core",
        "agent-advanced-core",
        "llm-routing-agy", "llm-routing-ollama",
    }

    AUTO_MODELS = {
        "llm-routing-auto-free", "llm-routing-auto-agy",
        "llm-routing-auto-ollama", "llm-routing-auto-agy-ollama",
    }

    client_model = body.get("model", "llm-routing-auto-free")

    if client_model in AUTO_MODELS:
        # Full pipeline: classify → route to best tier
        bypass_cache = request.headers.get("x-bypass-cache") == "true"
        target_model, triage_latency, was_cache_hit, raw_classification = await classify_request(
            last_user_message, bypass_cache=bypass_cache
        )
        logger.info(f"Triage decision (auto): Routing to -> '{target_model}'")
    elif client_model in DIRECT_TIERS:
        # Direct routing: client knows what tier they want, skip classifier
        target_model = client_model
        triage_latency = 0.0
        was_cache_hit = False
        raw_classification = f"direct ({client_model})"
        logger.info(f"Direct routing: Client requested '{client_model}', skipping classifier")
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model '{client_model}'. Use 'llm-routing-auto-free' for automatic routing, "
                    f"or one of: {', '.join(sorted(DIRECT_TIERS))}"
        )

    # Update in-memory statistics
    stats["total_requests"] += 1
    stats["last_triage_decision"] = target_model
    stats["total_triage_time_ms"] += triage_latency
    stats["avg_triage_latency_ms"] = stats["total_triage_time_ms"] / stats["total_requests"]
    
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
    save_persisted_stats()

    # Push classification trace to Langfuse (v4 API: start_observation)
    langfuse_trace_id = None
    lf = get_langfuse()
    if lf:
        try:
            # Create a trace ID first, then start an observation with it
            trace_id = lf.create_trace_id(seed=f"triage_{stats['total_requests']}")
            lf.start_observation(
                trace_context={"trace_id": trace_id},
                name=f"triage-{target_model}",
                input=last_user_message[:200],
                output={"tier": target_model, "raw": raw_classification},
                metadata={
                    "triage_latency_ms": round(triage_latency, 2),
                    "cache_hit": was_cache_hit,
                    "total_requests": stats["total_requests"],
                },
                level="DEFAULT",
            )
            lf.flush()
            langfuse_trace_id = trace_id
        except Exception as e:
            logger.warning(f"Langfuse trace push failed (non-fatal): {e}")

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
        or (client_model in AUTO_MODELS and target_model in ("agent-advanced-core", "agent-reasoning-core"))
    )
    should_try_ollama = (
        client_model == "llm-routing-ollama"  # direct — always try
        or (client_model in AUTO_MODELS and target_model in ("agent-advanced-core", "agent-reasoning-core"))
    )

    # --- AGY PROXY ---
    if should_try_agy:
        try:
            from agy_proxy import try_agy_proxy

            last_prompt = ""
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    last_prompt = msg.get("content", "")
                    break

            session_id = None
            if len(messages) >= 2:
                import hashlib
                fingerprint_parts = []
                for msg in messages[:4]:
                    c = msg.get("content", "") or ""
                    if c:
                        fingerprint_parts.append(c[:200])
                fingerprint = "|".join(fingerprint_parts)
                session_id = hashlib.md5(fingerprint.encode()).hexdigest()

            if last_prompt:
                agy_response = await try_agy_proxy(
                    prompt=last_prompt,
                    messages=messages,
                    session_id=session_id,
                    total_timeout=300.0,
                    target_tier=target_model
                )
                if agy_response:
                    latency_ms = (time.time() - start_time) * 1000.0
                    model_name = agy_response.get("model", "gemini-3.5-flash (via agy)")
                    usage = agy_response.get("usage", {})
                    prompt_tokens = usage.get("prompt_tokens", 0)
                    completion_tokens = usage.get("completion_tokens", 0)
                    record_tool_usage(
                        active_tool, prompt_tokens, completion_tokens,
                        model_name, latency_ms, route="google_oauth_direct"
                    )
                    logger.info(f"✅ agy proxy succeeded: {model_name}, {latency_ms:.0f}ms")

                    if body.get("stream", False):
                        content = agy_response.get("choices", [{}])[0].get("message", {}).get("content", "")
                        async def agy_stream_generator():
                            import uuid
                            created_time = int(time.time())
                            chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                            chunk_size = 40
                            for i in range(0, len(content), chunk_size):
                                chunk_text = content[i:i+chunk_size]
                                chunk_data = {
                                    "id": chunk_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_time,
                                    "model": model_name,
                                    "choices": [{
                                        "index": 0,
                                        "delta": {"content": chunk_text},
                                        "finish_reason": None
                                    }]
                                }
                                yield f"data: {json.dumps(chunk_data)}\n\n".encode("utf-8")
                                await asyncio.sleep(0.005)

                            finish_data = {
                                "id": chunk_id,
                                "object": "chat.completion.chunk",
                                "created": created_time,
                                "model": model_name,
                                "choices": [{
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": "stop"
                                }]
                            }
                            yield f"data: {json.dumps(finish_data)}\n\n".encode("utf-8")
                            yield b"data: [DONE]\n\n"
                        return StreamingResponse(agy_stream_generator(), media_type="text/event-stream")
                    else:
                        return agy_response
        except ImportError:
            logger.warning("agy_proxy module not available, falling back to LiteLLM")
        except Exception as e:
            logger.error(f"agy proxy failed: {e}, falling back to LiteLLM")

    # --- OLLAMA (via LiteLLM) ---
    # LiteLLM's ollama_chat provider handles the native Ollama API call.
    # We just proxy to LiteLLM with the appropriate model name.
    # Reasoning tier → deepseek-v4-flash (lighter, faster)
    # Advanced tier → deepseek-v4-pro (full power)
    # LiteLLM's fallback chain handles failures.
    if should_try_ollama:
        if target_model == "agent-reasoning-core":
            target_model = "ollama-deepseek-v4-flash"
        else:
            target_model = "ollama-deepseek-v4-pro"
        logger.info(f"Ollama route: proxying to LiteLLM as model={target_model}")

    # Resolve backend connection parameters
    backend_conf = backends.get(target_model)
    if not backend_conf:
        logger.error(f"Backend '{target_model}' not found in configuration backends.")
        raise HTTPException(status_code=500, detail=f"Backend {target_model} misconfigured")

    backend_api_base = backend_conf["api_base"]
    backend_api_key = backend_conf["api_key"]
    if backend_api_key == "DYNAMIC_LITELLM_MASTER_KEY_PLACEHOLDER":
        backend_api_key = os.getenv("LITELLM_MASTER_KEY", backend_api_key)

    # Delegate to LiteLLM which handles internal fallback chain
    # Router sends model=agent-complex-core (or agent-simple-core)
    # LiteLLM maps this to Nemotron → Kimi → GPT-OSS → local Qwen
    logger.info(f"Proxying to LiteLLM as model={target_model}")

    # Set up outgoing proxy request
    client = httpx.AsyncClient(timeout=3600.0)
    headers = {"Authorization": f"Bearer {backend_api_key}"}
    if langfuse_trace_id:
        headers["X-Langfuse-Trace-Id"] = langfuse_trace_id

    # Handle streaming vs non-streaming proxying (LiteLLM handles fallback internally)
    proxy_start = time.time()
    model_name = target_model  # LiteLLM handles fallback internally
    
    try:
        body_to_send = body.copy()
        body_to_send["model"] = model_name
        if "metadata" not in body_to_send or not isinstance(body_to_send["metadata"], dict):
            body_to_send["metadata"] = {}
        body_to_send["metadata"]["trace_name"] = "agent-completion"
        
        if body.get("stream", False):
            logger.info(f"Proxying streaming to LiteLLM as model={model_name}")
            req = client.build_request("POST", f"{backend_api_base}/chat/completions", json=body_to_send, headers=headers)
            r = await client.send(req, stream=True)
            if r.status_code == 200:
                async def stream_generator():
                    completion_chars = 0
                    request_tokens = len(json.dumps(body_to_send)) // 4
                    try:
                        async for chunk in r.aiter_bytes():
                            completion_chars += len(chunk)
                            yield chunk
                        proxy_latency = (time.time() - proxy_start) * 1000.0
                        stats["total_proxy_time_ms"] += proxy_latency
                        stats["avg_proxy_latency_ms"] = stats["total_proxy_time_ms"] / stats["total_requests"]
                        record_tool_usage(active_tool, request_tokens, completion_chars // 4, model_name, proxy_latency, route="litellm_fallback")
                    except Exception as ex:
                        logger.error(f"Stream error: {ex}")
                    finally:
                        await r.aclose()
                        await client.aclose()
                return StreamingResponse(stream_generator(), media_type="text/event-stream")
            else:
                error_body = await r.aread() if r else b""
                logger.warning(f"LiteLLM stream failed ({r.status_code}): {error_body[:300]}")
                await r.aclose(); await client.aclose()
                raise HTTPException(status_code=502, detail=f"LiteLLM failed: {r.status_code}")
        else:
            logger.info(f"Proxying to LiteLLM as model={model_name}")
            response = await client.post(f"{backend_api_base}/chat/completions", json=body_to_send, headers=headers)
            await client.aclose()
            if response.status_code == 200:
                proxy_latency = (time.time() - proxy_start) * 1000.0
                stats["total_proxy_time_ms"] += proxy_latency
                stats["avg_proxy_latency_ms"] = stats["total_proxy_time_ms"] / stats["total_requests"]
                resp_json = response.json()
                usage = resp_json.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens", len(json.dumps(body_to_send)) // 4)
                completion_tokens = usage.get("completion_tokens", len(json.dumps(resp_json)) // 4)
                record_tool_usage(active_tool, prompt_tokens, completion_tokens, model_name, proxy_latency, route="litellm_fallback")
                return resp_json
            else:
                logger.warning(f"LiteLLM failed ({response.status_code}): {response.text[:300]}")
                raise HTTPException(status_code=502, detail=f"LiteLLM failed: {response.status_code}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Exception during LiteLLM proxy: {e}")
        await client.aclose()
        raise HTTPException(status_code=502, detail="LiteLLM upstream failed")

@app.get("/metrics")
async def metrics():
    """Expose triage and circuit breaker metrics in Prometheus format."""
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
    lines.append("# HELP circuit_breaker_google_tier Google breaker cooldown tier (0=open, 3=max)")
    lines.append("# TYPE circuit_breaker_google_tier gauge")
    lines.append(f"circuit_breaker_google_tier {google['tier']}")
    lines.append("# HELP circuit_breaker_vendor_tier Vendor breaker cooldown tier (0=open, 3=max)")
    lines.append("# TYPE circuit_breaker_vendor_tier gauge")
    lines.append(f"circuit_breaker_vendor_tier {vendor['tier']}")
    lines.append("# HELP circuit_breaker_agy_allowed Whether EITHER breaker allows agy (backward-compat)")
    lines.append("# TYPE circuit_breaker_agy_allowed gauge")
    lines.append(f"circuit_breaker_agy_allowed {int(breaker.is_allowed())}")
    lines.append("# HELP circuit_breaker_total_trips Total trips across both breakers")
    lines.append("# TYPE circuit_breaker_total_trips counter")
    lines.append(f"circuit_breaker_total_trips {google['total_trips'] + vendor['total_trips']}")
    
    return Response(content="\n".join(lines), media_type="text/plain; version=0.0.4")

@app.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard():
    # 1. Run live health checks
    valkey_status = await check_tcp_port("127.0.0.1", 6379)
    litellm_status = await check_http_endpoint("http://127.0.0.1:4000/")
    llama_server_status = await check_http_endpoint("http://127.0.0.1:8080/health")
    langfuse_status = await check_http_endpoint("http://127.0.0.1:3001")

    # 1c. Check Gemini OAuth token status
    oauth_status = get_gemini_oauth_status()

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

    # 1b. Fetch top free model from OpenRouter
    best_free_model = await get_best_free_model()

    # 2. Query Goose Sessions SQLite DB
    goose_sessions = get_goose_sessions()

    # 2b. Fetch live llama.cpp metrics
    llamacpp = await get_llamacpp_metrics()

    # 3. Calculative metrics — 5-tier triage ratios
    tier_data = {
        "simple":  {"count": stats.get("simple_requests", 0),    "label": "Simple Core (Lite/Gemma)",  "color": "#34d399"},
        "medium":  {"count": stats.get("medium_requests", 0),    "label": "Medium Core",                "color": "#fbbf24"},
        "complex": {"count": stats.get("complex_requests", 0),   "label": "Complex Core (Qwen)",         "color": "#a78bfa"},
        "reasoning":{"count": stats.get("reasoning_requests", 0),"label": "Reasoning Core (Claude)",      "color": "#60a5fa"},
        "advanced":{"count": stats.get("advanced_requests", 0),  "label": "Advanced Core (Nemotron)",    "color": "#f472b6"},
    }
    for k, v in tier_data.items():
        v["ratio"] = (v["count"] / stats["total_requests"] * 100.0) if stats["total_requests"] > 0 else 0.0

    # Build 5-segment stacked bar widths for inline CSS
    tier_bar_segments = ""
    for k, v in tier_data.items():
        if v["ratio"] > 0:
            tier_bar_segments += f'<div style="width:{v["ratio"]:.2f}%;background:linear-gradient(90deg,{v["color"]},{v["color"]}dd);transition:width 0.5s ease;height:100%;"></div>'

    # Build 5-item legend rows
    tier_legend_rows = ""
    for k, v in tier_data.items():
        tier_legend_rows += f"""
        <div style="display:flex;align-items:center;gap:8px;font-size:12px;">
            <span style="width:10px;height:10px;border-radius:2px;background:{v['color']};display:inline-block;box-shadow:0 0 4px {v['color']}aa;"></span>
            <span style="font-weight:600;">{v['label']}:</span>
            <span style="opacity:0.7;">{v['count']} requests ({v['ratio']:.1f}%)</span>
        </div>"""

    # 4. Generate dynamic conic-gradient CSS background for the Pie Chart
    pie_gradient = get_pie_chart_gradient()
    total_tool_tokens = sum(stats["tool_tokens"].values())
    
    # 5. Generate tool tokens HTML & Pie Chart Legend
    tool_tokens_html = ""
    pie_legend_html = ""
    max_tool_val = max(stats["tool_tokens"].values()) if max(stats["tool_tokens"].values()) > 0 else 1
    
    tool_colors = {
        "tree": "#34d399",   # Green
        "shell": "#fbbf24",  # Amber/Orange
        "write": "#a78bfa",  # Violet
        "view": "#60a5fa",   # Blue
        "other": "#f472b6",  # Pink
    }
    
    for tool_name, token_count in stats["tool_tokens"].items():
        pct = (token_count / max_tool_val) * 100.0
        overall_pct = (token_count / total_tool_tokens * 100.0) if total_tool_tokens > 0 else 0.0
        color = tool_colors.get(tool_name, "#94a3b8")
        
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
            route_label = ev.get('route', 'litellm_fallback')
            route_color = '#fbbf24' if route_label == 'google_oauth_direct' else '#818cf8'
            route_short = 'GOOGLE' if route_label == 'google_oauth_direct' else 'LITELLM'
            timeline_html += f"""
            <div style="display: flex; gap: 15px; margin-bottom: 15px; border-left: 2px solid rgba(255,255,255,0.1); padding-left: 20px; position: relative;">
                <div style="width: 10px; height: 10px; background: {route_color}; border-radius: 50%; position: absolute; left: -6px; top: 6px; box-shadow: 0 0 8px {route_color};"></div>
                <div style="flex-grow: 1;">
                    <div style="display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 2px;">
                        <span style="font-weight: 600; text-transform: uppercase; color: #a5b4fc;">🔧 {ev['tool']} <span style="font-size: 9px; padding: 1px 5px; border-radius: 4px; background: {route_color}22; color: {route_color}; border: 1px solid {route_color}44; margin-left: 6px; vertical-align: middle;">{route_short}</span></span>
                        <span style="opacity: 0.5; font-family: monospace;">{ev['timestamp']}</span>
                    </div>
                    <div style="font-size: 14px; opacity: 0.9;">
                        Processed <strong>{ev['tokens']:,} tokens</strong> on <span style="color: #c084fc;">{ev['model']}</span>
                    </div>
                    <div style="font-size: 12px; opacity: 0.5; margin-top: 2px;">
                        Latency: {ev['latency_ms']} ms
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
            is_active = (idx == 0)
            badge_style = "background: rgba(129, 140, 248, 0.15); color: #c084fc; border: 1px solid rgba(129, 140, 248, 0.3);" if is_active else "background: rgba(255,255,255,0.03); color: #fff; border: 1px solid rgba(255,255,255,0.05);"
            active_label = "<span style='font-size: 10px; background: #10b981; color: #fff; padding: 2px 6px; border-radius: 4px; margin-right: 8px; font-weight: bold;'>ACTIVE</span>" if is_active else ""
            
            desc = sess.get('description') or sess.get('name') or "Interactive session"
            tokens = sess.get('accumulated_total_tokens', 0) or 0
            
            goose_html += f"""
            <div style="background: rgba(255, 255, 255, 0.02); border: 1px solid rgba(255, 255, 255, 0.05); border-radius: 12px; padding: 15px; margin-bottom: 12px; display: flex; flex-direction: column; gap: 8px;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div style="display: flex; align-items: center;">
                        {active_label}
                        <span style="font-weight: 600; font-size: 15px;">Session {sess['id']}</span>
                    </div>
                    <span style="font-size: 12px; padding: 3px 8px; border-radius: 20px; {badge_style}">{sess.get('goose_mode', 'auto').upper()}</span>
                </div>
                <div style="font-size: 13px; opacity: 0.7; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                    {desc}
                </div>
                <div style="display: flex; justify-content: space-between; font-size: 11px; opacity: 0.5; margin-top: 4px;">
                    <span>📅 {sess['updated_at']}</span>
                    <span style="font-weight: bold; color: #a5b4fc;">{tokens:,} total tokens</span>
                </div>
            </div>
            """

    # 8. Routing Paths pie chart & legend
    routing_paths = stats.get("routing_paths", {"google_oauth_direct": 0, "litellm_fallback": 0})
    total_routed = sum(routing_paths.values())
    routing_pie_gradient = "background: rgba(255, 255, 255, 0.05);"
    routing_legend_html = ""
    routing_colors = {
        "google_oauth_direct": "#fbbf24",
        "litellm_fallback": "#818cf8"
    }
    routing_labels = {
        "google_oauth_direct": "Google OAuth Direct",
        "litellm_fallback": "LiteLLM Fallback"
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
        routing_pie_gradient = f"background: conic-gradient({', '.join(route_grad_parts)});"

    # 9. Model Usage — canonical source is Langfuse traces (replaces duplicated in-memory counter)
    # See router trace → LiteLLM trace linkage via X-Langfuse-Trace-Id header.

    # Persistent aggregated tokens
    p_tokens = stats.get("prompt_tokens", 0)
    c_tokens = stats.get("completion_tokens", 0)
    t_tokens = p_tokens + c_tokens
    
    # Source badge helper: generates a colored inline source tag
    def src_badge(label, color):
        return f"<span style='font-size: 9px; padding: 2px 7px; border-radius: 4px; background: {color}18; color: {color}; border: 1px solid {color}44; font-weight: 700; letter-spacing: 0.5px; vertical-align: middle; margin-right: 8px;'>{label}</span>"

    # 10. Pre-compute llama.cpp HTML cards
    llamacpp_models_html = ""
    if llamacpp["models"]:
        for m in llamacpp["models"]:
            status_style = "background: rgba(16,185,129,0.12); color: #34d399; border: 1px solid rgba(16,185,129,0.25);" if m["status"] == "loaded" else "background: rgba(255,255,255,0.04); color: rgba(255,255,255,0.4); border: 1px solid rgba(255,255,255,0.08);"
            params_str = f"<span>\U0001f9e0 {m['n_params']/1e9:.1f}B params</span>" if m["n_params"] else ""
            ctx_str = f"<span>\U0001f4d0 ctx {m['n_ctx']:,}</span>" if m["n_ctx"] else ""
            size_str = f"<span>\U0001f4be {m['size_bytes']/1e6:.0f} MB</span>" if m["size_bytes"] else ""
            llamacpp_models_html += f"""
            <div style="background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.05); border-radius: 12px; padding: 14px 18px; margin-bottom: 10px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                    <span style="font-weight: 700; font-size: 14px; font-family: monospace;">{m['id']}</span>
                    <span style="font-size: 10px; padding: 2px 8px; border-radius: 20px; font-weight: 700; letter-spacing: 0.5px; {status_style}">{m['status'].upper()}</span>
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
            dot_style = "background: #34d399; box-shadow: 0 0 8px #34d399;" if sl["is_processing"] else "background: rgba(255,255,255,0.15);"
            slot_items += f"""
            <div style="background: rgba(255,255,255,0.015); border: 1px solid rgba(255,255,255,0.04); border-radius: 10px; padding: 10px 14px; position: relative; overflow: hidden;">
                <div style="position: absolute; top: 0; right: 0; width: 8px; height: 8px; margin: 8px; border-radius: 50%; {dot_style}"></div>
                <div style="font-size: 13px; font-weight: 700; margin-bottom: 4px;">Slot {sl['id']}</div>
                <div style="font-size: 11px; opacity: 0.6; display: flex; flex-direction: column; gap: 2px;">
                    <span>Prompt: {sl['n_prompt_processed']} tok</span>
                    <span>Decoded: {sl['n_decoded']} tok</span>
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
            // Auto refresh metrics every 3 seconds
            setInterval(() => {{
                window.location.reload();
            }}, 3000);
        </script>
    </head>
    <body>
        <header>
            <div class="logo-area">
                <div class="logo-dot"></div>
                <div class="logo-text">Antigravity Gateway</div>
            </div>
            <div class="dashboard-title">System Control Center</div>
        </header>

        {oauth_banner_html}

        <main>
            <!-- LEFT COLUMN: LIVE TELEMETRY, METERS, PIES & TIMELINES -->
            <div>
                <!-- Analytics Card -->
                <div class="glass-card">
                    <div class="section-title">
                        <span>{src_badge('ROUTER', '#818cf8')} Gateway Performance Telemetry</span>
                        <span style="font-size: 12px; opacity: 0.5; font-weight: normal;">Persistent telemetry</span>
                    </div>

                    <div class="metrics-grid">
                        <div class="metric-box">
                            <span class="metric-value">{stats["total_requests"]}</span>
                            <span class="metric-label">Total API Calls</span>
                        </div>
                        <div class="metric-box">
                            <span class="metric-value" style="color: #c084fc; font-size: 20px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">{stats["last_triage_decision"]}</span>
                            <span class="metric-label">Last Triage Split</span>
                        </div>
                        <div class="metric-box">
                            <span class="metric-value">{stats["avg_triage_latency_ms"]:.1f} ms</span>
                            <span class="metric-label">Avg Triage Time</span>
                        </div>
                        <div class="metric-box">
                            <span class="metric-value">{stats["avg_proxy_latency_ms"]:.1f} ms</span>
                            <span class="metric-label">Avg Proxy Time</span>
                        </div>
                        <div class="metric-box">
                            <span class="metric-value" style="color: #34d399;">{stats["cache_hits"]}</span>
                            <span class="metric-label">Triage Cache Hits</span>
                        </div>
                    </div>

                    <div style="background: rgba(255,255,255,0.01); border: 1px solid rgba(255,255,255,0.02); padding: 25px; border-radius: 20px;">
                        <div style="font-size: 13px; font-weight: 600; margin-bottom: 12px;">{src_badge('ROUTER', '#818cf8')} Triage Routing Split</div>
                        <div class="ratio-container">
                            {tier_bar_segments}
                        </div>
                        <div style="display:flex;flex-wrap:wrap;gap:12px 20px;margin-top:12px;justify-content:center;">
                            {tier_legend_rows}
                        </div>
                    </div>
                </div>

                <!-- Token Distribution & Circular Tool Pies Card -->
                <div class="glass-card">
                    <div class="section-title">
                        <span>{src_badge('ROUTER', '#818cf8')} Tool Token Distribution</span>
                        <span style="font-size: 12px; opacity: 0.5; font-weight: normal;">Live conic-gradient pie</span>
                    </div>
                    
                    <div style="display: flex; gap: 40px; align-items: center; margin-bottom: 30px; flex-wrap: wrap;">
                        <div class="pie-chart"></div>
                        <div style="display: flex; flex-direction: column; gap: 12px; flex-grow: 1; min-width: 200px;">
                            <h4 style="font-size: 14px; text-transform: uppercase; letter-spacing: 1px; opacity: 0.6; margin-bottom: 5px;">Active Tool Split %</h4>
                            {pie_legend_html}
                        </div>
                    </div>

                    <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; border-top: 1px solid rgba(255,255,255,0.05); padding-top: 20px; text-align: center;">
                        <div>
                            <div style="font-size: 20px; font-weight: 800; color: #60a5fa;">{p_tokens:,}</div>
                            <div style="font-size: 11px; text-transform: uppercase; opacity: 0.5; margin-top: 4px; font-weight: 600; letter-spacing: 0.5px;">Prompt Tokens</div>
                        </div>
                        <div>
                            <div style="font-size: 20px; font-weight: 800; color: #a78bfa;">{c_tokens:,}</div>
                            <div style="font-size: 11px; text-transform: uppercase; opacity: 0.5; margin-top: 4px; font-weight: 600; letter-spacing: 0.5px;">Completion Tokens</div>
                        </div>
                        <div>
                            <div style="font-size: 20px; font-weight: 800; color: #34d399;">{t_tokens:,}</div>
                            <div style="font-size: 11px; text-transform: uppercase; opacity: 0.5; margin-top: 4px; font-weight: 600; letter-spacing: 0.5px;">Combined Total</div>
                        </div>
                    </div>
                </div>

                <!-- Routing Path Distribution Pie -->
                <div class="glass-card">
                    <div class="section-title">
                        <span>{src_badge('ROUTER', '#818cf8')} Routing Path Distribution</span>
                        <span style="font-size: 12px; opacity: 0.5; font-weight: normal;">% requests per path</span>
                    </div>
                    <div style="display: flex; gap: 40px; align-items: center; flex-wrap: wrap;">
                        <div style="width: 130px; height: 130px; border-radius: 50%; {routing_pie_gradient} box-shadow: 0 0 25px rgba(0,0,0,0.4); position: relative; flex-shrink: 0;">
                            <div style="position: absolute; width: 60px; height: 60px; background: #111827; border-radius: 50%; top: 35px; left: 35px; box-shadow: inset 0 0 10px rgba(0,0,0,0.8);"></div>
                        </div>
                        <div style="display: flex; flex-direction: column; gap: 12px; flex-grow: 1; min-width: 180px;">
                            {routing_legend_html if routing_legend_html else "<div style='opacity: 0.5; font-size: 13px;'>No routing data yet</div>"}
                        </div>
                    </div>
                </div>

                <!-- Final Model Usage: canonically tracked in Langfuse -->
                <div class="glass-card">
                    <div class="section-title">
                        <span>{src_badge('LITELLM', '#34d399')} Model Usage</span>
                        <span style="font-size: 12px; opacity: 0.5; font-weight: normal;">Full traces in Langfuse</span>
                    </div>
                    <div style="text-align: center; padding: 25px 20px;">
                        <p style="opacity: 0.7; margin-bottom: 14px; font-size: 14px;">Per-model usage, token consumption & cost are tracked with full trace detail in Langfuse.</p>
                        <a href="http://localhost:3001" target="_blank" style="display: inline-block; padding: 8px 18px; background: rgba(232,121,249,0.12); color: #e879f9; border: 1px solid rgba(232,121,249,0.25); border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 13px;">Open Langfuse Observability →</a>
                    </div>
                </div>

                <!-- Live Meters for Tool Tokens Card -->
                <div class="glass-card">
                    <div class="section-title">
                        <span>{src_badge('GOOSE', '#fbbf24')} Live Tool Token Meters</span>
                        <span style="font-size: 12px; opacity: 0.5; font-weight: normal;">Token meters per extension tool</span>
                    </div>
                    <div>
                        {tool_tokens_html}
                    </div>
                </div>

                <!-- Timelines Card -->
                <div class="glass-card" style="margin-bottom: 0;">
                    <div class="section-title">
                        <span>{src_badge('ROUTER', '#818cf8')} Request Timeline</span>
                        <span style="font-size: 12px; opacity: 0.5; font-weight: normal;">Recent completions cascade</span>
                    </div>
                    <div style="max-height: 400px; overflow-y: auto; padding-right: 5px;">
                        {timeline_html}
                    </div>
                </div>
            </div>

            <!-- RIGHT COLUMN: INFRASTRUCTURE & ACTIVE GOOSE SESSIONS -->
            <div style="display: flex; flex-direction: column;">
                <!-- Frontier Free Model widget -->
                <div class="glass-card" style="background: rgba(16, 185, 129, 0.03); border-color: rgba(16, 185, 129, 0.15); margin-bottom: 30px;">
                    <div class="section-title" style="margin-bottom: 10px; border-bottom: 1px solid rgba(16, 185, 129, 0.15); padding-bottom: 12px;">
                        <span>{src_badge('INTELLECT', '#34d399')} Frontier Free Model</span>
                        <span style="font-size: 11px; opacity: 0.4; font-weight: normal; font-family: monospace;">agentic index score</span>
                    </div>
                    <div style="background: rgba(255, 255, 255, 0.01); border: 1px solid rgba(255, 255, 255, 0.04); border-radius: 12px; padding: 16px 20px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <span style="font-weight: 800; font-size: 16px; color: #fff;">{best_free_model['name']}</span>
                            <span style="font-size: 13px; font-weight: 800; padding: 4px 10px; border-radius: 20px; background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.25);">⚡ {best_free_model['score']:.1f}</span>
                        </div>
                        <div style="font-size: 12px; font-family: monospace; opacity: 0.6; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-bottom: 8px;">
                            ID: {best_free_model['id']}
                        </div>
                        <div style="display: flex; justify-content: space-between; font-size: 11px; opacity: 0.5;">
                            <span>📐 context {best_free_model['context_length']:,} tok</span>
                            <span style="color: #34d399; font-weight: bold;">{ "LIVE" if not best_free_model.get('is_fallback') else "FALLBACK" }</span>
                        </div>
                    </div>
                </div>

                <!-- Infrastructure nodes card -->
                <div class="glass-card status-container">
                    <div class="section-title" style="margin-bottom: 10px;">{src_badge('ROUTER', '#818cf8')} Infrastructure Nodes</div>
                    
                    <div class="service-row">
                        <div class="service-info">
                            <span class="service-name">Triage Router</span>
                            <span class="service-port">:5000</span>
                        </div>
                        <span class="badge badge-online"><span class="pulse-dot"></span>Online</span>
                    </div>

                    <div class="service-row">
                        <div class="service-info">
                            <span class="service-name">LiteLLM Proxy</span>
                            <span class="service-port">:4000</span>
                        </div>
                        <span class="badge {'badge-online' if litellm_status else 'badge-offline'}">
                            <span class="pulse-dot"></span>{'Online' if litellm_status else 'Offline'}
                        </span>
                    </div>

                    <div class="service-row">
                        <div class="service-info">
                            <span class="service-name">Valkey Cache</span>
                            <span class="service-port">:6379</span>
                        </div>
                        <span class="badge {'badge-online' if valkey_status else 'badge-offline'}">
                            <span class="pulse-dot"></span>{'Online' if valkey_status else 'Offline'}
                        </span>
                    </div>

                    <div class="service-row">
                        <div class="service-info">
                            <span class="service-name">Llama-Server</span>
                            <span class="service-port">:8080</span>
                        </div>
                        <span class="badge {'badge-online' if llama_server_status else 'badge-offline'}">
                            <span class="pulse-dot"></span>{'Online' if llama_server_status else 'Offline'}
                        </span>
                    </div>

                    <div class="service-row">
                        <div class="service-info">
                            <span class="service-name">Langfuse Traces</span>
                            <span class="service-port">:3001</span>
                        </div>
                        <span class="badge {'badge-online' if langfuse_status else 'badge-offline'}">
                            <span class="pulse-dot"></span>{'Online' if langfuse_status else 'Offline'}
                        </span>
                    </div>
                </div>

                <!-- Llama.cpp Metrics Card -->
                <div class="glass-card">
                    <div class="section-title" style="margin-bottom: 10px;">
                        <span>{src_badge('LLAMA.CPP', '#fb923c')} Engine Metrics</span>
                        <span style="font-size: 11px; opacity: 0.4; font-weight: normal; font-family: monospace;">build {llamacpp['build']}</span>
                    </div>
                    {llamacpp_models_html}
                    {llamacpp_slots_html}
                </div>

                <!-- Goose active sessions and status card -->
                <div class="glass-card">
                    <div class="section-title" style="margin-bottom: 10px;">{src_badge('GOOSE', '#fbbf24')} Session Directory</div>
                    <div style="max-height: 420px; overflow-y: auto; padding-right: 5px;">
                        {goose_html}
                    </div>
                </div>

                <!-- Quick console links card -->
                <div class="glass-card status-container">
                    <div class="section-title" style="margin-bottom: 10px;">Quick Console Links</div>
                    <div class="btn-group">
                        <!-- Goose Dashboard local -->
                        <a href="https://t.me/SheepBot?start=goose" target="_blank" class="btn" style="background: rgba(251, 191, 36, 0.05); border-color: rgba(251, 191, 36, 0.2);">
                            <span>{src_badge('GOOSE', '#fbbf24')} 🦢 Goose Telegram Bot</span>
                            <span class="btn-arrow">→</span>
                        </a>
                    </div>
                    <div class="btn-group">
                        <a href="http://localhost:3001" target="_blank" class="btn">
                            <span>{src_badge('LANGFUSE', '#e879f9')} Observability UI</span>
                            <span class="btn-arrow">→</span>
                        </a>
                        <a href="http://localhost:4000/ui" target="_blank" class="btn">
                            <span>{src_badge('LITELLM', '#34d399')} Admin UI</span>
                            <span class="btn-arrow">→</span>
                        </a>
                        <a href="http://localhost:8080" target="_blank" class="btn">
                            <span>{src_badge('LLAMA.CPP', '#fb923c')} Server Router UI</span>
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

if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting LLM Triage Router on {host}:{port}...")
    uvicorn.run(app, host=host, port=port)
