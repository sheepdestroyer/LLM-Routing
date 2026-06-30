import pytest
import copy
from unittest.mock import patch

import router.main

# Save the original stats dictionary to reset it after each test
_ORIGINAL_STATS = copy.deepcopy(router.main.stats)

@pytest.fixture(autouse=True)
def reset_stats(monkeypatch):
    """Fixture to reset the stats dictionary before each test to ensure isolation."""
    # We patch the stats dict with a fresh copy of the original
    monkeypatch.setattr(router.main, "stats", copy.deepcopy(_ORIGINAL_STATS))
    yield

@pytest.fixture(autouse=True)
def mock_persistence():
    """Mock out disk writing functions to avoid side effects during tests."""
    with patch("router.main._atomic_write_json_sync"), patch("router.main.save_persisted_stats"):
        yield

def test_record_tool_usage_basic():
    """Test basic token recording for a standard tool."""
    router.main.record_tool_usage(
        tool_name="shell",
        prompt_tokens=10,
        completion_tokens=20,
        model="gpt-4",
        latency_ms=150.0
    )

    assert router.main.stats["tool_tokens"]["shell"] == 30
    assert router.main.stats["prompt_tokens"] == 10
    assert router.main.stats["completion_tokens"] == 20
    assert router.main.stats["routing_paths"]["litellm_fallback"] == 1

    assert len(router.main.stats["timeline"]) == 1
    event = router.main.stats["timeline"][0]
    assert event["tool"] == "shell"
    assert event["model"] == "gpt-4"
    assert event["route"] == "litellm_fallback"
    assert event["tokens"] == 30
    assert event["latency_ms"] == 150

def test_record_tool_usage_none_mapping():
    """Test that 'none' tool is correctly mapped to 'other'."""
    router.main.record_tool_usage(
        tool_name="none",
        prompt_tokens=5,
        completion_tokens=5,
        model="gpt-4",
        latency_ms=100.0
    )

    assert "none" not in router.main.stats["tool_tokens"] or router.main.stats["tool_tokens"].get("none") == 0
    assert router.main.stats["tool_tokens"]["other"] == 10

def test_record_tool_usage_accumulation():
    """Test that tokens accumulate correctly over multiple calls."""
    router.main.record_tool_usage("write", 10, 10, "model1", 50.0)
    router.main.record_tool_usage("write", 20, 30, "model2", 60.0)

    assert router.main.stats["tool_tokens"]["write"] == 70
    assert router.main.stats["prompt_tokens"] == 30
    assert router.main.stats["completion_tokens"] == 40
    assert len(router.main.stats["timeline"]) == 2

def test_record_tool_usage_timeline_limit():
    """Test that the timeline buffer is capped at 15 events."""
    # Add 20 events
    for i in range(20):
        router.main.record_tool_usage(f"tool_{i}", 1, 1, "model", 10.0)

    assert len(router.main.stats["timeline"]) == 15
    # The first 5 events should be popped off, so the oldest event in the timeline
    # should be from tool_5 (since we started at tool_0).
    assert router.main.stats["timeline"][0]["tool"] == "tool_5"
    assert router.main.stats["timeline"][-1]["tool"] == "tool_19"

def test_record_tool_usage_custom_route():
    """Test recording tool usage with a custom route."""
    router.main.record_tool_usage(
        tool_name="tree",
        prompt_tokens=5,
        completion_tokens=5,
        model="gpt-4",
        latency_ms=100.0,
        route="google_oauth_direct"
    )

    assert router.main.stats["routing_paths"]["google_oauth_direct"] == 1
    assert router.main.stats["routing_paths"]["litellm_fallback"] == 0
