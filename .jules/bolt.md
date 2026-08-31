## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.
## 2024-05-22 - [JSON Deep Copy Optimization via Binary Caching]
**Learning:** In Python, calling `copy.deepcopy()` on a large nested dictionary is significantly slower than parsing raw JSON bytes via `orjson.loads()`.
**Action:** When caching JSON file contents that must be returned as fresh dictionaries to avoid caller mutations, cache the raw `bytes` from an `"rb"` file read. Then return `orjson.loads(cached_bytes)` instead of storing a dict and returning `copy.deepcopy(cached_dict)`.
