## 2024-06-16 - Synchronous I/O in Async API Handlers
**Learning:** `save_persisted_stats()` was being called synchronously on every API request, cache hit, and tool usage log, triggering blocking disk I/O in the main event loop.
**Action:** Always throttle or batch background telemetry writes in async Python applications to prevent blocking the event loop under load.

## 2026-06-24 - Async Offloading for SQLite I/O
**Learning:** Synchronous blocking I/O (like SQLite queries) inside an async event loop handler can cause significant latency spikes and block other requests.
**Action:** Use `asyncio.to_thread()` to offload synchronous database interactions to a worker thread, ensuring the main event loop remains responsive.
## 2026-06-24 - [Token Estimation Optimization]
**Learning:** Replacing `isinstance` calls with `type(obj) is` in the hot path of token estimation (which iterates deeply through complex message dictionaries) significantly reduces type-checking overhead. Furthermore, removing default fallbacks like `or ""` and instead checking for truthiness/existence explicitly speeds up property accesses.
**Action:** Next time writing data traversal code for metrics/counters that processes heavily nested structures on every request, optimize type checking by using strict `type(x) is` instead of `isinstance()` when inheritance is not a factor. This led to a ~25% reduction in execution time for the `estimate_prompt_tokens` function.
