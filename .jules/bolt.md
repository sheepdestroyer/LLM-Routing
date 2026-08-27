## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.

## 2024-05-20 - [Fast Deep Copy via Raw JSON Bytes]
**Learning:** For Python performance optimizations involving cached data that must not be mutated by callers, avoid storing dictionaries and returning `copy.deepcopy()`. Instead, cache the raw JSON bytes and return `orjson.loads(cached_bytes)` to achieve significantly faster deep copies. This is especially impactful for I/O bound caches where parsing from memory is faster than deep copying large dictionaries.
**Action:** When caching read-only data from disk (like JSON), cache the raw bytes and parse on read rather than caching the parsed dictionary and deep copying it on read.
