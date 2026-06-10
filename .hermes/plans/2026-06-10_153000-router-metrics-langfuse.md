# Router Metrics → Langfuse Pipeline: Diagnosis & Improvement Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make the triage router's aggregate metrics (5-tier classification, latency, cache hits, circuit breaker state) visible in the Langfuse observability UI, and add trace-level linkage between classification decisions and downstream model execution — eliminating the blind spot where Langfuse sees model calls but has zero visibility into the triage layer.

**Architecture:** The router will gain a Langfuse SDK integration that pushes two things: (1) per-request classification traces (linked to LiteLLM's downstream traces via `trace_id` pass-through), and (2) periodic aggregate score pushes (dashboard KPIs as Langfuse scores). LiteLLM already sends per-request traces. The plan bridges the gap between the two.

**Tech Stack:** `langfuse` Python SDK (v3.x), existing router/main.py FastAPI app, same Langfuse project/host as LiteLLM uses.

---

## Current State Diagram

```
Request → Router (classifier) → LiteLLM (proxy) → OpenRouter → Response
                │                      │
                │                 ┌─────────────────────┐
                │                 │ Langfuse callback    │ ← LiteLLM's built-in
                │                 │ (per-request traces) │   (callbacks: [langfuse])
                │                 │ ✓ model used         │
                │                 │ ✓ tokens in/out      │
                │                 │ ✓ latency            │
                │                 │ ✓ cost               │
                │                 │ ✗ classification tier│
                │                 └─────────────────────┘
                │
        ┌─────────────────────┐
        │ Router /metrics      │ ← Prometheus format
        │ (aggregate counters) │
        │ ✓ 5-tier counts      │   NOBODY SCRAPES THIS
        │ ✓ token totals       │
        │ ✓ cache hits         │
        │ ✓ circuit breaker    │
        │ ✓ timeline (last 15) │ ← ephemeral, dies on restart
        └─────────────────────┘
```

**Key findings:**
- Router `/metrics` and Langfuse traces are **complementary, not duplicated** — one is aggregates, the other is per-request detail. Zero overlap in delivery mechanism.
- LiteLLM sends per-request traces to Langfuse silently (WARNING log level suppresses success callbacks).
- The router has **zero integration with Langfuse** — no SDK, no imports, no trace submission.
- The router's `model_usage` counter is a weaker duplicate of what Langfuse traces already capture — Langfuse has the canonical model usage data.
- The router's `routing_paths` (google_oauth_direct vs litellm_fallback) have **no equivalent in Langfuse** — that data is lost to the observability layer.
- The router's timeline (last 15 events) dies on every pod restart — `router_stats.json` persists but timeline is in-memory only.
- No Prometheus server exists, so `/metrics` is dead output — nothing scrapes it.

---

### Task 1: Install Langfuse SDK in Router Container

**Objective:** Add `langfuse` Python package to the router container image so the SDK is importable.

**Files:**
- Modify: `router/Containerfile` (pip install line)

**Step 1: Add langfuse to pip install**

Current line:
```
RUN pip install --no-cache-dir fastapi uvicorn httpx pyyaml python-multipart asyncpg
```

Change to:
```
RUN pip install --no-cache-dir fastapi uvicorn httpx pyyaml python-multipart asyncpg langfuse
```

**Step 2: Verify the package installs correctly**

```bash
podman build -t localhost/llm-triage-router:latest -f router/Containerfile router/ 2>&1 | tail -5
# Expected: Successfully tagged localhost/llm-triage-router:latest
```

**Step 3: Verify langfuse is importable in the container**

After `./start-stack.sh --full-rebuild`:
```bash
podman exec agent-router-pod-llm-triage-router python3 -c "import langfuse; print(langfuse.__version__)"
# Expected: 3.x.x (or whatever is latest)
```

---

### Task 2: Add Langfuse Environment Variables to pod.yaml

**Objective:** Expose `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_HOST` to the triage router container so the SDK can authenticate, using the same credentials LiteLLM already uses (already in `.env`).

**Files:**
- Modify: `pod.yaml` (env section for llm-triage-router container)

**Step 1: Add env vars to the router container's env section**

Find the llm-triage-router container block in `pod.yaml` and add under `env:`:
```yaml
    - name: LANGFUSE_PUBLIC_KEY
      valueFrom:
        configMapKeyRef:
          name: env-config
          key: LANGFUSE_PUBLIC_KEY
    - name: LANGFUSE_SECRET_KEY
      valueFrom:
        configMapKeyRef:
          name: env-config
          key: LANGFUSE_SECRET_KEY
    - name: LANGFUSE_HOST
      value: "http://127.0.0.1:3001"
```

The `configMapKeyRef` + `env-config` is what the existing pod.yaml uses — the `.env` file is sourced as a configMap at pod creation. Verify the existing pattern for other vars like `LITELLM_MASTER_KEY` and replicate.

**Step 2: Verify env vars land in the container**

After rebuild:
```bash
podman exec agent-router-pod-llm-triage-router env | grep LANGFUSE
# Expected: LANGFUSE_PUBLIC_KEY=pk-..., LANGFUSE_SECRET_KEY=sk-..., LANGFUSE_HOST=http://127.0.0.1:3001
```

---

### Task 3: Initialize Langfuse Client at Router Startup

**Objective:** Create a Langfuse client singleton the router can use to push classification traces and scores.

**Files:**
- Modify: `router/main.py` (near the top, after other initializations)

**Step 1: Import langfuse and initialize client**

Add after `logger = logging.getLogger("llm-triage-router")` (around line 17):

```python
# Langfuse observability — per-request traces + aggregate score pushes
_langfuse_client = None

def get_langfuse():
    """Return the Langfuse client singleton, lazily initialized."""
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
```

**Step 2: Add graceful error handling**

The `get_langfuse()` function returns `None` if Langfuse is unreachable — all trace pushes must check for None. This ensures the router never crashes because Langfuse is down.

**Step 3: Verify the client initializes**

Rebuild + deploy, then:
```bash
journalctl --user CONTAINER_NAME=agent-router-pod-llm-triage-router --since "30s ago" --no-pager | grep -i langfuse
# Expected: "Langfuse client initialized"
```

---

### Task 4: Push Classification Decision Traces to Langfuse

**Objective:** For every request, push a Langfuse trace recording the classification tier, latency, cache hit/miss, and routing path. This is the key new data Langfuse currently lacks — the triage layer's decision.

**Files:**
- Modify: `router/main.py` (in `chat_completions`, after classification, around line 830)

**Step 1: Add trace submission after classification**

After the stats increment block (after `save_persisted_stats()` around line 851), add:

```python
    # Push classification trace to Langfuse
    lf = get_langfuse()
    if lf:
        try:
            trace_name = f"triage-{target_model}"
            lf.trace(
                name=trace_name,
                input={"prompt_preview": prompt_text[:200] if 'prompt_text' in dir() else "N/A"},
                output={"tier": target_model},
                metadata={
                    "triage_latency_ms": round(triage_latency, 2),
                    "cache_hit": was_cache_hit,
                    "classification_raw": raw_classification_result if 'raw_classification_result' in dir() else target_model,
                    "total_requests": stats["total_requests"],
                },
                tags=[target_model, "classification"],
            )
        except Exception as e:
            logger.warning(f"Langfuse trace push failed (non-fatal): {e}")
```

**Step 2: Capture `raw_classification_result` and `was_cache_hit`**

In the cache-check section of `classify_request()` (around line 307-330), capture:
```python
was_cache_hit = False
raw_classification_result = None
```
Set `was_cache_hit = True` in the cache-hit branch. Set `raw_classification_result = ...` after the model returns its raw output (before grammar parsing).

Pass both back through the return value or as function-level variables accessible in the caller.

**Step 3: Verify traces appear in Langfuse UI**

After rebuild + deploy + sending a few test requests:
```bash
# Open Langfuse at http://localhost:3001
# Navigate to Traces → filter by tag "classification"
# Expected: traces named "triage-agent-simple-core", "triage-agent-complex-core", etc.
```

---

### Task 5: Link Router Trace to LiteLLM's Downstream Trace

**Objective:** Pass a `trace_id` from the router's classification trace into the LiteLLM request as a custom header, so Langfuse can link the two traces and show the full pipeline: classification → model execution.

**Files:**
- Modify: `router/main.py` (classification trace generation + LiteLLM proxy call)

**Step 1: Generate trace_id at classification and pass downstream**

When the Langfuse trace is created in Task 4, extract the trace ID:
```python
trace = lf.trace(...)
trace_id = trace.id  # the Langfuse-generated trace ID
```

**Step 2: Pass trace_id as a header to LiteLLM**

In the LiteLLM proxy call section (around line 1083), add to the headers:
```python
headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
    "X-Langfuse-Trace-Id": trace_id,  # NEW: links router trace → LiteLLM trace
}
```

LiteLLM's Langfuse callback reads `X-Langfuse-Trace-Id` and attaches the downstream call as a child span of the router's trace.

**Step 3: Verify linked traces**

In Langfuse UI → open a classification trace → verify it has child spans for the LiteLLM model call(s).

---

### Task 6: Push Periodic Aggregate Scores to Langfuse

**Objective:** Push the dashboard's aggregate KPI values (tier split %, cache hit rate, avg latency) as Langfuse "scores" every 5 minutes, making trends visible in the Langfuse UI without Prometheus/Grafana.

**Files:**
- Modify: `router/main.py` (add a background task on a timer)

**Step 1: Add a score-push background task**

```python
async def push_aggregate_scores():
    """Push aggregate KPIs as Langfuse scores every 5 minutes."""
    while True:
        await asyncio.sleep(300)  # 5 minutes
        lf = get_langfuse()
        if not lf:
            continue
        try:
            total = stats["total_requests"]
            if total == 0:
                continue
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
                {"name": "circuit_breaker_google_tier", "value": float(get_breaker().google.tier)},
                {"name": "circuit_breaker_vendor_tier", "value": float(get_breaker().vendor.tier)},
                {"name": "google_oauth_direct_ratio_pct", "value": stats["routing_paths"]["google_oauth_direct"] / total * 100},
            ]
            for s in scores:
                lf.score(name=s["name"], value=s["value"])
            lf.flush()
            logger.info(f"Pushed {len(scores)} aggregate scores to Langfuse")
        except Exception as e:
            logger.warning(f"Langfuse score push failed (non-fatal): {e}")
```

**Step 2: Start the background task at lifespan**

In the `@asynccontextmanager async def lifespan(app: FastAPI)` function, after `yield`, add:
```python
asyncio.create_task(push_aggregate_scores())
```

**Step 3: Verify scores appear in Langfuse**

After 5+ minutes:
- Langfuse UI → Scores → filter by name `simple_ratio_pct`
- Expected: score values trending over time

---

### Task 7: Remove Duplicated model_usage Counter (reduce code)

**Objective:** The router's `model_usage` counter tracks which model ran how many times — but Langfuse already has this data more precisely (per-request with full detail). Remove `model_usage` from the in-memory stats and dashboard, replacing with a note that Langfuse is the canonical source.

**Files:**
- Modify: `router/main.py` (stats struct, record_tool_usage, dashboard HTML)

**Step 1: Remove `model_usage` from stats init**

Delete lines 65-66:
```python
    "model_usage": {},
```

**Step 2: Remove `model_usage` tracking from record_tool_usage**

Delete lines 489-492:
```python
    if "model_usage" not in stats:
        stats["model_usage"] = {}
    stats["model_usage"][model] = stats["model_usage"].get(model, 0) + 1
```

**Step 3: Replace "Final Model Usage" dashboard pie with Langfuse link**

In the dashboard HTML (around line 1999-2040), replace the model usage pie chart section with:

```html
<div class="glass-card">
    <div class="section-title">
        <span>{src_badge('LANGFUSE', '#e879f9')} Per-Model Usage</span>
        <span style="font-size: 12px; opacity: 0.5; font-weight: normal;">Traces in Langfuse</span>
    </div>
    <div style="text-align: center; padding: 20px;">
        <p style="opacity: 0.7; margin-bottom: 12px;">Detailed per-model usage & traces are available in Langfuse.</p>
        <a href="http://localhost:3001" target="_blank" style="color: #818cf8; text-decoration: none; font-weight: 600;">
            Open Langfuse Observability →
        </a>
    </div>
</div>
```

**Step 4: Verify nothing breaks**

Rebuild + deploy, check the dashboard renders without errors:
```bash
curl -s http://localhost:5000/dashboard | grep -c "Per-Model Usage"
# Expected: 1 (the new section renders)
```

---

### Task 8: Persist Timeline Events to Disk (survive pod restarts)

**Objective:** The router's timeline (last 15 request events) currently lives in-memory only and resets on pod restart. Write it to a JSON file on each event so it survives.

**Files:**
- Modify: `router/main.py` (record_tool_usage, load_persisted_stats)

**Step 1: Save timeline to JSON file**

In `record_tool_usage()`, after appending the event and popping old events, add:
```python
    # Persist timeline to disk so it survives pod restarts
    try:
        timeline_path = "/config/router_dir/router_timeline.json"
        os.makedirs(os.path.dirname(timeline_path), exist_ok=True)
        with open(timeline_path, "w") as f:
            json.dump(stats["timeline"], f)
    except Exception as e:
        logger.warning(f"Failed to persist timeline: {e}")
```

**Step 2: Load timeline on startup**

In `load_persisted_stats()`, add:
```python
    timeline_path = "/config/router_dir/router_timeline.json"
    if os.path.exists(timeline_path):
        try:
            with open(timeline_path, "r") as f:
                stats["timeline"] = json.load(f)
        except Exception:
            pass  # stale/broken timeline file → start fresh
```

**Step 3: Verify timeline survives restart**

```bash
# Before restart: note the timeline
curl -s http://localhost:5000/dashboard | grep -o '"timestamp": "[^"]*"' | head -3

# Rebuild + deploy
./start-stack.sh --full-rebuild

# After restart: verify same events appear
curl -s http://localhost:5000/dashboard | grep -o '"timestamp": "[^"]*"' | head -3
```

---

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Langfuse SDK adds latency to requests | Classification traces are fire-and-forget via `lf.trace()` — non-blocking. Score pushes run in background task. Zero impact on request path. |
| Langfuse unavailable (container down) | Non-fatal — `get_langfuse()` returns `None`, all pushes check for None. Router never crashes. |
| Langfuse SDK dependency bloat | `pip install langfuse` adds ~2MB to the container image (already ~200MB). Negligible. |
| Trace volume overwhelms Langfuse (ClickHouse) | ~5,000 requests/day × 1 trace each = negligible for ClickHouse. Score pushes are 12 values every 5 min. |
| `model_usage` removal breaks dashboard | The "Final Model Usage" pie chart is replaced with a Langfuse link — no JS dependencies, pure HTML. |

## Verification Checklist

After all tasks complete:
- [ ] `podman exec ... python3 -c "import langfuse"` succeeds
- [ ] `LANGFUSE_*` env vars present in router container
- [ ] "Langfuse client initialized" in router startup logs
- [ ] Langfuse UI shows classification traces (tag: `classification`)
- [ ] Langfuse UI shows linked traces (router trace → LiteLLM trace)
- [ ] Langfuse UI shows aggregate scores (after 5+ minutes)
- [ ] Dashboard "Per-Model Usage" section links to Langfuse
- [ ] Timeline survives pod restart
- [ ] `curl :5000/v1/chat/completions` returns 200 with valid response
- [ ] `curl :5000/metrics` returns 200 (still works, unchanged)
- [ ] `curl :5000/dashboard` returns 200 (all widgets render)
- [ ] Zero ERROR in journalctl after 5 minutes of operation
