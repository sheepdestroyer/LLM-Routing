# Automation, Testing, and Verification Scripts

This directory and the repository root contain various scripts used for stack orchestration, database backups, routing verification, classifier training/benchmarking, and system integration testing.

---

## 1. Stack Orchestration & Backups

### `start-stack.sh` (Root Directory)
Unified startup and credential extraction script for the Podman Kubernetes container stack.
- **Usage**:
  - `./start-stack.sh` (Restart existing pod — fast, preserves logs)
  - `./start-stack.sh --replace` (Stop + clean ports + redeploy pod from `pod.yaml`)
  - `./start-stack.sh --full-rebuild` (Same as `--replace` + rebuild the triage router image; required for code changes in `router/`)

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

### `scripts/verification/verify_breaker.py`
Sanity verification check for the dual circuit breaker logic.

### `scripts/verification/mock_rate_limit_server.py`
A simple HTTP server that returns `429 Rate Limit Exceeded` to simulate rate limits when testing cooldowns.
- **Usage**: `python3 scripts/verification/mock_rate_limit_server.py` (Runs on `127.0.0.1:9999`)

---

## 3. Classifier, Daemons & Maintenance (`scripts/`)

These tools and helper daemons are used to benchmark the prompt classifier, extract datasets from Langfuse traces, and orchestrate client-host communication:

- **`benchmark_classifier.py`**: Benchmarks latency and precision metrics of the Ryzen PRO APU-offloaded classifier.
- **`classify_direct.py`**: Takes a string prompt argument and prints the classification decision directly.
- **`extract_prompts.py` / `extract_complex.py` / `extract_gapfill.py`**: Mines prompt datasets from Langfuse PG/ClickHouse database traces for fine-tuning.
- **`reclassify_all.py`**: Re-evaluates prompt classifications against updated models.
- **`retry_errors.py`**: Retries failed queries.
- **`host_agy_daemon.py`**: Real-time PTY-based streaming daemon for low-latency streaming for agent clients.
- **`sync_gemini_token.py`**: Extraction and sync script for keyring OAuth credentials.
- **`get_pr_status.py`**: PR status query helper.
- **`watch_quota.sh`**: Watch/polling script for observing quota status.
- **`test_quota_reset.sh`**: Simulates/triggers quota reset conditions.

---

## 4. Integration Test Suite (`tests/`)

The integration test suite is located in the `tests/` directory. Tests are categorized below based on their primary function:

### Circuit Breaker Tests
- **`tests/test_circuit_breaker.py`**: Unit/integration tests for the dual circuit breaker (`router/circuit_breaker.py`), covering independent Google/Vendor tiers and probe-granting logic.
- **`tests/test_a2_verify.py`**: Quick sanity integration check for the agy proxy circuit breaker.

### Classifier Tests
- **`tests/test_classifier_accuracy.py`**: Accuracy evaluation suite covering 25 system prompts.
- **`tests/test_map_tool_to_category.py`**: Tests prompt-classifier category mapping.

### Routing & Proxy Tests
- **`tests/test_agy_tiers.py`**: Validates `agy` proxy model tier routing.
- **`tests/test_antigravity.py`**: Tests the connection to the host Antigravity CLI daemon (`agentapi`).
- **`tests/test_models_proxy.py`**: Tests direct reverse-proxy and router routing mechanics.

### Performance & Monitoring Tests
- **`tests/test_stream_latency.py`**: Measures Time-To-First-Token (TTFT) and token generation speed.

### Simulation & Helper Daemon Tests
- **`tests/test_agy_behavior.py`**: Asserts the behavior of the `agy` CLI client under quota limits.
- **`tests/test_host_agy_daemon.py`**: Tests real-time streaming capabilities and connection handling of the host `agy` daemon.
- **`tests/test_sync_gemini_token.py`**: Tests OAuth credentials extraction and sync.

### Utility Tests
- **`tests/test_atomic_write.py`**: Tests atomic file writing logic used for config updates.
- **`tests/test_check_http_endpoint.py`**: Tests health check monitoring logic for HTTP endpoints.
- **`tests/test_compute_free_model_score.py`**: Tests local free model load and accuracy scoring.
- **`tests/test_pie_chart_gradient.py`**: Tests dynamic CSS gradient calculation for stats visualization.
- **`tests/test_record_tool_usage.py`**: Tests Valkey/Redis recording of LLM tool usage.
- **`tests/test_src_badge.py`**: Tests dynamic visual badge generation for source status.
