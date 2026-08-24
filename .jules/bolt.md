## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.
## 2024-08-24 - Python dict cleanup optimization
**Learning:** To evict the oldest entries from a Python 3.7+ dictionary cache with O(1) memory overhead, `list(itertools.islice(d.keys(), excess))` is significantly faster (and uses less memory) than `list(d.keys())[:excess]`. This avoids materializing all keys into a list before slicing.
**Action:** Use `itertools.islice` when evicting entries from a python dictionary based on insertion order instead of `list(d.keys())[:excess]`.
