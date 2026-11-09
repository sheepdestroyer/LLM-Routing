"""Contract for the single Qwen3.8-27B llama-router instance (AGENTS.md L1 amendment, 2026-10-30).

llama-router serves local-qwen, local-qwen-hass and local-qwen-vl from one preset, so the hass and
vision profiles are LiteLLM request defaults. config.yaml and model_sync.py must agree.

hass: thinking off with the model card's non-thinking sampling. vl: thinking on since 2026-11-09
(owner decision after the TASK-353 rebench) with the model card's thinking-mode temperature and
top_p, which are also the preset's.
"""

from pathlib import Path
from unittest.mock import MagicMock

import yaml

from router.model_sync import HASS_SAMPLING, VL_SAMPLING, ModelRegistrySync

THINKING_OFF = {"chat_template_kwargs": {"enable_thinking": False}}
THINKING_ON = {"chat_template_kwargs": {"enable_thinking": True}}
EXPECTED = {
    "locallama-qwen-hass": (HASS_SAMPLING, THINKING_OFF, False),
    "local-qwen-hass": (HASS_SAMPLING, THINKING_OFF, False),
    "locallama-qwen-vl": (VL_SAMPLING, THINKING_ON, True),
    "local-qwen-vl": (VL_SAMPLING, THINKING_ON, True),
}
CONFIG = Path(__file__).resolve().parents[1] / "litellm/config.yaml"


def _check(deployment: dict, sampling: dict, thinking: dict, reasoning: bool) -> None:
    params = deployment["litellm_params"]
    assert params["extra_body"] == thinking
    for key, value in sampling.items():
        assert params[key] == value, key
    # Every other sampling parameter comes from the [Qwen3.8-27B-spec] preset (rule L1).
    assert not ({"presence_penalty", "top_k", "min_p", "repeat_penalty"} - sampling.keys()) & params.keys()
    assert deployment["model_info"]["supports_reasoning"] is reasoning


def test_profiles():
    # hass: Qwen3.8 model card, non-thinking mode. vl: model card, thinking mode (temperature 1.0,
    # top_p 0.95: the [Qwen3.8-27B-spec] preset's own values) plus the former vl preset's seed.
    assert HASS_SAMPLING == {"temperature": 0.7, "top_p": 0.8, "presence_penalty": 1.5}
    assert VL_SAMPLING == {"temperature": 1.0, "top_p": 0.95, "seed": 3407}


def test_config_yaml_alias_params():
    config = yaml.safe_load(CONFIG.read_text())
    deployments = {m["model_name"]: m for m in config["model_list"]}
    for name, expected in EXPECTED.items():
        _check(deployments[name], *expected)
    assert "temperature" not in deployments["local-qwen"]["litellm_params"]


def test_model_sync_alias_params():
    sync = ModelRegistrySync(
        litellm_url="http://test-litellm:4000",
        master_key="sk-test-key",
        agy_daemon_url="http://test-agy:5005",
        llama_server_url="http://test-llama:8083",
        whisper_server_url="http://test-whisper:8084",
        classifier_url="http://test-classifier:8086",
        client=MagicMock(),
    )
    deployments = {m["model_name"]: m for m in sync.build_locallama_models()}
    for name, expected in EXPECTED.items():
        _check(deployments[name], *expected)
    # Each deployment has its own extra_body: no shared mutable dict between aliases.
    bodies = [deployments[name]["litellm_params"]["extra_body"] for name in EXPECTED]
    assert len({id(body) for body in bodies}) == len(bodies)
