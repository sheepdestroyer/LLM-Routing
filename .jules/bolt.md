## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by simply popping elements off the front using `next(iter(dict))`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.
