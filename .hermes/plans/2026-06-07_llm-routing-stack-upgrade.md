# LLM-Routing Stack Upgrade — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task. Each section is a self-contained workstream; within each section, tasks are ordered by dependency.

**Goal:** Harden the LLM-Routing gateway with circuit-breaking for agy quota exhaustion, concurrent daemon processing, semantic caching, tuned LiteLLM routing, automated monitoring cron jobs, and infrastructure observability upgrades.

**Architecture:** Changes touch four layers — (a) the triage router's agy proxy module for circuit-breaking, (b) the host-side agy daemon for concurrency, (c) LiteLLM/PG infra for caching and routing, (d) Hermes cron jobs for unattended monitoring. All changes are backward-compatible and additive; no existing behavior is removed.

**Tech Stack:** Python 3 (FastAPI + asyncio), LiteLLM proxy (YAML config), PostgreSQL (pgvector), Podman, systemd user services, Hermes cron.

---

## Workstream A: agy Quota Circuit Breaker (P0)

> **Circuit breaker design:** 3-tier exponential cooldown enforcing escalating backoff on Cloud Code Assist quota exhaustion. 1st hit → short penalty, 2nd hit → medium penalty, 3rd+ hit → 5 hours (300 min = official Google quota refresh window). After any cooldown expires, the breaker resets to Tier 0 (open for business) and the next hit starts at Tier 1 again.

**State machine:**

```
Tier 0: OPEN (agy allowed)
  └── quota detected → Tier 1: 5-minute cooldown (300s)

Tier 1: COOLDOWN (agy blocked, 5 min)
  └── cooldown expires → Tier 1.5: PROBE (1 request allowed)
      ├── success → Tier 0 (reset)
      └── quota again → Tier 2: 30-minute cooldown (1800s)

Tier 2: COOLDOWN (agy blocked, 30 min)
  └── cooldown expires → Tier 2.5: PROBE (1 request allowed)
      ├── success → Tier 0 (reset)
      └── quota again → Tier 3: 5-hour cooldown (18000s)

Tier 3: COOLDOWN (agy blocked, 5 hours)
  └── cooldown expires → Tier 3.5: PROBE (1 request allowed)
      ├── success → Tier 0 (reset)
      └── quota again → Tier 3 (stay at 5-hour cooldown, repeat)
```

**Probe behavior:** When a cooldown expires and a request arrives, agy is allowed exactly one attempt. If it succeeds, the breaker resets fully. If quota is still exhausted, the breaker advances to the next tier immediately (or stays at Tier 3).

**Implementation location:** New file `router/circuit_breaker.py`, integrated into `router/agy_proxy.py` at the `try_agy_proxy()` entry point and in `router/main.py` at the complex-request routing path.

### Task A1: Create circuit breaker state module

**Objective:** Create a standalone module with the state machine logic — no integration yet, just the data structure and decision functions.

**Files:**
- Create: `/home/gpav/Vrac/LAB/AI/LLM-Routing/router/circuit_breaker.py`

**Complete code:**

```python
"""
Circuit breaker for Google Cloud Code Assist quota exhaustion.

3-tier exponential cooldown:
  Tier 1: 5 min (300s)
  Tier 2: 30 min (1800s)  
  Tier 3: 5 hours (18000s) — matches official Google quota refresh window

After each cooldown, one probe request is allowed. If it succeeds
the breaker resets. If quota is still exhausted, the breaker advances
to the next tier (or stays at Tier 3).
"""

import time
import logging

logger = logging.getLogger("circuit-breaker")

# Cooldown durations in seconds
TIER_COOLDOWNS = {
    1: 300,     # 5 minutes
    2: 1800,    # 30 minutes
    3: 18000,   # 5 hours
}

MAX_TIER = 3


class AgyCircuitBreaker:
    """Tracks quota exhaustion state with exponential cooldown."""

    def __init__(self):
        # tier: 0 = open (agy allowed), 1-3 = cooldown active
        self.tier: int = 0
        self.cooldown_until: float = 0.0
        # probe_granted: True when cooldown expired and next request is the probe
        self.probe_granted: bool = False
        # Statistics
        self.total_trips: int = 0
        self.last_trip_time: float = 0.0

    def is_allowed(self) -> bool:
        """
        Check whether an agy request is currently allowed.
        
        Returns True if:
          - tier == 0 (breaker open, agy operational)
          - probe_granted (cooldown expired, one probe attempt allowed)
        
        If a cooldown has just expired and no probe was granted yet,
        grant the probe and return True.
        """
        now = time.time()

        if self.tier == 0:
            return True

        # Check if cooldown has expired
        if self.cooldown_until > 0 and now >= self.cooldown_until:
            # Cooldown expired — grant exactly one probe
            if not self.probe_granted:
                self.probe_granted = True
                logger.info(
                    f"Circuit breaker: Tier {self.tier} cooldown expired. "
                    f"Granting 1 probe request to test agy availability."
                )
                return True
            # Already granted and consumed — stay blocked until reset
            return False

        # Cooldown still active
        remaining = self.cooldown_until - now
        logger.debug(
            f"Circuit breaker: agy blocked (tier {self.tier}, "
            f"{remaining:.0f}s remaining)"
        )
        return False

    def record_success(self):
        """Called when an agy request succeeds — resets the breaker to Tier 0."""
        if self.tier > 0:
            logger.info(
                f"Circuit breaker: agy probe succeeded — resetting from "
                f"Tier {self.tier} to Tier 0 (open)"
            )
        self.tier = 0
        self.cooldown_until = 0.0
        self.probe_granted = False

    def record_failure(self):
        """
        Called when agy returns quota-exhausted.
        
        If we were in a probe window, consume the probe and advance to next tier.
        If we were in Tier 0, trip to Tier 1.
        If we were already at Tier 3, stay at Tier 3 (renew cooldown).
        """
        now = time.time()
        self.total_trips += 1
        self.last_trip_time = now

        if self.tier == 0:
            # First failure — trip to Tier 1
            new_tier = 1
        elif self.probe_granted:
            # Probe failed — advance to next tier (or stay at max)
            new_tier = min(self.tier + 1, MAX_TIER)
        else:
            # Already in cooldown (shouldn't normally happen — 
            # is_allowed() would have blocked this)
            new_tier = min(self.tier + 1, MAX_TIER)

        cooldown = TIER_COOLDOWNS[new_tier]
        self.tier = new_tier
        self.cooldown_until = now + cooldown
        self.probe_granted = False

        if new_tier == MAX_TIER:
            logger.warning(
                f"Circuit breaker: TRIPPED to Tier {new_tier} — "
                f"agy blocked for {cooldown / 3600:.1f} hours "
                f"(until official quota refresh). "
                f"Total trips: {self.total_trips}"
            )
        else:
            logger.warning(
                f"Circuit breaker: advanced to Tier {new_tier} — "
                f"agy blocked for {cooldown / 60:.0f} min. "
                f"Total trips: {self.total_trips}"
            )

    def status(self) -> dict:
        """Return a structured status dict for the dashboard."""
        now = time.time()
        remaining = max(0, self.cooldown_until - now)
        return {
            "tier": self.tier,
            "agy_allowed": self.is_allowed(),
            "cooldown_remaining_seconds": int(remaining),
            "cooldown_total_seconds": TIER_COOLDOWNS.get(self.tier, 0),
            "total_trips": self.total_trips,
            "last_trip_time": self.last_trip_time,
            "probe_granted": self.probe_granted,
        }


# Module-level singleton — all agy-related code imports this instance
_breaker = AgyCircuitBreaker()


def get_breaker() -> AgyCircuitBreaker:
    """Return the module-level circuit breaker singleton."""
    return _breaker
```

**Verification:**
```bash
cd /home/gpav/Vrac/LAB/AI/LLM-Routing
python3 -c "
from router.circuit_breaker import get_breaker
b = get_breaker()
assert b.is_allowed() == True, 'Tier 0 should be open'
b.record_failure()
assert b.tier == 1, 'Should be at Tier 1'
assert b.is_allowed() == False, 'Tier 1 should block (cooldown active)'
# Force cooldown expiry
b.cooldown_until = 0
assert b.is_allowed() == True, 'Probe should be granted'
assert b.probe_granted == True
b.record_failure()  # probe fails
assert b.tier == 2, 'Should advance to Tier 2'
print('All assertions passed')
"
```

### Task A2: Integrate circuit breaker into agy_proxy.py

**Objective:** Wrap the `try_agy_proxy()` function with circuit breaker checks — before attempting agy, check `is_allowed()`; on success call `record_success()`; on quota exhaustion call `record_failure()`.

**Files:**
- Modify: `/home/gpav/Vrac/LAB/AI/LLM-Routing/router/agy_proxy.py`

**Step 1: Add import at top of `agy_proxy.py` (after existing imports, line ~30)**

```python
from circuit_breaker import get_breaker
```

**Step 2: Add breaker check at the top of `try_agy_proxy()` (after the docstring, around line 189)**

Insert after the docstring and before the context-building:
```python
    breaker = get_breaker()
    if not breaker.is_allowed():
        logger.info(
            f"agy proxy: circuit breaker open (tier {breaker.tier}) — "
            f"skipping agy, falling through to LiteLLM directly"
        )
        return None
```

**Step 3: Record success after a successful non-streaming response (around line 367, inside the success block)**

After the `logger.info("agy proxy: ✅ tier ... succeeded ...")` and before `return _wrap_response(...)`, add:
```python
                breaker.record_success()
```

**Step 4: Record success after a successful streaming response**

In the streaming path, after the first token is successfully yielded (around line 285-295, inside the `token_generator` where the conversation ID is first set), record success. Actually, for streaming it's ambiguous — record success when the stream starts (after confirming the first line is not a failure):

After the `# Success! Stream has started.` comment block (around line 282), before the `async def token_generator(...)`:
```python
            breaker.record_success()
```

**Step 5: Record failure on quota exhaustion**

In the non-streaming path, modify the quota check (around line 334):
```python
            if _is_quota_exhausted(returncode, stdout, stderr):
                breaker.record_failure()
                logger.warning(...)
                continue
```

In the streaming path, after detecting quota exhaustion from the first status message (around line 276-280), add:
```python
                if _is_quota_exhausted(rc, "", stderr_content) or rc != 0:
                    if _is_quota_exhausted(rc, "", stderr_content):
                        breaker.record_failure()
                    ...
```

**Verification:**
```bash
cd /home/gpav/Vrac/LAB/AI/LLM-Routing
python3 -c "
from router.circuit_breaker import get_breaker
from router.agy_proxy import try_agy_proxy
# Test that breaker blocks agy after 3 failures
import asyncio
b = get_breaker()
# Force tier 3 with 5h cooldown
b.tier = 3
b.cooldown_until = __import__('time').time() + 18000
b.probe_granted = False
result = asyncio.run(try_agy_proxy('test prompt'))
assert result is None, 'Breaker should return None when blocked'
print('Breaker integration verified')
"
```

### Task A3: Add circuit breaker status to the dashboard

**Objective:** Show the circuit breaker state on the `/dashboard` endpoint so the user can see when agy is blocked and when it will retry.

**Files:**
- Modify: `/home/gpav/Vrac/LAB/AI/LLM-Routing/router/main.py` (dashboard endpoint, around line 1122)

**Step 1: Add import near top of `main.py` (around line 17)**
```python
from circuit_breaker import get_breaker
```

**Step 2: Add breaker status to the dashboard endpoint**

In the `get_dashboard()` function, after the OAuth status section (around line 1130), add:
```python
    # Circuit breaker status
    breaker_status = get_breaker().status()
```

**Step 3: Add an HTML card in the dashboard**

Add a card in the dashboard HTML showing:
- Current tier (0 = open, 1-3 = cooldown)
- Whether agy is currently allowed
- Remaining cooldown time
- Total trip count

Position it in the right column, near the OAuth banner or health checks.

Use a color-coded card:
- Tier 0: green border ("agy Active")
- Tier 1: amber border ("agy Cooling Down — 5 min")
- Tier 2: orange border ("agy Cooling Down — 30 min")  
- Tier 3: red border ("agy BLOCKED — 5 hour cooldown")

**Verification:**
```bash
# Restart the router and visit http://localhost:5000/dashboard
# Verify the circuit breaker card appears and shows correct state
```

### Task A4: Integration test for circuit breaker

**Objective:** Write a test script that simulates quota exhaustion and verifies the breaker advances through tiers correctly.

**Files:**
- Create: `/home/gpav/Vrac/LAB/AI/LLM-Routing/test_circuit_breaker.py`

**Complete code:**

```python
#!/usr/bin/env python3
"""
Integration test for the agy circuit breaker.

Simulates 4 consecutive quota failures and verifies:
  - Tier 1 cooldown (5 min) after 1st failure
  - Tier 2 cooldown (30 min) after 2nd failure  
  - Tier 3 cooldown (5 hours) after 3rd failure
  - Probe behavior: one allowed attempt after cooldown
  - Reset to Tier 0 on success
  - Stay at Tier 3 on repeated failure
"""

import sys
import time
sys.path.insert(0, '/home/gpav/Vrac/LAB/AI/LLM-Routing/router')

from circuit_breaker import get_breaker, TIER_COOLDOWNS, MAX_TIER


def test_initial_state():
    """Breaker starts at Tier 0 (open)."""
    b = get_breaker()
    b.tier = 0
    b.cooldown_until = 0
    b.probe_granted = False
    b.total_trips = 0
    assert b.is_allowed() == True
    assert b.tier == 0
    print("✓ Initial state: Tier 0, agy allowed")


def test_first_failure_trips_to_tier1():
    """1st failure → Tier 1, 5 min cooldown."""
    b = get_breaker()
    b.tier = 0
    b.cooldown_until = 0
    b.probe_granted = False
    b.record_failure()
    assert b.tier == 1, f"Expected tier 1, got {b.tier}"
    assert b.cooldown_until > time.time(), "Cooldown should be set"
    assert b.is_allowed() == False, "Should block during cooldown"
    print("✓ 1st failure → Tier 1 (5 min cooldown)")


def test_probe_granted_after_cooldown():
    """After cooldown expires, exactly one probe is allowed."""
    b = get_breaker()
    b.tier = 1
    b.cooldown_until = time.time() - 10  # expired 10s ago
    b.probe_granted = False
    assert b.is_allowed() == True, "Probe should be granted"
    assert b.probe_granted == True, "Probe flag should be set"
    assert b.is_allowed() == False, "Second call should be denied"
    print("✓ Probe granted after cooldown expiry, consumed on next check")


def test_probe_failure_advances_tier():
    """Probe failure → advance to next tier."""
    b = get_breaker()
    b.tier = 1
    b.cooldown_until = time.time() - 10
    b.probe_granted = True  # probe was granted
    b.record_failure()  # probe fails
    assert b.tier == 2, f"Expected tier 2, got {b.tier}"
    assert b.probe_granted == False
    print("✓ Failed probe at Tier 1 → advanced to Tier 2 (30 min)")


def test_tier3_stays_at_tier3():
    """At Tier 3, failure → stays at Tier 3 (renews cooldown)."""
    b = get_breaker()
    b.tier = MAX_TIER
    b.cooldown_until = time.time() - 10
    b.probe_granted = True
    old_until = b.cooldown_until
    b.record_failure()
    assert b.tier == MAX_TIER, "Should stay at Tier 3"
    assert b.cooldown_until > old_until, "Cooldown should be renewed"
    assert b.probe_granted == False
    print("✓ Tier 3 failure → stays at Tier 3 (renews 5-hour cooldown)")


def test_success_resets():
    """Success at any tier → reset to Tier 0."""
    b = get_breaker()
    b.tier = 2
    b.cooldown_until = time.time() + 1000
    b.probe_granted = False
    b.record_success()
    assert b.tier == 0
    assert b.is_allowed() == True
    print("✓ Success resets breaker to Tier 0 from any tier")


def test_full_cycle():
    """Complete cycle: success → 3 failures → probe success → reset."""
    b = get_breaker()
    b.tier = 0
    b.cooldown_until = 0
    b.probe_granted = False
    b.total_trips = 0

    # Operate normally
    assert b.is_allowed()
    b.record_success()
    assert b.tier == 0

    # 1st failure
    b.record_failure()
    assert b.tier == 1
    assert not b.is_allowed()

    # Simulate cooldown expiry
    b.cooldown_until = time.time() - 10
    assert b.is_allowed()  # probe granted
    b.record_failure()  # probe fails
    assert b.tier == 2

    # Simulate cooldown expiry
    b.cooldown_until = time.time() - 10
    assert b.is_allowed()  # probe granted
    b.record_failure()  # probe fails again
    assert b.tier == 3
    assert TIER_COOLDOWNS[3] == 18000, "Tier 3 must be 5 hours"

    # Simulate cooldown expiry + probe success
    b.cooldown_until = time.time() - 10
    assert b.is_allowed()  # probe granted
    b.record_success()  # probe succeeds
    assert b.tier == 0
    assert b.total_trips == 3

    print("✓ Full cycle: 3 failures → Tier 3 → probe success → reset")


if __name__ == "__main__":
    test_initial_state()
    test_first_failure_trips_to_tier1()
    test_probe_granted_after_cooldown()
    test_probe_failure_advances_tier()
    test_tier3_stays_at_tier3()
    test_success_resets()
    test_full_cycle()
    
    print("\n" + "=" * 60)
    print("  ALL CIRCUIT BREAKER TESTS PASSED ✓")
    print("=" * 60)
```

**Verification:**
```bash
cd /home/gpav/Vrac/LAB/AI/LLM-Routing
python3 test_circuit_breaker.py
# Expected: all 7 tests pass with ✓ markers
```

---

## Workstream B: Multi-Threaded host_agy_daemon (P0)

> **Problem:** `host_agy_daemon.py` uses `http.server.HTTPServer` which processes one request at a time. Concurrent complex queries serialize behind the daemon. Fix: replace with `ThreadingHTTPServer` (stdlib, zero new dependencies).

### Task B1: Replace HTTPServer with ThreadingHTTPServer

**Objective:** Make the daemon handle concurrent agy requests by switching to a threaded server.

**Files:**
- Modify: `/home/gpav/Vrac/LAB/AI/LLM-Routing/host_agy_daemon.py`

**Changes (3 lines):**

Line 7 — add import:
```python
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
```

Line 223 — replace `HTTPServer` with `ThreadingHTTPServer`:
```python
    server = ThreadingHTTPServer(('127.0.0.1', PORT), AgyDaemonHandler)
```

**Verification:**
```bash
# Restart daemon and send 2 concurrent requests:
python3 host_agy_daemon.py &
sleep 1
# Send two requests simultaneously
curl -s -X POST http://127.0.0.1:5005/run \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"sleep 5 && echo done1","timeout":10}' &
curl -s -X POST http://127.0.0.1:5005/run \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"echo done2","timeout":10}' &
wait
# Both should complete ~5s (not 10s serialized)
kill %1 2>/dev/null
```

---

## Workstream C: pgvector + Semantic Caching (P1)

> **Goal:** Enable PostgreSQL-backed vector storage for LiteLLM's semantic cache, dramatically improving cache hit rates over exact-match Valkey.

### Task C1: Enable pgvector extension in PostgreSQL

**Objective:** The PostgreSQL 16 container already ships with pgvector — just create the extension.

**Files:** None (CLI only)

**Command:**
```bash
podman exec agent-router-pod-postgres-db psql -U postgres -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

**Verification:**
```bash
podman exec agent-router-pod-postgres-db psql -U postgres -c "SELECT * FROM pg_extension WHERE extname='vector';"
# Expected: one row with extname='vector'
```

### Task C2: Configure LiteLLM for semantic caching

**Objective:** Add semantic cache configuration to LiteLLM that uses embedding similarity for cache lookups alongside the existing Valkey exact-match cache.

**Files:**
- Modify: `/home/gpav/Vrac/LAB/AI/LLM-Routing/litellm/config.yaml`

**Changes in `litellm_settings.cache_params`:**
```yaml
litellm_settings:
  detailed_debug: false
  callbacks: ["langfuse"]
  drop_params: true
  cache: true
  caching_backend: "redis"
  cache_params:
    mode: "default"
    ttl: 3600
    type: "redis"
    host: "127.0.0.1"
    port: 6379
    # --- NEW: Semantic caching (requires pgvector) ---
    supported_callbacks: ["langfuse"]
  # --- NEW: Vector store for semantic caching ---
  service_callbacks: ["langfuse"]
```

For LiteLLM's semantic cache, the configuration requires setting up a vector store. Add a new section to `config.yaml`:

```yaml
# Vector store configuration for semantic caching
# LiteLLM uses this to find similar past requests
vector_store_settings:
  store_type: "postgres"
  connection_string: "postgresql://postgres:postgres-local-pw-2026@127.0.0.1:5432/postgres"
  collection_name: "litellm_semantic_cache"
  embedding_model: "openai/text-embedding-3-small"  # uses OpenRouter key
```

> **Note:** Semantic caching via embeddings requires an embedding model. Using `openai/text-embedding-3-small` through OpenRouter costs very little (~$0.02 per 1M tokens). For zero-cost, a local embedding model can be added to llama-server but adds complexity.

**Verification:**
```bash
# Restart stack with --full-rebuild and check LiteLLM logs:
journalctl --user CONTAINER_NAME=agent-router-pod-litellm-gateway --no-pager | grep -i "semantic\|vector\|cache"
# Should show vector store initialization without errors
```

---

## Workstream D: LiteLLM Cooldown & Routing Tuning (P1)

> **Goal:** Reduce wasted latency by tightening LiteLLM's retry/cooldown parameters so failing deployments are quarantined faster, and healthy ones get more traffic.

### Task D1: Update LiteLLM router_settings

**Objective:** Tune the cooldown, failure threshold, and retry count for faster failover.

**Files:**
- Modify: `/home/gpav/Vrac/LAB/AI/LLM-Routing/litellm/config.yaml`

**Change the `router_settings` section:**
```yaml
router_settings:
  cooldown_time: 30       # 30s (was 5s) — quarantine failing models longer
  allowed_fails: 2        # 2 fails (was 3) — fail faster before cooldown
  num_retries: 1          # 1 retry (was 2) — each retry tries a different deployment
  routing_strategy: "latency-based-routing"
  fallbacks:
    - agent-complex-core: ["local-qwen-3.6"]
    - agent-simple-core: ["local-qwen-3.6"]
  # --- NEW: Pre-call health checks ---
  enable_pre_call_checks: true
```

**Verification:**
```bash
# Restart LiteLLM and verify config loaded:
journalctl --user CONTAINER_NAME=agent-router-pod-litellm-gateway --no-pager | grep -i "cooldown_time\|allowed_fails"
```

---

## Workstream E: Automated Monitoring Cron Jobs (P1)

> **Goal:** Hermes cron jobs that run unattended — health checks, roster refresh, quota monitoring.

### Task E1: Stack health check cron job

**Objective:** Every 5 minutes, verify all 5 containers in the pod are running and healthy. Alert if any are down.

**Cron job setup:**
```
Schedule: */5 * * * *
Prompt: "Check the LLM-Routing stack health. Run 'podman pod ps' and 'podman ps --pod --filter pod=agent-router-pod'. Verify all 5 containers (valkey-cache, litellm-gateway, llm-triage-router, postgres-db, langfuse-server) are in 'Running' state. Check journald logs for the last 2 minutes for any ERROR or FATAL lines. Check that port 5000 responds to a health check: curl -s http://127.0.0.1:5000/dashboard | head -1. If any container is down or endpoints unreachable, report exactly which ones and the last 10 relevant log lines. Keep the report SILENT if everything is healthy."
Skills: ["llm-routing-stack"]
Enabled toolsets: ["terminal", "web"]
```

### Task E2: OpenRouter model roster refresh

**Objective:** Daily at 6 AM, refresh the static agentic_scores.json and trigger a LiteLLM roster sync.

**Cron job setup:**
```
Schedule: 0 6 * * *
Prompt: "Refresh the OpenRouter free model roster for LLM-Routing. Steps: 1) curl https://openrouter.ai/api/v1/models and filter for models with pricing.prompt='0' and pricing.completion='0'. 2) For each free model found, check if it exists in /home/gpav/Vrac/LAB/AI/LLM-Routing/router/agentic_scores.json. If new free models are found, add them with a default score of 50.0 and note in your report. 3) Trigger a roster sync by restarting the triage router: 'podman restart agent-router-pod-llm-triage-router'. 4) Report: which new models were added, which models from the score file are no longer free on OpenRouter."
Skills: ["llm-routing-stack"]
Enabled toolsets: ["terminal", "web"]
```

### Task E3: agy quota monitoring

**Objective:** Every 30 minutes, check if the Google Cloud Code Assist quota is exhausted.

**Cron job setup:**
```
Schedule: */30 * * * *
Prompt: "Check the Google Cloud Code Assist quota status. Run: tail -50 /home/gpav/.gemini/antigravity-cli/cli.log | grep -i 'RESOURCE_EXHAUSTED\|429\|quota'. Also check the circuit breaker status at http://127.0.0.1:5000/dashboard (grep for 'agy_allowed' or circuit breaker tier). If quota is exhausted or breaker tier > 0, estimate when it will reset based on the 5-hour Google refresh window. If breaker tier is 3, note the exact cooldown remaining time. Report SILENT if quota is available."
Enabled toolsets: ["terminal", "web"]
```

### Task E4: Daily backup verification

**Objective:** Verify the backup script ran successfully and backup files are recent.

**Cron job setup:**
```
Schedule: 0 7 * * *
Prompt: "Verify LLM-Routing backups are healthy. Run: ls -la /home/gpav/Vrac/LAB/AI/LLM-Routing/backups/ | tail -20. Check that at least one .dump file and one .tar.gz file were created in the last 25 hours. Verify the latest .dump file is >0 bytes. Report status: healthy or what's missing."
Enabled toolsets: ["terminal"]
```

---

## Workstream F: Granular Routing Tiers (P2)

> **Goal:** Expand from 2 routing tiers (simple/complex) to 4, matching agentic tool categories to specialized models.

### Task F1: Define new routing tiers and update classifier grammar

**Objective:** Define 4 tiers: `agent-shell-core`, `agent-code-core`, `agent-reasoning-core`, `agent-simple-core`. Update the GGUF grammar in the classifier and the LiteLLM roster sync.

**Files:**
- Modify: `/home/gpav/Vrac/LAB/AI/LLM-Routing/router/config.yaml` (classifier system prompt + grammar)
- Modify: `/home/gpav/Vrac/LAB/AI/LLM-Routing/router/main.py` (classifier endpoint, roster sync)

**New grammar:**
```
root ::= "agent-shell-core" | "agent-code-core" | "agent-reasoning-core" | "agent-simple-core"
```

**New system prompt** (update in `config.yaml` `classification_rules.system_prompt`):
```
Analyze the user request and classify it into exactly one of these categories:
- agent-shell-core: the request is about running commands, file operations, git, or interpreting terminal/shell tool output
- agent-code-core: the request is about writing, editing, refactoring, or reviewing code (any language)
- agent-reasoning-core: the request requires complex reasoning, multi-step planning, architecture decisions, or debugging complex bugs
- agent-simple-core: the request is trivial — a simple lookup, formatting, short explanation, or greeting

Return ONLY the exact identifier string. No markdown, no explanation.
```

**Update the `classify_request()` grammar in `main.py` line ~187:**
```python
"grammar": 'root ::= "agent-shell-core" | "agent-code-core" | "agent-reasoning-core" | "agent-simple-core"'
```

**Update the classification decision logic (line ~210):**
```python
valid_targets = {"agent-shell-core", "agent-code-core", "agent-reasoning-core", "agent-simple-core"}
decision = content_clean if content_clean in valid_targets else "agent-reasoning-core"
```

### Task F2: Update LiteLLM roster sync for 4 tiers

**Objective:** In `sync_adaptive_router_roster()`, create 4 deployment groups instead of 2. Each group gets models optimized for its purpose.

**Files:**
- Modify: `/home/gpav/Vrac/LAB/AI/LLM-Routing/router/main.py` (function `sync_adaptive_router_roster`)

**Tier model assignments:**
```
agent-reasoning-core: Top 3 highest-score models (Nemotron Ultra, Kimi K2.6, Nemotron Super)
agent-code-core:       Models with score 65-80 (Gemma 31B, DeepSeek V4 Flash, Laguna M.1)
agent-shell-core:      Fast/cheap models ≤ 65 (Nemotron Nano, Laguna XS.2, LFM 1.2B)
agent-simple-core:     Any remaining free models, speed-optimized
```

### Task F3: Update backends in router config.yaml

**Files:**
- Modify: `/home/gpav/Vrac/LAB/AI/LLM-Routing/router/config.yaml`

Add new backend entries:
```yaml
backends:
  - name: "agent-simple-core"
    api_base: "http://127.0.0.1:4000/v1"
    api_key: ""
  - name: "agent-shell-core"
    api_base: "http://127.0.0.1:4000/v1"
    api_key: ""
  - name: "agent-code-core"
    api_base: "http://127.0.0.1:4000/v1"
    api_key: ""
  - name: "agent-reasoning-core"
    api_base: "http://127.0.0.1:4000/v1"
    api_key: ""
```

### Task F4: Update agy proxy routing — complex → reasoning-core

**Objective:** The agy proxy currently triggers for `agent-complex-core`. Map it to `agent-reasoning-core` in the new scheme.

**Files:**
- Modify: `/home/gpav/Vrac/LAB/AI/LLM-Routing/router/main.py` (line ~839, the agy trigger condition)

```python
# Replace: if target_model == "agent-complex-core":
if target_model == "agent-reasoning-core":
```

### Task F5: Update LiteLLM fallback config for new tiers

**Files:**
- Modify: `/home/gpav/Vrac/LAB/AI/LLM-Routing/litellm/config.yaml`

```yaml
router_settings:
  fallbacks:
    - agent-reasoning-core: ["agent-code-core", "openrouter-auto", "local-qwen-3.6"]
    - agent-code-core: ["agent-shell-core", "openrouter-auto", "local-qwen-3.6"]
    - agent-shell-core: ["agent-simple-core", "openrouter-auto", "local-qwen-3.6"]
    - agent-simple-core: ["openrouter-auto", "local-qwen-3.6"]
```

---

## Workstream G: Prometheus /metrics Endpoint (P2)

> **Goal:** Export router statistics in Prometheus format for Grafana dashboards and alerting.

### Task G1: Add /metrics endpoint to FastAPI router

**Objective:** Serve a Prometheus-compatible text format endpoint with all key metrics.

**Files:**
- Modify: `/home/gpav/Vrac/LAB/AI/LLM-Routing/router/main.py`

**Add after the dashboard endpoint (around line 1120):**

```python
@app.get("/metrics")
async def get_metrics():
    """Prometheus-compatible metrics endpoint."""
    breaker = get_breaker().status()
    
    lines = [
        "# HELP triage_requests_total Total requests processed by triage router",
        "# TYPE triage_requests_total counter",
        f"triage_requests_total {stats['total_requests']}",
        "",
        "# HELP triage_simple_ratio Ratio of simple classifications",
        "# TYPE triage_simple_ratio gauge",
        f"triage_simple_ratio {stats.get('simple_requests', 0) / max(1, stats['total_requests'])}",
        "",
        "# HELP triage_cache_hits_total Total triage cache hits",
        "# TYPE triage_cache_hits_total counter",
        f"triage_cache_hits_total {stats.get('cache_hits', 0)}",
        "",
        "# HELP triage_latency_ms Average triage latency",
        "# TYPE triage_latency_ms gauge",
        f"triage_latency_ms {stats.get('avg_triage_latency_ms', 0):.1f}",
        "",
        "# HELP proxy_latency_ms Average proxy latency",
        "# TYPE proxy_latency_ms gauge",
        f"proxy_latency_ms {stats.get('avg_proxy_latency_ms', 0):.1f}",
        "",
        "# HELP tokens_prompt_total Total prompt tokens",
        "# TYPE tokens_prompt_total counter",
        f"tokens_prompt_total {stats.get('prompt_tokens', 0)}",
        "",
        "# HELP tokens_completion_total Total completion tokens",
        "# TYPE tokens_completion_total counter",
        f"tokens_completion_total {stats.get('completion_tokens', 0)}",
        "",
        "# HELP circuit_breaker_tier Current circuit breaker tier (0=open)",
        "# TYPE circuit_breaker_tier gauge",
        f"circuit_breaker_tier {breaker['tier']}",
        "",
        "# HELP circuit_breaker_agy_allowed Whether agy proxy is currently allowed (1=yes)",
        "# TYPE circuit_breaker_agy_allowed gauge",
        f"circuit_breaker_agy_allowed {1 if breaker['agy_allowed'] else 0}",
        "",
        "# HELP circuit_breaker_trips_total Total circuit breaker trips",
        "# TYPE circuit_breaker_trips_total counter",
        f"circuit_breaker_trips_total {breaker['total_trips']}",
        "",
        "# HELP circuit_breaker_cooldown_remaining_seconds Cooldown remaining",
        "# TYPE circuit_breaker_cooldown_remaining_seconds gauge",
        f"circuit_breaker_cooldown_remaining_seconds {breaker['cooldown_remaining_seconds']}",
        "",
        "# HELP routing_path_total Requests per routing path",
        "# TYPE routing_path_total counter",
    ]
    
    for path_name, count in stats.get("routing_paths", {}).items():
        lines.append(f'routing_path_total{{path="{path_name}"}} {count}')
    
    return Response(content="\n".join(lines) + "\n", media_type="text/plain")
```

**Verification:**
```bash
curl http://127.0.0.1:5000/metrics
# Expected: Prometheus-formatted text output with all metrics
```

---

## Workstream H: Systemd-ize host_agy_daemon (P2)

> **Goal:** The host daemon should start automatically on boot and restart on failure.

### Task H1: Create systemd user service

**Objective:** Wrap `host_agy_daemon.py` in a resilient systemd user service.

**Files:**
- Create: `/home/gpav/.config/systemd/user/agy-daemon.service`

**Complete unit file:**

```ini
[Unit]
Description=Host agy Daemon for LLM-Routing Gateway
Documentation=https://github.com/gpav/LLM-Routing
After=network.target
Wants=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/gpav/Vrac/LAB/AI/LLM-Routing/host_agy_daemon.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=agy-daemon

# Security hardening
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/gpav/.gemini /tmp
ReadOnlyPaths=/home/gpav/.local/bin/agy

[Install]
WantedBy=default.target
```

**Verification:**
```bash
systemctl --user daemon-reload
systemctl --user enable --now agy-daemon.service
systemctl --user status agy-daemon.service
# Verify: active (running)
curl -s -X POST http://127.0.0.1:5005/run \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"echo hello","timeout":10}'
# Should return JSON with returncode=0 and stdout="hello"
```

### Task H2: Update start-stack.sh to check daemon health

**Objective:** Add a pre-flight check in `start-stack.sh` that verifies `agy-daemon.service` is running before deploying the pod.

**Files:**
- Modify: `/home/gpav/Vrac/LAB/AI/LLM-Routing/start-stack.sh`

Add after the agy check section (around line 54):
```bash
# Check host agy daemon
if systemctl --user is-active --quiet agy-daemon.service 2>/dev/null; then
    echo "✓ Host agy daemon is running"
else
    echo "⚠️  Warning: Host agy daemon is not running. Starting it..."
    systemctl --user start agy-daemon.service || echo "⚠️  Failed to start agy daemon"
fi

# Verify daemon is responsive
if curl -s --max-time 2 http://127.0.0.1:5005/run >/dev/null 2>&1; then
    echo "✓ Host agy daemon responsive on port 5005"
else
    echo "⚠️  Warning: Host agy daemon not responding on port 5005"
fi
```

---

## Workstream I: HAProxy + Consul Distributed Setup (P3)

> **Goal:** Prepare for horizontal scaling by deploying the already-configured HAProxy + Consul templates.

### Task I1: Verify and complete Consul template

**Files:**
- Check: `/home/gpav/Vrac/LAB/AI/LLM-Routing/distributed/consul/consul-template.hcl`

If file is empty/missing, create:

```hcl
consul {
  address = "127.0.0.1:8500"
  retry {
    enabled  = true
    attempts = 12
    backoff  = "250ms"
  }
}

template {
  source      = "/home/gpav/Vrac/LAB/AI/LLM-Routing/distributed/haproxy/haproxy.cfg.ctmpl"
  destination = "/etc/haproxy/haproxy.cfg"
  command     = "systemctl reload haproxy"
}
```

### Task I2: Create podman-run script for Consul dev agent

**Files:**
- Create: `/home/gpav/Vrac/LAB/AI/LLM-Routing/distributed/start-consul-dev.sh`

```bash
#!/bin/bash
# Development Consul agent for service discovery
podman run -d --name consul-dev \
  --network host \
  -v /home/gpav/Vrac/LAB/AI/LLM-Routing/distributed/consul:/consul/config \
  docker.io/hashicorp/consul:latest \
  agent -dev -client=0.0.0.0 -config-dir=/consul/config
```

### Task I3: Document distributed deployment steps

**Files:**
- Create: `/home/gpav/Vrac/LAB/AI/LLM-Routing/distributed/README.md`

Brief doc covering: prerequisites (Consul, HAProxy installed on host), startup sequence, how to register triage router instances with Consul, and how HAProxy auto-discovers them via consul-template.

---

## Workstream J: Fallback Diversification (P3)

> **Goal:** Add a cheap paid OpenRouter model as intermediate fallback before local Qwen, reducing load on the APU during cascade failures.

### Task J1: Add paid fallback deployment to LiteLLM config

**Objective:** Register a cheap-but-reliable paid model (e.g., `google/gemini-2.5-flash-lite` at ~$0.00015/1K tokens) as an intermediate fallback tier.

**Files:**
- Modify: `/home/gpav/Vrac/LAB/AI/LLM-Routing/litellm/config.yaml`

Add to `model_list`:
```yaml
  - model_name: paid-fallback
    litellm_params:
      model: openrouter/google/gemini-2.5-flash-lite
      request_timeout: 60
```

Update fallbacks:
```yaml
router_settings:
  fallbacks:
    - agent-complex-core: ["openrouter-auto", "paid-fallback", "local-qwen-3.6"]
    - agent-simple-core: ["openrouter-auto", "paid-fallback", "local-qwen-3.6"]
```

### Task J2: Add openrouter/auto fallback to LiteLLM model list

**Objective:** Register `openrouter/auto` — OpenRouter's native smart router that auto-selects the best model for each request — as a deployment in LiteLLM. This sits *before* any paid model and *after* the dynamic free-model roster, providing an always-available fallback that doesn't depend on your manually-curated roster.

**Files:**
- Modify: `/home/gpav/Vrac/LAB/AI/LLM-Routing/litellm/config.yaml`

Add to `model_list`:
```yaml
  - model_name: openrouter-auto
    litellm_params:
      model: openrouter/openrouter/auto
      request_timeout: 120
```

> **What `openrouter/auto` does:** OpenRouter's API automatically routes the request to the best available model based on price, latency, and quality — using their real-time model availability data. No manual roster maintenance needed. This is a perfect "always works" fallback. Note: may select paid models if free ones are unavailable, so it costs a small amount. If you want strictly free auto-routing, append `:free` → `openrouter/openrouter/auto:free`.

**Complete updated fallback chain (after all workstreams applied):**
```
Triage Router classifies → sends to LiteLLM as agent-*-core
  └── LiteLLM latency-based-routing across dynamic OpenRouter free roster (5 models)
      ├── openrouter/auto (OpenRouter's own smart router — any model, any price)
      ├── paid-fallback (e.g., gemini-2.5-flash-lite, ~$0.00015/1K tokens)
      └── local-qwen-3.6 (Vulkan-accelerated Ryzen APU — last resort)
```

> **Note:** This requires `OPENROUTER_API_KEY` already set in `.env` and a small credit balance on OpenRouter. Even $1 covers ~6M tokens at this model's pricing.

**Verification:**
```bash
# Test the paid model works:
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $(grep LITELLM_MASTER_KEY .env | cut -d= -f2 | tr -d '"')" \
  -H "Content-Type: application/json" \
  -d '{"model":"paid-fallback","messages":[{"role":"user","content":"Hi"}],"max_tokens":10}'
```

---

## Dependency Graph

```
A1 (breaker module) ──┬── A2 (integrate into agy_proxy)
                      ├── A3 (dashboard card)
                      └── A4 (tests)

B1 (threaded daemon) — independent

C1 (pgvector) ── C2 (semantic cache config)

D1 (LiteLLM cooldown) — independent

E1-E4 (cron jobs) — independent (can run in parallel)

F1 (grammar) ── F2 (roster) ── F3 (backends) ── F4 (agy mapping) ── F5 (fallbacks)

G1 (Prometheus endpoint) — independent

H1 (systemd service) ── H2 (start-stack check)

I1 (Consul template) ── I2 (dev agent script) ── I3 (docs)

J1 (paid fallback) ── J2 (openrouter/auto) — sequential
```

## Recommended Delegation Order

| Batch | Workstreams | Parallelizable | Est. Time |
|-------|------------|----------------|-----------|
| 1 | A1, B1 | Yes (different files) | 10 min |
| 2 | A2, A3, A4, C1 | Yes (A depends on A1, C1 is CLI) | 15 min |
| 3 | C2, D1, H1 | Yes (different files) | 10 min |
| 4 | E1-E4, G1 | Yes (cron + metrics) | 10 min |
| 5 | F1-F5 | Sequential within F | 20 min |
| 6 | H2, J1, J2 | Yes | 5 min |
| 7 | I1-I3 | Sequential within I | 10 min |

**Total estimated implementation time:** ~80 minutes

---

## Pre-Flight Checklist

- [ ] Pod `agent-router-pod` is running (`podman pod ps`)
- [ ] OpenRouter API key is valid (test: `curl -s https://openrouter.ai/api/v1/auth/key -H "Authorization: Bearer $OPENROUTER_API_KEY"`)
- [ ] agy is authenticated (`agy --print "Hello"` returns a response)
- [ ] `host_agy_daemon.py` not currently running on port 5005 (will be systemd-ized)
- [ ] PostgreSQL container is healthy (`podman exec agent-router-pod-postgres-db pg_isready -U postgres`)
- [ ] Backup taken before starting (`bash scripts/backup.sh`)

## Post-Implementation Verification

- [ ] All 7 circuit breaker tests pass (`python3 test_circuit_breaker.py`)
- [ ] Dashboard at `http://localhost:5000/dashboard` shows circuit breaker status card
- [ ] `http://localhost:5000/metrics` returns Prometheus-format metrics
- [ ] Two concurrent `curl` requests to `:5005/run` complete in parallel (not serialized)
- [ ] `pgvector` extension exists in PostgreSQL
- [ ] LiteLLM starts without errors after config changes
- [ ] All 4 cron jobs visible in `hermes cron list`
- [ ] `systemctl --user status agy-daemon.service` shows `active (running)`
- [ ] agy proxy test: send a complex request through port 5000, verify it reaches agy or falls through gracefully
