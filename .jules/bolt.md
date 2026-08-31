## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.
## 2024-05-17 - Parallelize HTTP DB Registrations
**Learning:** Sequential HTTP calls inside Python `for` loops are a major performance bottleneck for initialization routines.
**Action:** Used `asyncio.gather` with a coroutine wrapper to dispatch N independent HTTP requests concurrently, reducing O(N) latency to roughly O(1).
