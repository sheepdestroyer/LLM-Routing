# 🧪 Add tests for memory value JSON encoding/decoding

## Description

🎯 **What:** The testing gap addressed was an untested memory JSON encoder `_memory_value` and its counterpart decoder `_parse_memory_value` within `router/memory_mcp.py`. These pure functions handle payload storage preparation but lacked verification.

📊 **Coverage:** The following scenarios are now tested:
- Happy paths for converting data and tags into serialized JSON strings.
- Edge cases where `tags` are provided as `None`.
- Unicode handling to ensure that `ensure_ascii=False` appropriately works without escaping characters.
- Decoding behavior of valid JSON, invalid JSON formats (catching `json.JSONDecodeError`), and TypeError handling for `_parse_memory_value`.

✨ **Result:** Test coverage for the core memory data encoding mechanism is significantly improved, preventing potential regressions during memory data restructuring. Tests successfully passed `pytest`.
