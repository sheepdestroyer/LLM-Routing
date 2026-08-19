## 2024-05-18 - Caching LiteLLM Config Loading
**Learning:** Loading the same YAML file repeatedly in a short period (e.g. sequentially in different functions) is unnecessary overhead. Also, simple dictionary caching is thread-safe in Python because of the GIL.
**Action:** When data changes infrequently (like configuration files), introduce a module-level TTL cache to limit redundant disk I/O and parsing time.
