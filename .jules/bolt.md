## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.

## 2024-05-18 - [Optimization: Tuple passed to startswith]
**Learning:** For Python performance optimization when checking if a string starts with multiple prefixes, pass a tuple directly to `str.startswith()` (e.g., `s.startswith(prefix_tuple)`) rather than using a generator expression with `any()`.
**Action:** Use tuple directly in `str.startswith` to utilize native C implementation and avoid generator overhead.
