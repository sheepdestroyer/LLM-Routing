## 2024-08-13 - [Fast JSON Deserialization]
**Learning:** Using `orjson.loads` is significantly faster than `json.loads` for parsing JSON payloads. In hot loops such as SSE streaming responses processing (`chat_completions` function), the performance difference is substantial (e.g. 1.9s vs 6.3s on 1M iterations of small SSE chunks).
**Action:** Always prefer `orjson` over the standard `json` library for deserialization in high-throughput hot paths like response streamers.
