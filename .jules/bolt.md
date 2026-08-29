## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.
## 2023-10-24 - Deepcopy vs JSON Serialization
**Learning:** `copy.deepcopy()` is slow in Python, especially for large nested structures like cached dashboard annotations.
**Action:** When caching data that shouldn't be mutated by callers, it's ~10x faster to cache raw JSON bytes and return `orjson.loads(cached_bytes)` instead of parsing once and returning a `copy.deepcopy()` of the dictionary.
