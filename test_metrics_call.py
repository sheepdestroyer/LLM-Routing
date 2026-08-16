import asyncio
import os
import sys

# Fake out environment variables that the router needs
os.environ["CONFIG_PATH"] = "router/config.yaml"
os.environ["ROUTER_API_KEY"] = "test-key"
os.environ["LITELLM_MASTER_KEY"] = "test-key"
os.environ["LLAMA_CLASSIFIER_URL"] = "http://localhost:8080/v1"
os.environ["LITELLM_ADMIN_URL"] = "http://localhost:4000"

# Mock out redis so it doesn't fail
import unittest.mock
with unittest.mock.patch("router.main.get_redis", return_value=None):
    from router.main import metrics, stats, _ollama_cooldown_until

    async def main():
        resp = await metrics()
        print(resp.body.decode('utf-8'))

    asyncio.run(main())
