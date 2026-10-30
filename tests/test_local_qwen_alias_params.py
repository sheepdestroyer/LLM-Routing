"""Contract for the single Qwen3.8-27B llama-router instance (AGENTS.md L1 amendment, 2026-10-30).

llama-router serves local-qwen, local-qwen-hass and local-qwen-vl from one preset, so the hass and
vision profiles are LiteLLM request defaults. config.yaml and model_sync.py must agree.
"""

from pathlib import Path
from unittest.mock import MagicMock

import yaml

from router.model_sync import HASS_SAMPLING, VL_SAMPLING, ModelRegistrySync

NO_THINKING = {"chat_template_kwargs": {"enable_thinking": False}}
EXPECTED = {
    "locallama-qwen-hass": HASS_SAMPLING,
    "local-qwen-hass": HASS_SAMPLING,
    "locallama-qwen-vl": VL_SAMPLING,
    "local-qwen-vl": VL_SAMPLING,
}


def _check(params: dict, sampling: dict) -> None:
    assert params["extra_body"] == NO_THINKING
    for key, value in sampling.items():
        assert params[key] == value, key


def test_profiles_match_former_presets():
    assert HASS_SAMPLING == {"temperature": 0.7, "top_p": 0.8, "presence_penalty": 1.5}
    assert VL_SAMPLING == {"temperature": 0.7, "top_p": 0.8, "seed": 3407}


def test_config_yaml_alias_params():
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "litellm/config.yaml").read_text())
    deployments = {m["model_name"]: m["litellm_params"] for m in config["model_list"]}
    for name, sampling in EXPECTED.items():
        _check(deployments[name], sampling)
    assert "temperature" not in deployments["local-qwen"]


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
    deployments = {m["model_name"]: m["litellm_params"] for m in sync.build_locallama_models()}
    for name, sampling in EXPECTED.items():
        _check(deployments[name], sampling)
