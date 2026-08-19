## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.

## 2024-05-18 - Caching LiteLLM Config Loading
**Learning:** Loading the same YAML file repeatedly in a short period (e.g. sequentially in different functions) is unnecessary overhead. Also, simple dictionary caching is thread-safe in Python because of the GIL. Note: be careful to deepcopy when returning cached config dictionaries if callers mutate them, but in this case we're only reading.
**Action:** When data changes infrequently (like configuration files), introduce a module-level TTL cache to limit redundant disk I/O and parsing time.
