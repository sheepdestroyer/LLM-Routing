## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.
## 2024-05-24 - [Avoid copy.deepcopy for Large Dict Serialization]
**Learning:** For large JSON-like dictionaries, `copy.deepcopy()` is incredibly slow due to Python object overhead and cyclic reference checks. Serializing and deserializing via a fast C-based library like `orjson` is significantly faster (e.g., 7x speedup for 1000 items) and provides a clean deep copy.
**Action:** When working with large cache dicts that require a deep copy before returning to prevent mutation by the caller, prefer `orjson.loads(cached_bytes)` over storing the dict and calling `copy.deepcopy(cached_dict)`.
