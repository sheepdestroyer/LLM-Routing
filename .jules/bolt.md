## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.

## 2024-05-16 - [FastAPI SSE Streaming Optimization]
**Learning:** Using `orjson.dumps()` concatenated with byte strings is significantly faster than using Python's built-in `json.dumps()` wrapped in string formatting and encoded to UTF-8.
**Action:** Always prefer `orjson` and native byte manipulation in hot streaming loops (like SSE emitters) to reduce encoding overhead and string instantiation.
