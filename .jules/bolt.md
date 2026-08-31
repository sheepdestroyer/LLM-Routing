## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.
## YYYY-MM-DD - Caching raw bytes for faster deserialization
**Learning:** In Python, caching raw JSON bytes and returning `orjson.loads()` on access is significantly faster than caching the parsed dictionary and returning `copy.deepcopy()`.
**Action:** For cached data that must not be mutated by callers, always cache the raw bytes and rely on fast deserialization instead of `copy.deepcopy()`.
