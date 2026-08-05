import os
import importlib
from unittest.mock import patch
import pytest
import router.main


@pytest.fixture(autouse=True)
def reset_router_main():
    yield
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OLLAMA_COOLDOWN_SECONDS", None)
        importlib.reload(router.main)


@pytest.mark.parametrize("env_val, expected", [
    ("invalid", 300),
    ("-10", 300),
    ("0", 300),
    ("600", 600),
    (None, 300),
])
def test_ollama_cooldown_config(env_val, expected):
    env_changes = {}
    if env_val is not None:
        env_changes["OLLAMA_COOLDOWN_SECONDS"] = env_val

    with patch.dict(os.environ, env_changes):
        if env_val is None and "OLLAMA_COOLDOWN_SECONDS" in os.environ:
            del os.environ["OLLAMA_COOLDOWN_SECONDS"]
        importlib.reload(router.main)
        assert router.main.OLLAMA_COOLDOWN_SECONDS == expected
