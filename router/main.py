import os
import sys
import json
import time
import socket
import logging
import yaml
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("llm-triage-router")

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
router_model_name = router_model_conf.get("model", "qwen-35b-q4ks")

system_prompt = config.get("classification_rules", {}).get("system_prompt", "")
backends = {b["name"]: b for b in config.get("backends", [])}

# Triage and Performance Metric Trackers
stats = {
    "total_requests": 0,
    "simple_requests": 0,
    "complex_requests": 0,
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
    "model_usage": {},
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

app = FastAPI(title="LLM Triage Router")

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

async def classify_request(prompt: str) -> tuple[str, float]:
    """Queries the local fast Qwen instance to classify request complexity with TTL caching."""
    global triage_cache, stats
    
    # Normalize the prompt text for cache mapping
    normalized_prompt = prompt.strip().lower()
    
    # 1. Check in-memory TTL cache
    if normalized_prompt in triage_cache:
        cached_decision, cached_time = triage_cache[normalized_prompt]
        if time.time() - cached_time < CACHE_TTL_SECONDS:
            logger.info(f"⚡ Triage Cache Hit for prompt: '{normalized_prompt[:50]}...' -> routed to '{cached_decision}'")
            stats["cache_hits"] = stats.get("cache_hits", 0) + 1
            save_persisted_stats()
            return cached_decision, 0.0  # 0.0ms classification latency
            
    start_time = time.time()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            payload = {
                "model": router_model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.0,
                "max_tokens": 15
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
                return "agent-complex-core", latency
                
            result = response.json()
            message_obj = result["choices"][0]["message"]
            content = message_obj.get("content") or ""
            reasoning = message_obj.get("reasoning_content") or ""
            classification = (content + " " + reasoning).strip()
            logger.info(f"Raw classifier response (content + reasoning): '{classification}'")
            
            # Sanitize response
            classification_clean = classification.replace("`", "").replace('"', '').replace("'", "").strip()
            
            decision = "agent-complex-core"
            if "agent-simple-core" in classification_clean:
                decision = "agent-simple-core"
                
            # Store in cache
            triage_cache[normalized_prompt] = (decision, time.time())
            return decision, latency
                
    except Exception as e:
        latency = (time.time() - start_time) * 1000.0
        logger.error(f"Exception during classification: {e}")
        return "agent-complex-core", latency

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
                    logger.warning("⚠️ Gemini OAuth token on disk is expired or missing.")
    except Exception as e:
        logger.error(f"Failed to read live OAuth token: {e}")
    return None

def detect_active_tool(body: dict) -> str:
    """Inspects request payload messages to identify which developer tool is currently being invoked."""
    messages = body.get("messages", [])
    for msg in reversed(messages):
        role = msg.get("role")
        if role in ("tool", "function"):
            name = msg.get("name") or "other"
            if "__" in name:
                name = name.split("__")[-1]
            return name
        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls and isinstance(tool_calls, list):
                for tc in tool_calls:
                    name = tc.get("function", {}).get("name") or "other"
                    if "__" in name:
                        name = name.split("__")[-1]
                    return name
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
    
    # Track final model usage
    if "model_usage" not in stats:
        stats["model_usage"] = {}
    stats["model_usage"][model] = stats["model_usage"].get(model, 0) + 1
    
    # Append to timeline event stack
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

    # Classify request
    target_model, triage_latency = await classify_request(last_user_message)
    logger.info(f"Triage decision: Routing request to backend model -> '{target_model}'")

    # Update in-memory statistics
    stats["total_requests"] += 1
    stats["last_triage_decision"] = target_model
    stats["total_triage_time_ms"] += triage_latency
    stats["avg_triage_latency_ms"] = stats["total_triage_time_ms"] / stats["total_requests"]
    
    if target_model == "agent-simple-core":
        stats["simple_requests"] += 1
    else:
        stats["complex_requests"] += 1
    save_persisted_stats()

    # --- GOOGLE OAUTH DIRECT ROUTE WITH FALLBACK ---
    oauth_token = get_live_gemini_oauth_token()
    if oauth_token:
        google_model = "gemini-3.5-flash" if target_model == "agent-complex-core" else "gemini-3.1-flash-lite"
        logger.info(f"🔄 Direct Gemini OAuth Route: Mapping '{target_model}' to Google '{google_model}'...")
        
        google_body = body.copy()
        google_body["model"] = google_model
        
        google_headers = {
            "Authorization": f"Bearer {oauth_token}",
            "Content-Type": "application/json"
        }
        google_api_base = "https://generativelanguage.googleapis.com/v1beta/openai"
        
        try:
            logger.info("Attempting direct Google Gemini API call...")
            
            if body.get("stream", False):
                client = httpx.AsyncClient(timeout=3600.0)
                req = client.build_request(
                    "POST",
                    f"{google_api_base}/chat/completions",
                    json=google_body,
                    headers=google_headers
                )
                r = await client.send(req, stream=True)
                if r.status_code == 200:
                    async def google_stream_generator():
                        completion_chars = 0
                        request_tokens = len(json.dumps(google_body)) // 4
                        try:
                            async for chunk in r.aiter_bytes():
                                completion_chars += len(chunk)
                                yield chunk
                            # Log tool metrics
                            latency_ms = (time.time() - start_time) * 1000.0
                            record_tool_usage(active_tool, request_tokens, completion_chars // 4, google_model, latency_ms, route="google_oauth_direct")
                        except Exception as ex:
                            logger.error(f"Stream generation error on direct Google call: {ex}")
                        finally:
                            await r.aclose()
                            await client.aclose()
                    return StreamingResponse(google_stream_generator(), media_type="text/event-stream")
                else:
                    logger.warning(f"Direct Google stream call failed with status {r.status_code}. Falling back to default LiteLLM path.")
                    await r.aclose()
                    await client.aclose()
            else:
                client = httpx.AsyncClient(timeout=3600.0)
                r = await client.post(
                    f"{google_api_base}/chat/completions",
                    json=google_body,
                    headers=google_headers
                )
                await client.aclose()
                if r.status_code == 200:
                    resp_json = r.json()
                    usage = resp_json.get("usage", {})
                    prompt_tokens = usage.get("prompt_tokens", len(json.dumps(google_body)) // 4)
                    completion_tokens = usage.get("completion_tokens", len(json.dumps(resp_json)) // 4)
                    latency_ms = (time.time() - start_time) * 1000.0
                    record_tool_usage(active_tool, prompt_tokens, completion_tokens, google_model, latency_ms, route="google_oauth_direct")
                    return resp_json
                else:
                    logger.warning(f"Direct Google completion call failed with status {r.status_code}. Falling back to default LiteLLM path.")
        except Exception as e:
            logger.error(f"Direct Google call encountered exception: {e}. Falling back to default LiteLLM path.")

    # Resolve backend connection parameters
    backend_conf = backends.get(target_model)
    if not backend_conf:
        logger.error(f"Backend '{target_model}' not found in configuration backends.")
        raise HTTPException(status_code=500, detail=f"Backend {target_model} misconfigured")

    backend_api_base = backend_conf["api_base"]
    backend_api_key = backend_conf["api_key"]
    if backend_api_key == "DYNAMIC_LITELLM_MASTER_KEY_PLACEHOLDER":
        backend_api_key = os.getenv("LITELLM_MASTER_KEY", backend_api_key)

    # Modify incoming payload to use the triaged model name
    body["model"] = target_model

    # Set up outgoing proxy request
    client = httpx.AsyncClient(timeout=3600.0)
    headers = {"Authorization": f"Bearer {backend_api_key}"}

    # Handle streaming vs non-streaming proxying
    if body.get("stream", False):
        async def stream_generator():
            proxy_start = time.time()
            completion_chars = 0
            request_tokens = len(json.dumps(body)) // 4
            try:
                async with client.stream(
                    "POST",
                    f"{backend_api_base}/chat/completions",
                    json=body,
                    headers=headers
                ) as r:
                    if r.status_code != 200:
                        logger.error(f"Backend streaming failed with status {r.status_code}")
                        yield f"data: {{\"error\": \"Backend returned status {r.status_code}\"}}\n\n".encode("utf-8")
                        return
                    async for chunk in r.aiter_bytes():
                        completion_chars += len(chunk)
                        yield chunk
                
                # Update proxy metrics on completion
                proxy_latency = (time.time() - proxy_start) * 1000.0
                stats["total_proxy_time_ms"] += proxy_latency
                stats["avg_proxy_latency_ms"] = stats["total_proxy_time_ms"] / stats["total_requests"]
                
                record_tool_usage(active_tool, request_tokens, completion_chars // 4, target_model, proxy_latency, route="litellm_fallback")
            except Exception as e:
                logger.error(f"Streaming connection error: {e}")
                yield f"data: {{\"error\": \"Proxy streaming exception: {e}\"}}\n\n".encode("utf-8")
            finally:
                await client.aclose()

        return StreamingResponse(stream_generator(), media_type="text/event-stream")
    else:
        try:
            proxy_start = time.time()
            response = await client.post(
                f"{backend_api_base}/chat/completions",
                json=body,
                headers=headers
            )
            await client.aclose()
            
            # Update proxy metrics
            proxy_latency = (time.time() - proxy_start) * 1000.0
            stats["total_proxy_time_ms"] += proxy_latency
            stats["avg_proxy_latency_ms"] = stats["total_proxy_time_ms"] / stats["total_requests"]
            
            resp_json = response.json()
            usage = resp_json.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", len(json.dumps(body)) // 4)
            completion_tokens = usage.get("completion_tokens", len(json.dumps(resp_json)) // 4)
            record_tool_usage(active_tool, prompt_tokens, completion_tokens, target_model, proxy_latency, route="litellm_fallback")
            
            return resp_json
        except Exception as e:
            await client.aclose()
            logger.error(f"Backend connection error: {e}")
            raise HTTPException(status_code=502, detail=f"Bad Gateway proxying connection: {e}")

@app.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard():
    # 1. Run live health checks
    valkey_status = await check_tcp_port("127.0.0.1", 6379)
    litellm_status = await check_http_endpoint("http://127.0.0.1:4000/")
    llama_server_status = await check_http_endpoint("http://127.0.0.1:8080/health")
    langfuse_status = await check_http_endpoint("http://127.0.0.1:3000")

    # 2. Query Goose Sessions SQLite DB
    goose_sessions = get_goose_sessions()

    # 2b. Fetch live llama.cpp metrics
    llamacpp = await get_llamacpp_metrics()

    # 3. Calculative metrics
    simple_ratio = 0.0
    complex_ratio = 0.0
    if stats["total_requests"] > 0:
        simple_ratio = (stats["simple_requests"] / stats["total_requests"]) * 100.0
        complex_ratio = (stats["complex_requests"] / stats["total_requests"]) * 100.0

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

    # 9. Model Usage pie chart & legend
    model_usage = stats.get("model_usage", {})
    total_model_calls = sum(model_usage.values())
    model_pie_gradient = "background: rgba(255, 255, 255, 0.05);"
    model_legend_html = ""
    model_palette = ["#34d399", "#60a5fa", "#f472b6", "#fbbf24", "#a78bfa", "#fb923c", "#38bdf8", "#e879f9"]
    if total_model_calls > 0:
        current_angle = 0.0
        model_grad_parts = []
        for idx_m, (mname, mcount) in enumerate(sorted(model_usage.items(), key=lambda x: -x[1])):
            mpct = (mcount / total_model_calls) * 100.0
            next_angle = current_angle + mpct
            mcolor = model_palette[idx_m % len(model_palette)]
            model_grad_parts.append(f"{mcolor} {current_angle:.1f}% {next_angle:.1f}%")
            model_legend_html += f"""
            <div style="display: flex; align-items: center; gap: 8px; font-size: 13px;">
                <span style="width: 12px; height: 12px; border-radius: 50%; background: {mcolor}; display: inline-block; box-shadow: 0 0 6px {mcolor}aa;"></span>
                <span style="font-weight: 600; font-family: monospace; font-size: 12px;">{mname}:</span>
                <span style="opacity: 0.7;">{mcount} ({mpct:.1f}%)</span>
            </div>
            """
            current_angle = next_angle
        model_pie_gradient = f"background: conic-gradient({', '.join(model_grad_parts)});"

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

            .ratio-simple {{
                background: linear-gradient(90deg, #34d399, #10b981);
                transition: width 0.5s ease;
            }}

            .ratio-complex {{
                background: linear-gradient(90deg, #a78bfa, #818cf8);
                transition: width 0.5s ease;
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
                        <div style="font-size: 13px; font-weight: 600; margin-bottom: 8px;">{src_badge('ROUTER', '#818cf8')} Triage Routing Split</div>
                        <div style="display: flex; justify-content: space-between; font-weight: 600; font-size: 14px;">
                            <span>Simple Splits (Lite / Gemma)</span>
                            <span>Complex Splits (Gemini / Qwen)</span>
                        </div>
                        <div class="ratio-container">
                            <div class="ratio-simple" style="width: {simple_ratio}%"></div>
                            <div class="ratio-complex" style="width: {complex_ratio}%"></div>
                        </div>
                        <div class="ratio-legend">
                            <span>{stats["simple_requests"]} requests ({simple_ratio:.1f}%)</span>
                            <span>{stats["complex_requests"]} requests ({complex_ratio:.1f}%)</span>
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

                <!-- Final Model Usage Pie -->
                <div class="glass-card">
                    <div class="section-title">
                        <span>{src_badge('LITELLM', '#34d399')} Final Model Usage</span>
                        <span style="font-size: 12px; opacity: 0.5; font-weight: normal;">% calls per model</span>
                    </div>
                    <div style="display: flex; gap: 40px; align-items: center; flex-wrap: wrap;">
                        <div style="width: 130px; height: 130px; border-radius: 50%; {model_pie_gradient} box-shadow: 0 0 25px rgba(0,0,0,0.4); position: relative; flex-shrink: 0;">
                            <div style="position: absolute; width: 60px; height: 60px; background: #111827; border-radius: 50%; top: 35px; left: 35px; box-shadow: inset 0 0 10px rgba(0,0,0,0.8);"></div>
                        </div>
                        <div style="display: flex; flex-direction: column; gap: 12px; flex-grow: 1; min-width: 180px;">
                            {model_legend_html if model_legend_html else "<div style='opacity: 0.5; font-size: 13px;'>No model data yet</div>"}
                        </div>
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
                            <span class="service-port">:3000</span>
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
                        <a href="http://localhost:3001" target="_blank" class="btn" style="background: rgba(251, 191, 36, 0.05); border-color: rgba(251, 191, 36, 0.2);">
                            <span>{src_badge('GOOSE', '#fbbf24')} 🦢 Goose Local Dashboard</span>
                            <span class="btn-arrow">→</span>
                        </a>
                    </div>
                    <div class="btn-group">
                        <!-- Goose Dashboard link -->
                        <a href="https://block.github.io/goose/" target="_blank" class="btn" style="background: rgba(129, 140, 248, 0.05); border-color: rgba(129, 140, 248, 0.2);">
                            <span>🐦 Goose Official Portal Dashboard</span>
                            <span class="btn-arrow">→</span>
                        </a>
                        <a href="http://localhost:3000" target="_blank" class="btn">
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
