## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.
## 2025-02-18 - Type checking and Loop inlining
**Learning:** Python's `isinstance` adds significant overhead inside hot token counting loops processing deeply nested dictionaries. `type(x) is` checks are up to 40% faster for exact type matching.
**Action:** Prefer exact type checks and fully inlined computation loops when processing high-volume text iterations like token heuristics.
