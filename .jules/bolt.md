## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.
## 2024-05-20 - [Fast Immutable Deep Copies via orjson]
**Learning:** Storing dictionaries in an application cache often requires `copy.deepcopy()` to prevent mutation by callers. However, caching the raw JSON bytes and returning `orjson.loads(cached_bytes)` is significantly faster and enforces immutability with zero deep copy overhead.
**Action:** When a cache must return deeply mutable data structures that callers shouldn't alter, consider caching the serialized bytes instead of Python objects and leveraging fast native decoders like `orjson`.
