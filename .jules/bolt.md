## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.
## 2025-02-18 - Optimized N+1 HTTP Request in Ollama Model Registration
**Learning:** Sequential `await client.post(...)` inside a `for` loop blocks the event loop and takes linear time. Python's `asyncio` allows nested `async def` wrappers to maintain `nonlocal` counters (like `registered` and `failed`) safely without locking, because synchronous updates between `await` yields are atomic in a single-threaded event loop.
**Action:** When finding serial asynchronous network requests, wrap the block in an `async def` and execute with `asyncio.gather()` to make them concurrent. Use `nonlocal` to retain cleanly scoped metrics.
