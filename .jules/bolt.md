## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.

## 2024-05-21 - [str.startswith Performance with generator expressions vs tuples]
**Learning:** Checking multiple string prefixes using a generator expression like `any(s.startswith(p) for p in prefixes)` executes entirely in Python user-space and has overhead. Passing a tuple directly to `str.startswith()` (e.g., `s.startswith(tuple_of_prefixes)`) is natively supported in C, skipping generator overhead and resulting in up to 7x faster execution.
**Action:** Always pass tuples directly to `str.startswith()` or `str.endswith()` instead of wrapping them in `any()` loops.

## 2024-05-21 - [PEP-8 and type() is vs isinstance()]
**Learning:** While `type(obj) is type` can be nanoseconds faster than `isinstance(obj, type)` because it skips MRO traversal, it violates PEP-8 ("Object type comparisons should always use isinstance()") and introduces functional regression risks by failing on valid subclasses (like OrderedDict). It provides no measurable real-world performance improvement for applications like HTTP routers.
**Action:** Never replace `isinstance()` with strict `type() is` for micro-optimizations. Correctness and idiomatic code take precedence over negligible nanosecond gains.
