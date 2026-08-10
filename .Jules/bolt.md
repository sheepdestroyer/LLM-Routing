## 2024-05-24 - Async Mocking with Multiple Calls
**Learning:** When mocking a function like `asyncio.to_thread` that gets called multiple times with different functions (e.g., `os.path.exists` and `yaml.safe_load`), a simple `Exception` side effect will fail the first call. Use a custom `side_effect` function to selectively raise exceptions based on the first argument (the wrapped function).
**Action:** When updating mocked behavior for repeated function calls, check if the arguments differ and route the mock's behavior using a custom side effect function.
