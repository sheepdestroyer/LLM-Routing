## 2024-08-13 - [Fast JSON Deserialization]
**Learning:** Using `orjson.loads` is significantly faster than `json.loads` for parsing JSON payloads. In hot loops such as SSE streaming responses processing (`chat_completions` function), the performance difference is substantial (e.g. 1.9s vs 6.3s on 1M iterations of small SSE chunks).
**Action:** Always prefer `orjson` over the standard `json` library for deserialization in high-throughput hot paths like response streamers.
## 2024-08-14 - [Orjson Refactoring]
**Learning:** When migrating from `json.dumps` to `orjson.dumps`, you must handle the fact that `orjson.dumps` returns `bytes` instead of a string. In contexts that expect a string (like `sys.stdout.write` or assigning to a dict key that is eventually serialized differently), you must append `.decode('utf-8')`. Additionally, `orjson` raises its own `orjson.JSONDecodeError`, so you must update exception handlers (e.g. from `except json.JSONDecodeError`) to avoid uncaught parse errors.
**Action:** Always append `.decode('utf-8')` when replacing `json.dumps` if the target expects a string, and audit `try-except` blocks around deserialization logic to catch `orjson.JSONDecodeError`.
## 2024-08-15 - Migrate from json.loads to orjson.loads
**Learning:** `orjson.loads` is significantly faster than `json.loads` (~4x faster on a sample string) and is already available as a dependency in the project (imported in `router/main.py`). The project is actively doing loads in hot paths (e.g. processing streaming JSON responses).
**Action:** Replace `json.loads` with `orjson.loads` globally where performance is critical, and ensure correct byte/string types are handled.
## 2024-08-16 - Optimize Prometheus metrics generation
**Learning:** Found multiple consecutive `lines.append()` calls inside `router/main.py`'s `metrics()` endpoint, which causes overhead in terms of repeated function calls and dynamic array resizing.
**Action:** Combined multiple string appending operations into a single list initialization, returning `\n.join(lines)`. This avoids reallocation overhead and is an O(N) generation instead of multiple O(1) appends. Next time, always check for repeated list appends in hot endpoints.
