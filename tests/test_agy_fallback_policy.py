"""Regression contract for the Gemini delegation fallback policy."""

from pathlib import Path

import yaml


def test_agy_gemini_fallbacks_are_local_only_and_ordered():
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "litellm/config.yaml").read_text())
    matches = [entry["agy-gemini"] for entry in config["litellm_settings"]["fallbacks"] if "agy-gemini" in entry]
    assert matches == [["strata-qwen", "locallama-qwen"]]
    fallbacks = {name: targets for entry in config["litellm_settings"]["fallbacks"] for name, targets in entry.items()}
    assert fallbacks["strata-qwen"] == ["locallama-qwen"]
    assert "locallama-qwen" not in fallbacks
    assert {"strata-qwen", "locallama-qwen"} <= set(config["litellm_settings"]["public_model_groups"])


def test_agy_opus_fallbacks_are_gemini_then_local_only():
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "litellm/config.yaml").read_text())
    matches = [entry["agy-opus"] for entry in config["litellm_settings"]["fallbacks"] if "agy-opus" in entry]
    assert matches == [["agy-gemini", "strata-qwen", "locallama-qwen"]]
    fallbacks = {name: targets for entry in config["litellm_settings"]["fallbacks"] for name, targets in entry.items()}
    assert "agy-opus" not in fallbacks["agy-gemini"]


LOCAL_API_BASES = {"LLAMA_SERVER_URL_PLACEHOLDER", "STRATA_SERVER_URL_PLACEHOLDER"}


def test_strata_qwen_fallback_closure_stays_local():
    """Every model group reachable from strata-qwen through (nested) fallbacks is LAN/local."""
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "litellm/config.yaml").read_text())
    fallbacks = {name: targets for entry in config["litellm_settings"]["fallbacks"] for name, targets in entry.items()}
    assert not config["litellm_settings"].get("default_fallbacks")
    assert not config["router_settings"].get("default_fallbacks")
    seen, todo = set(), ["strata-qwen"]
    while todo:
        group = todo.pop()
        if group in seen:
            continue
        seen.add(group)
        todo.extend(fallbacks.get(group, []))
    assert seen == {"strata-qwen", "locallama-qwen"}
    for dep in config["model_list"]:
        if dep["model_name"] in seen:
            assert dep["litellm_params"]["api_base"] in LOCAL_API_BASES


def test_strata_qwen_retry_and_cooldown_policy():
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "litellm/config.yaml").read_text())
    policy = config["router_settings"]["model_group_retry_policy"]["strata-qwen"]
    assert policy["InternalServerErrorRetries"] >= 1 and policy["TimeoutErrorRetries"] >= 1
    assert policy["BadRequestErrorRetries"] == 0 and policy["AuthenticationErrorRetries"] == 0
    (dep,) = [d for d in config["model_list"] if d["model_name"] == "strata-qwen"]
    assert dep["model_info"]["allowed_fails_policy"]["InternalServerErrorAllowedFails"] >= 1
    assert dep["model_info"]["cooldown_time"] < config["router_settings"]["cooldown_time"]
    sampling = {"temperature", "top_p", "top_k", "min_p", "presence_penalty", "repeat_penalty"}
    assert not sampling & set(dep["litellm_params"])  # L1: sampling presets live in model_presets.ini


def _fallback_closure(config, start):
    fallbacks = {name: targets for entry in config["litellm_settings"]["fallbacks"] for name, targets in entry.items()}
    seen, todo = set(), [start]
    while todo:
        group = todo.pop()
        if group in seen:
            continue
        seen.add(group)
        todo.extend(fallbacks.get(group, []))
    return seen


def test_strata_qwen_vl_vision_route_stays_on_the_lan():
    """TASK-353: case page images go to Strata, then only to the local Qwen3.8-27B vision profile."""
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "litellm/config.yaml").read_text())
    assert not config["litellm_settings"].get("default_fallbacks")
    assert not config["router_settings"].get("default_fallbacks")
    assert _fallback_closure(config, "strata-qwen-vl") == {"strata-qwen-vl", "locallama-qwen-vl"}
    # No-fallback groups: the local vision profile, and Strata alone (two-reader comparisons).
    for group in ("locallama-qwen-vl", "local-qwen-vl", "gb10-strata"):
        assert _fallback_closure(config, group) == {group}
    deployments = {d["model_name"]: d for d in config["model_list"]}
    assert deployments["strata-qwen-vl"]["litellm_params"]["api_base"] == "STRATA_SERVER_URL_PLACEHOLDER"
    for group in ("locallama-qwen-vl", "local-qwen-vl"):
        assert deployments[group]["litellm_params"]["api_base"] == "LLAMA_SERVER_URL_PLACEHOLDER"
    for group in ("strata-qwen-vl", "locallama-qwen-vl", "local-qwen-vl", "gb10-strata"):
        assert deployments[group]["model_info"]["supports_vision"] is True
    assert "strata-qwen-vl" in config["litellm_settings"]["public_model_groups"]


def test_strata_qwen_vl_retry_timeout_and_cooldown_policy():
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "litellm/config.yaml").read_text())
    policy = config["router_settings"]["model_group_retry_policy"]["strata-qwen-vl"]
    assert policy["InternalServerErrorRetries"] >= 1
    assert policy["TimeoutErrorRetries"] == 0  # a 540 s page is never run twice on Strata
    assert policy["BadRequestErrorRetries"] == 0 and policy["AuthenticationErrorRetries"] == 0
    (dep,) = [d for d in config["model_list"] if d["model_name"] == "strata-qwen-vl"]
    assert dep["litellm_params"]["request_timeout"] < 600  # leaves the fallback time inside a 600 s client budget
    assert dep["model_info"]["allowed_fails_policy"]["InternalServerErrorAllowedFails"] >= 1
    assert dep["model_info"]["cooldown_time"] < config["router_settings"]["cooldown_time"]
    sampling = {"temperature", "top_p", "top_k", "min_p", "presence_penalty", "repeat_penalty"}
    assert not sampling & set(dep["litellm_params"])  # L1: sampling presets live in model_presets.ini
