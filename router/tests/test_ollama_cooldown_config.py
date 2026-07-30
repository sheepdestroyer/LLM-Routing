import os
import sys
import tempfile
import subprocess
import pytest
import yaml

@pytest.fixture
def dummy_env():
    env = os.environ.copy()
    env["ROUTER_API_KEY"] = "test-key"
    env["LITELLM_MASTER_KEY"] = "test-key"
    env["LLAMA_CLASSIFIER_URL"] = "http://localhost:8080/v1"
    env["LITELLM_ADMIN_URL"] = "http://localhost:4000"

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".yaml") as f:
        yaml.dump({
            "server": {"host": "127.0.0.1"},
            "router": {"router_model": {"api_key": "test-key"}},
            "backends": [{"name": "test-backend"}]
        }, f)
        config_path = f.name

    env["CONFIG_PATH"] = config_path

    yield env

    os.remove(config_path)

@pytest.mark.parametrize("env_val, expected", [
    ("invalid", 300),
    ("-10", 300),
    ("0", 300),
    ("600", 600),
    (None, 300),
])
def test_ollama_cooldown_config(dummy_env, env_val, expected):
    if env_val is not None:
        dummy_env["OLLAMA_COOLDOWN_SECONDS"] = env_val
    else:
        dummy_env.pop("OLLAMA_COOLDOWN_SECONDS", None)

    code = """
import sys
import router.main
print(router.main.OLLAMA_COOLDOWN_SECONDS)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=dummy_env,
        capture_output=True,
        text=True,
        check=True
    )
    assert int(result.stdout.strip()) == expected
