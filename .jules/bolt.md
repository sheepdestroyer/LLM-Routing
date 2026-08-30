## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.
## 2025-05-20 - Faster deep copies with `orjson` caching
**Learning:** Returning `copy.deepcopy()` on large cached dictionaries is significantly slower than storing the raw JSON bytes and returning `orjson.loads(cached_bytes)` when protecting cached data from caller mutation.
**Action:** When a cache needs to hand out mutable dicts without exposing its internal state, store the raw `bytes` and parse them on-demand with a fast JSON parser like `orjson` instead of using Python's `copy.deepcopy`.
