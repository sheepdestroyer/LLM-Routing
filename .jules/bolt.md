## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.
## 2024-05-19 - Fast Deep Copies via orjson
**Learning:** In Python, standard `copy.deepcopy()` is incredibly slow for large nested dictionaries and can be a significant bottleneck for cached data that callers are allowed to mutate.
**Action:** When caching Python dictionaries containing JSON-like data, serialize them to raw `bytes` using `orjson.dumps()` upon caching, and deserialize them on every read using `orjson.loads()`. It acts as a much faster deep copy (~8-10x faster) without any mutation bleeding issues.
