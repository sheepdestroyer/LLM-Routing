## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.
## 2024-05-20 - [Avoid multiple lines.append() for metrics rendering]
**Learning:** In hot endpoints like `/metrics` that construct large text responses, using over 50 consecutive `lines.append()` calls introduces significant function call overhead and dynamic array resizing in Python.
**Action:** Always prefer a single list literal initialization `lines = [...]` for known strings, formatting them in place to improve CPU efficiency and reduce memory fragmentation.
