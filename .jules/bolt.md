## 2024-06-24 - [Test implementation of detect_active_tool]
**Learning:** `router/main.py` requires configuration context (`CONFIG_PATH`) to be set, otherwise importing it for unit tests throws an error since it attempts to read config upon importing.
**Action:** Always mock or provide required environment variables such as `CONFIG_PATH` before importing functions from `router/main.py`.
