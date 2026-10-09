"""Regression contract for the Gemini delegation fallback policy."""

from pathlib import Path

import yaml


def test_agy_gemini_fallbacks_are_local_only_and_ordered():
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "litellm/config.yaml").read_text())
    matches = [entry["agy-gemini"] for entry in config["litellm_settings"]["fallbacks"] if "agy-gemini" in entry]
    assert matches == [["strata-qwen", "locallama-qwen"]]
    fallbacks = {name: targets for entry in config["litellm_settings"]["fallbacks"] for name, targets in entry.items()}
    assert "strata-qwen" not in fallbacks
    assert "locallama-qwen" not in fallbacks
    assert {"strata-qwen", "locallama-qwen"} <= set(config["litellm_settings"]["public_model_groups"])


def test_agy_opus_fallbacks_are_gemini_then_local_only():
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "litellm/config.yaml").read_text())
    matches = [entry["agy-opus"] for entry in config["litellm_settings"]["fallbacks"] if "agy-opus" in entry]
    assert matches == [["agy-gemini", "strata-qwen", "locallama-qwen"]]
    fallbacks = {name: targets for entry in config["litellm_settings"]["fallbacks"] for name, targets in entry.items()}
    assert "agy-opus" not in fallbacks["agy-gemini"]
