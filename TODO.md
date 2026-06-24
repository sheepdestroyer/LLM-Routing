# TODO: Complete Fix for Issue #41 (Async Dashboard Updates)

This list outlines the remaining steps to fully resolve the performance degradation and UX issues caused by full-page reloads on the LLM-Routing dashboard:

- [ ] **Strip the Global Page Reloader**
  - Locate the `<script>` tag in `router/main.py` (around line 2888).
  - Remove the destructive `window.location.reload()` page reloader.

- [ ] **Implement Fetch-based Updates in Dashboard HTML**
  - Add an asynchronous JavaScript function (e.g., `refreshStats()`) inside the dashboard HTML template.
  - Implement a `fetch("/api/dashboard-stats")` call inside `refreshStats()`.
  - Set a `setInterval(refreshStats, 3000)` to execute periodic fetches.
  - Call `refreshStats()` once on `DOMContentLoaded` for immediate population.

- [ ] **Implement Focused DOM Updates in Callback**
  - Update specific UI elements dynamically inside the fetch success callback:
    - **Infrastructure Node Statuses**: litellm, valkey, llama_server, langfuse.
    - **Dashboard Metrics**: Total API calls, last triage split, avg triage time, avg proxy time, cache hits.
    - **Dynamic HTML Tables/Sections**: Update the Goose, Valkey, Langfuse, LiteLLM UIs, active slots, and active models sections dynamically using the pre-computed HTML snippets returned by the API.

- [ ] **Enhancement: Migrate Telemetry `stats` to Valkey/Redis**
  - Migrate the global in-memory `stats` dictionary to Redis/Valkey to ensure consistent metrics across multi-worker server environments (e.g., using `uvicorn` with multiple workers).
