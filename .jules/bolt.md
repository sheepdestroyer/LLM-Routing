## 2024-06-16 - Synchronous I/O in Async API Handlers
**Learning:** `save_persisted_stats()` was being called synchronously on every API request, cache hit, and tool usage log, triggering blocking disk I/O in the main event loop.
**Action:** Always throttle or batch background telemetry writes in async Python applications to prevent blocking the event loop under load.

## 2026-06-24 - Async Offloading for SQLite I/O
**Learning:** Synchronous blocking I/O (like SQLite queries) inside an async event loop handler can cause significant latency spikes and block other requests.
**Action:** Use `asyncio.to_thread()` to offload synchronous database interactions to a worker thread, ensuring the main event loop remains responsive.
## 2026-06-24 - [Performance Improvement] Offload synchronous file I/O in async functions

**Learning:** When making code optimizations within async functions, avoid creating standalone tasks (`asyncio.create_task()`) for synchronous functions that execute blocking I/O if the return isn't needed, as this can trigger Task destruction errors, swallow exceptions, or throw RuntimeErrors if called outside of the main event loop context.

**Action:** Always prefer wrapping the blocking function call directly using `await asyncio.to_thread(func, args)` at the caller site within the async context rather than wrapping the synchronous function definition itself. This maintains safe concurrent behavior while avoiding task lifecycle/GC issues and event loop errors.
