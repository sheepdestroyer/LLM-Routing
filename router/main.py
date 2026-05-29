import os
import sys
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
}

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
            stats["cache_hits"] += 1
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

    # Resolve backend connection parameters
    backend_conf = backends.get(target_model)
    if not backend_conf:
        logger.error(f"Backend '{target_model}' not found in configuration backends.")
        raise HTTPException(status_code=500, detail=f"Backend {target_model} misconfigured")

    backend_api_base = backend_conf["api_base"]
    backend_api_key = backend_conf["api_key"]

    # Modify incoming payload to use the triaged model name
    body["model"] = target_model

    # Set up outgoing proxy request
    client = httpx.AsyncClient(timeout=3600.0)  # 1-hour timeout matching llama-server timeout
    headers = {"Authorization": f"Bearer {backend_api_key}"}

    # Handle streaming vs non-streaming proxying
    if body.get("stream", False):
        async def stream_generator():
            proxy_start = time.time()
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
                        yield chunk
                
                # Update proxy metrics on completion
                proxy_latency = (time.time() - proxy_start) * 1000.0
                stats["total_proxy_time_ms"] += proxy_latency
                stats["avg_proxy_latency_ms"] = stats["total_proxy_time_ms"] / stats["total_requests"]
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
            
            return response.json()
        except Exception as e:
            await client.aclose()
            logger.error(f"Backend connection error: {e}")
            raise HTTPException(status_code=502, detail=f"Bad Gateway proxying connection: {e}")

@app.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard():
    # 1. Run live health checks
    triage_router_status = True
    valkey_status = await check_tcp_port("127.0.0.1", 6379)
    litellm_status = await check_http_endpoint("http://127.0.0.1:4000/health")
    llama_server_status = await check_http_endpoint("http://127.0.0.1:8080/health")
    langfuse_status = await check_http_endpoint("http://127.0.0.1:3000")

    # Calculative metrics
    simple_ratio = 0.0
    complex_ratio = 0.0
    if stats["total_requests"] > 0:
        simple_ratio = (stats["simple_requests"] / stats["total_requests"]) * 100.0
        complex_ratio = (stats["complex_requests"] / stats["total_requests"]) * 100.0

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=device-width, initial-scale=initial-scale=1.0">
        <title>LLM Triage Gateway - Control Center</title>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
        <style>
            :root {{
                --slate-900: #0f172a;
                --indigo-950: #1e1b4b;
                --emerald-500: #10b981;
                --rose-500: #f43f5e;
                --text-main: #f8fafc;
                --glass-bg: rgba(255, 255, 255, 0.05);
                --glass-border: rgba(255, 255, 255, 0.1);
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
                max-width: 1200px;
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
                max-width: 1200px;
                margin: 0 auto;
                padding: 0 20px 50px 20px;
                flex-grow: 1;
                display: grid;
                grid-template-columns: 2fr 1fr;
                gap: 30px;
            }}

            @media (max-width: 900px) {{
                main {{
                    grid-template-columns: 1fr;
                }}
            }}

            .glass-card {{
                background: var(--glass-bg);
                backdrop-filter: blur(12px);
                border: 1px solid var(--glass-border);
                border-radius: 20px;
                padding: 30px;
                box-shadow: 0 20px 40px rgba(0, 0, 0, 0.3);
                transition: transform 0.3s ease, border-color 0.3s ease;
            }}

            .glass-card:hover {{
                border-color: rgba(255, 255, 255, 0.2);
            }}

            .status-container {{
                display: flex;
                flex-direction: column;
                gap: 20px;
            }}

            .section-title {{
                font-size: 20px;
                font-weight: 600;
                margin-bottom: 20px;
                color: #e2e8f0;
                display: flex;
                justify-content: space-between;
                align-items: center;
            }}

            .service-row {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 15px 20px;
                background: rgba(255, 255, 255, 0.02);
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.05);
            }}

            .service-info {{
                display: flex;
                align-items: center;
                gap: 15px;
            }}

            .service-name {{
                font-weight: 600;
                font-size: 16px;
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
                font-size: 12px;
                font-weight: 600;
                padding: 6px 14px;
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
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 20px;
                margin-bottom: 30px;
            }}

            .metric-box {{
                background: rgba(255, 255, 255, 0.02);
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 16px;
                padding: 20px;
                display: flex;
                flex-direction: column;
                gap: 8px;
            }}

            .metric-value {{
                font-size: 32px;
                font-weight: 800;
                color: #fff;
            }}

            .metric-label {{
                font-size: 13px;
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
                gap: 15px;
                margin-top: 20px;
            }}

            .btn {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 16px 24px;
                background: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 12px;
                color: #fff;
                text-decoration: none;
                font-weight: 600;
                transition: all 0.3s ease;
            }}

            .btn:hover {{
                background: rgba(255, 255, 255, 0.08);
                border-color: rgba(129, 140, 248, 0.4);
                transform: translateX(5px);
            }}

            .btn-arrow {{
                opacity: 0.5;
                font-size: 18px;
                transition: transform 0.3s ease;
            }}

            .btn:hover .btn-arrow {{
                transform: translateX(3px);
                opacity: 1;
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
            // Auto refresh the metrics/health states every 5 seconds
            setInterval(() => {{
                window.location.reload();
            }}, 5000);
        </script>
    </head>
    <body>
        <header>
            <div class="logo-area">
                <div class="logo-dot"></div>
                <div class="logo-text">Antigravity Gateway</div>
            </div>
            <div class="dashboard-title">Control Center</div>
        </header>

        <main>
            <!-- LEFT SECTION: SERVICE STATS & METRICS -->
            <div class="glass-card">
                <div class="section-title">
                    <span>Performance Analytics</span>
                    <span style="font-size: 12px; opacity: 0.5;">Auto-refreshing 5s</span>
                </div>

                <div class="metrics-grid">
                    <div class="metric-box">
                        <span class="metric-value">{stats["total_requests"]}</span>
                        <span class="metric-label">Total Requests</span>
                    </div>
                    <div class="metric-box">
                        <span class="metric-value" style="color: #c084fc;">{stats["last_triage_decision"]}</span>
                        <span class="metric-label">Last Triage Split</span>
                    </div>
                    <div class="metric-box">
                        <span class="metric-value">{stats["avg_triage_latency_ms"]:.1f}ms</span>
                        <span class="metric-label">Avg Triage Latency</span>
                    </div>
                    <div class="metric-box">
                        <span class="metric-value">{stats["avg_proxy_latency_ms"]:.1f}ms</span>
                        <span class="metric-label">Avg Gateway proxy latency</span>
                    </div>
                    <div class="metric-box">
                        <span class="metric-value" style="color: #34d399;">{stats["cache_hits"]}</span>
                        <span class="metric-label">Triage Cache Hits</span>
                    </div>
                </div>

                <div class="section-title" style="margin-top: 40px; margin-bottom: 10px;">
                    <span>Triage Distribution</span>
                </div>
                <div style="background: rgba(255,255,255,0.01); border: 1px solid rgba(255,255,255,0.03); padding: 25px; border-radius: 16px;">
                    <div style="display: flex; justify-content: space-between; font-weight: 600;">
                        <span>Simple Tasks (Lite / Gemma)</span>
                        <span>Complex Tasks (Gemini / Qwen)</span>
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

            <!-- RIGHT SECTION: SERVICE STATUSES & WEB SERVICES LINKS -->
            <div style="display: flex; flex-direction: column; gap: 30px;">
                <!-- health card -->
                <div class="glass-card status-container">
                    <div class="section-title" style="margin-bottom: 10px;">Infrastructure Nodes</div>
                    
                    <!-- triage -->
                    <div class="service-row">
                        <div class="service-info">
                            <span class="service-name">Triage Router</span>
                            <span class="service-port">:5000</span>
                        </div>
                        <span class="badge badge-online"><span class="pulse-dot"></span>Online</span>
                    </div>

                    <!-- litellm -->
                    <div class="service-row">
                        <div class="service-info">
                            <span class="service-name">LiteLLM Proxy</span>
                            <span class="service-port">:4000</span>
                        </div>
                        <span class="badge {'badge-online' if litellm_status else 'badge-offline'}">
                            <span class="pulse-dot"></span>{'Online' if litellm_status else 'Offline'}
                        </span>
                    </div>

                    <!-- valkey -->
                    <div class="service-row">
                        <div class="service-info">
                            <span class="service-name">Valkey Cache</span>
                            <span class="service-port">:6379</span>
                        </div>
                        <span class="badge {'badge-online' if valkey_status else 'badge-offline'}">
                            <span class="pulse-dot"></span>{'Online' if valkey_status else 'Offline'}
                        </span>
                    </div>

                    <!-- llama-server -->
                    <div class="service-row">
                        <div class="service-info">
                            <span class="service-name">Llama-Server</span>
                            <span class="service-port">:8080</span>
                        </div>
                        <span class="badge {'badge-online' if llama_server_status else 'badge-offline'}">
                            <span class="pulse-dot"></span>{'Online' if llama_server_status else 'Offline'}
                        </span>
                    </div>

                    <!-- langfuse -->
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

                <!-- navigation links card -->
                <div class="glass-card status-container">
                    <div class="section-title" style="margin-bottom: 10px;">Quick Console Links</div>
                    <div class="btn-group">
                        <a href="http://localhost:3000" target="_blank" class="btn">
                            <span>Langfuse Observability UI</span>
                            <span class="btn-arrow">→</span>
                        </a>
                        <a href="http://localhost:4000/ui" target="_blank" class="btn">
                            <span>LiteLLM Admin UI</span>
                            <span class="btn-arrow">→</span>
                        </a>
                        <a href="http://localhost:8080" target="_blank" class="btn">
                            <span>Llama-Server Router UI</span>
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
