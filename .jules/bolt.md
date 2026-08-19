## 2024-05-20 - [O(1) LRU Eviction using Python Dict Insertion Order]
**Learning:** Python 3.7+ dictionaries maintain insertion order. For caches where hits don't update the timestamp (like our `triage_cache`), the dictionary is naturally ordered from oldest to newest. We can avoid O(N log N) sorting for TTL and LRU evictions by popping elements off the front using `list(dict.keys())[:excess]`.
**Action:** Always consider dictionary insertion order before using `sorted()` or `heapq` for eviction logic in TTL caches without touch-on-read mechanics.

## 2023-10-27 - Batch DB Operations
**Learning:** I encountered an N+1 query loop when deleting items from the database via `asyncpg`. When dealing with PostgreSQL matching multiple strings or patterns, `asyncpg` seamlessly supports passing Python `list` types to a single placeholder (`$1`) when using `LIKE ANY($1)` or `= ANY($1)`. This avoids string interpolation vulnerabilities while condensing N round-trip queries into a single batched operation.
**Action:** Always batch PostgreSQL operations inside loops using `ANY($1)` and arrays/lists to minimize connection and query round-trip overhead.
