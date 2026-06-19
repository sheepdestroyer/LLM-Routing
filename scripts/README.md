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
Sends sample prompts of varying complexity to `llm-routing-auto-ollama` and `llm-routing-ollama` to verify correct gating and routing:
- **Expected Routing (`llm-routing-auto-ollama`)**:
  - Simple $\rightarrow$ `agent-simple-core`
  - Complex $\rightarrow$ `ollama-deepseek-v4-flash`
  - Reasoning $\rightarrow$ `ollama-deepseek-v4-pro`
- **Expected Routing (`llm-routing-ollama`)**:
  - Simple/Complex $\rightarrow$ `ollama-deepseek-v4-flash`
  - Reasoning/Advanced $\rightarrow$ `ollama-deepseek-v4-pro`

### `scripts/verification/verify_ollama_cooldown.py`
Simulates fallback cascades to verify that failed Ollama requests activate the 5-minute router-side cooldown and correctly bypass LiteLLM to prevent crashloops.

### `scripts/verification/verify_direct_ollama_cooldown.py`
Asserts that direct requests to `llm-routing-ollama` immediately trigger the cooldown response without hammering downstream endpoints.

### `scripts/verification/mock_rate_limit_server.py`
A simple HTTP server that returns `429 Rate Limit Exceeded` to simulate rate limits when testing cooldowns.
- **Usage**: `python3 scripts/verification/mock_rate_limit_server.py` (Runs on `127.0.0.1:9999`)

---

## 3. Classifier & Dataset Maintenance (`scripts/`)

These tools are used to benchmark the prompt classifier and extract datasets from Langfuse traces:

- **`benchmark_classifier.py`**: Benchmarks latency and precision metrics of the Ryzen PRO APU-offloaded classifier.
- **`classify_direct.py`**: Takes a string prompt argument and prints the classification decision directly.
- **`extract_prompts.py` / `extract_complex.py` / `extract_gapfill.py`**: Mines prompt datasets from Langfuse PG/ClickHouse database traces for fine-tuning.
- **`reclassify_all.py`**: Re-evaluates prompt classifications against updated models.
- **`retry_errors.py`**: Retries failed queries.

---

## 4. Integration Test Suite (Root Directory)

- **`test_circuit_breaker.py`**: Unit/integration tests for the dual circuit breaker (`router/circuit_breaker.py`), covering independent Google/Vendor tiers and probe-granting logic.
- **`test_classifier_accuracy.py`**: Accuracy evaluation suite covering 25 system prompts.
- **`test_agy_tiers.py`**: Validates `agy` proxy model tier routing.
- **`test_agy_behavior.py`**: Asserts the behavior of the `agy` CLI client under quota limits.
- **`test_a2_verify.py`**: Quick sanity integration check for the agy proxy circuit breaker.
- **`test_antigravity.py`**: Tests the connection to the host Antigravity CLI daemon (`agentapi`).
- **`test_stream_latency.py`**: Measures Time-To-First-Token (TTFT) and token generation speed.
- **`test_quota_reset.sh`**: Simulates/triggers quota reset conditions.
- **`verify_breaker.py`**: Sanity verification check for the circuit breaker.
- **`watch_quota.sh`**: Watch/polling script for observing quota status.
