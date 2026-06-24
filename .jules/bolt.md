## 2024-06-16 - Synchronous I/O in Async API Handlers
**Learning:** `save_persisted_stats()` was being called synchronously on every API request, cache hit, and tool usage log, triggering blocking disk I/O in the main event loop.
**Action:** Always throttle or batch background telemetry writes in async Python applications to prevent blocking the event loop under load.

## 2026-06-24 - Async Offloading for SQLite I/O
**Learning:** Synchronous blocking I/O (like SQLite queries) inside an async event loop handler can cause significant latency spikes and block other requests.
**Action:** Use `asyncio.to_thread()` to offload synchronous database interactions to a worker thread, ensuring the main event loop remains responsive.


## 2024-06-25 - [Async Event Loop Optimization via to_thread]
**Learning:** Python's standard `open()` and file I/O operations (like `json.dump`) are synchronous and will block the entire asyncio event loop if called directly within an `async def` function. This can cause severe performance regressions in asynchronous services like FastAPI, especially when dealing with slow disk I/O.
**Action:** When performing file system operations inside an `async` context, wrap the synchronous I/O operations inside a helper function and execute it concurrently using `await asyncio.to_thread(func)`. This offloads the blocking calls to a separate thread pool, keeping the main event loop responsive.
