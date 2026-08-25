## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.

## 2024-05-20 - [str.startswith Performance with Tuples]
**Learning:** Checking multiple string prefixes using `any(s.startswith(p) for p in prefixes)` incurs significant generator expression overhead. Passing the tuple directly to `str.startswith(prefixes)` is supported natively in C by Python and is ~8-9x faster in microbenchmarks.
**Action:** Always pass tuples directly to string methods like `startswith` and `endswith` rather than iterating over prefixes in Python space.
