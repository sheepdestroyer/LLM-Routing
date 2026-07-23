# Automation, Testing, and Verification Scripts

This directory and the repository root contain various scripts used for stack orchestration, database backups, routing verification, classifier training/benchmarking, and system integration testing.

---

## 1. Stack Orchestration & Backups

### `start-stack.sh` (Root Directory)
Unified startup and credential extraction script for the systemd Quadlet-managed Podman stack.
- **Usage**:
  - `./start-stack.sh` (Restart the generated `llm-routing-pod.service`)
  - `./start-stack.sh --replace` (Stop + clean ports + render/install Quadlets + daemon-reload + recreate stack)
  - `./start-stack.sh --full-rebuild` (Same as `--replace` + rebuild the triage router image; required for code changes in `router/`)
- Quadlet templates live in `quadlets/`; rendered owner-only units use environment-specific namespaces: dev under `~/.config/containers/systemd/llm-routing-dev/` and prod under `~/.config/containers/systemd/llm-routing-prod/`. Dev uses `llm-routing-dev-pod.service`; prod uses `llm-routing-prod-pod.service`. Use the matching `systemctl --user status <namespace>-pod.service --no-pager` and `journalctl --user -u <namespace>-router.service --no-pager` for lifecycle diagnostics.

### `scripts/backup.sh`
Automated database backup script that runs before every stack deployment. Uses `pg_isready` to safely wait for database connections and manages timestamped backups under `backups/`.

---

## 2. Routing & Cooldown Verification Scripts

These scripts are located in `scripts/verification/` and are used to assert that the router-side Ollama cooldowns and prompt-classification gating function correctly:

### `scripts/verification/verify_ollama_routing.py`
Sends sample prompts of varying complexity to `llm-routing-auto-ollama` and `llm-routing-ollama` to verify correct gating and routing.

> [!NOTE]
> Routing `agent-reasoning-core` to the Pro tier (`ollama-deepseek-v4-pro`) is an intentional design choice rather than routing it to the Flash tier. This ensures that reasoning-tier queries receive the highest accuracy and reasoning capabilities available in the Pro model group.

- **Expected Routing (`llm-routing-auto-ollama`)**:
  - Simple $\rightarrow$ `agent-simple-core`
  - Complex $\rightarrow$ `ollama-deepseek-v4-flash`
  - Reasoning $\rightarrow$ `ollama-deepseek-v4-pro` (Intentional design choice)
- **Expected Routing (`llm-routing-ollama`)**:
  - Simple/Complex $\rightarrow$ `ollama-deepseek-v4-flash`
  - Reasoning/Advanced $\rightarrow$ `ollama-deepseek-v4-pro` (Intentional design choice)

### `scripts/verification/verify_ollama_cooldown.py`
Simulates fallback cascades to verify that failed Ollama requests activate the router-side cooldown (configured by the OLLAMA_COOLDOWN_SECONDS environment variable) and correctly bypass LiteLLM to prevent crash loops.

### `scripts/verification/verify_direct_ollama_cooldown.py`
Asserts that direct requests to `llm-routing-ollama` immediately trigger the cooldown response without hammering downstream endpoints.

### `scripts/verification/mock_rate_limit_server.py`
A simple HTTP server that returns `429 Rate Limit Exceeded` to simulate rate limits when testing cooldowns.
- **Usage**: `python3 scripts/verification/mock_rate_limit_server.py` (Runs on `127.0.0.1:9999`)

---

## 3. Classifier & Dataset Maintenance (`scripts/`)

These tools are used to benchmark the prompt classifier and extract datasets from Langfuse traces:

- **`benchmark_classifier.py`**: Benchmarks latency and precision metrics of the Ryzen PRO APU-offloaded classifier.
- **`chat_helpers.py`**: Shared defensive chat completion response parser (`parse_chat_response`) used by classifier scripts, verification scripts, and canonical endpoint checks. Safely extracts content and reasoning_content with full isinstance guards.
- **`classify_direct.py`**: Takes a string prompt argument and prints the classification decision directly.
- **`extract_prompts.py` / `extract_complex.py` / `extract_gapfill.py`**: Mines prompt datasets from Langfuse PG/ClickHouse database traces for fine-tuning.
- **`reclassify_all.py`**: Re-evaluates prompt classifications against updated models.
- **`retry_errors.py`**: Retries failed queries.

---

## 4. Integration Test Suite

The integration test suite is located in the `tests/` and `scripts/` directories. Tests are categorized below based on their primary function:

### Circuit Breaker Tests
- **`tests/test_circuit_breaker.py`**: Unit/integration tests for the dual circuit breaker (`router/circuit_breaker.py`), covering independent Google/Vendor tiers and probe-granting logic.
- **`scripts/verification/verify_breaker.py`**: Sanity verification check for the circuit breaker.
- **`tests/test_a2_verify.py`**: Quick sanity integration check for the agy proxy circuit breaker.

### Classifier Tests
- **`tests/test_classifier_accuracy.py`**: Accuracy evaluation suite covering 25 system prompts.

### Routing & Proxy Tests
- **`tests/test_agy_tiers.py`**: Validates `agy` proxy model tier routing.
- **`tests/test_antigravity.py`**: Tests the connection to the host Antigravity CLI daemon (`agentapi`).
- **`router/tests/test_routing_behavior.py`**: Validates Qwen classifier prompt truncation and direct `llm-routing-agy` request fallback routing.
- **`scripts/verification/verify_reasoning_tiers.py`**: Sends prompts across all 5 triage tiers and validates routing to the expected model.

### Performance & Monitoring Tests
- **`tests/test_stream_latency.py`**: Measures Time-To-First-Token (TTFT) and token generation speed.

### Simulation Tests
- **`tests/test_agy_behavior.py`**: Asserts the behavior of the `agy` CLI client under quota limits.
- **`scripts/test_quota_reset.sh`**: Simulates/triggers quota reset conditions.
- **`scripts/watch_quota.sh`**: Watch/polling script for observing quota status.
