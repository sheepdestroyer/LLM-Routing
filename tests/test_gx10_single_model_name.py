"""Contract: every GX10 deployment asks the engine for one served name, strata-qwen.

Strata accepts several aliases, Basalt only one (its --alias). The client-facing model groups
(strata-qwen, locallama-strata, gb10-strata, strata-qwen-vl) stay; only the backend name is shared,
so either engine can serve all four. config.yaml and model_sync.py must agree.
"""

from pathlib import Path
from unittest.mock import MagicMock

import yaml

from router.model_sync import ModelRegistrySync

GX10_GROUPS = {"strata-qwen", "locallama-strata", "gb10-strata", "strata-qwen-vl"}
SERVED = "openai/strata-qwen"


def test_config_yaml_gx10_deployments_send_one_name():
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "litellm/config.yaml").read_text())
    gx10 = [m for m in config["model_list"] if m["litellm_params"].get("api_base") == "STRATA_SERVER_URL_PLACEHOLDER"]
    assert {m["model_name"] for m in gx10} == GX10_GROUPS
    assert {m["litellm_params"]["model"] for m in gx10} == {SERVED}


def test_model_sync_gx10_deployments_send_one_name():
    sync = ModelRegistrySync(
        litellm_url="http://test-litellm:4000",
        master_key="sk-test-key",
        agy_daemon_url="http://test-agy:5005",
        llama_server_url="http://test-llama:8083",
        whisper_server_url="http://test-whisper:8084",
        classifier_url="http://test-classifier:8086",
        strata_server_url="http://test-gx10:8080",
        client=MagicMock(),
    )
    gx10 = sync.build_strata_models()
    assert {m["model_name"] for m in gx10} == GX10_GROUPS
    assert {m["litellm_params"]["model"] for m in gx10} == {SERVED}
    assert {m["litellm_params"]["api_base"] for m in gx10} == {"http://test-gx10:8080/v1"}
