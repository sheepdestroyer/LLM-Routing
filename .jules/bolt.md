## 2024-06-16 - Synchronous I/O in Async API Handlers
**Learning:** `save_persisted_stats()` was being called synchronously on every API request, cache hit, and tool usage log, triggering blocking disk I/O in the main event loop.
**Action:** Always throttle or batch background telemetry writes in async Python applications to prevent blocking the event loop under load.
