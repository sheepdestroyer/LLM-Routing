## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.

## 2024-08-23 - [O(1) LRU Eviction using itertools.islice]
**Learning:** For caches where dictionary insertion order is maintained (like Python 3.7+ dictionaries), evicting the oldest entries using `list(itertools.islice(d.keys(), excess))` provides O(1) memory overhead because it doesn't materialize all the keys in memory like `list(d.keys())[:excess]` does.
**Action:** Always prefer `itertools.islice` over list slicing when dealing with dictionary views to save memory overhead during cache eviction.

## 2024-08-23 - [Fast string prefix matching using tuple in str.startswith()]
**Learning:** Python's `str.startswith()` method natively accepts a tuple of strings. Passing a tuple directly (e.g., `s.startswith(prefix_tuple)`) runs the check entirely in optimized C code, avoiding the Python-level generator overhead of expressions like `any(s.startswith(p) for p in prefix_list)`.
**Action:** Always use `str.startswith(tuple)` instead of `any()` generator expressions for matching multiple prefixes to improve execution speed.
