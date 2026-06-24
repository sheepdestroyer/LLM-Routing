import pytest
import asyncio
from unittest.mock import patch, MagicMock, call
import copy

import router.main as router_main

# A snapshot of the initial stats structure for resetting
INITIAL_STATS = {
    "total_requests": 0,
    "simple_requests": 0,
    "medium_requests": 0,
    "complex_requests": 0,
    "reasoning_requests": 0,
    "advanced_requests": 0,
    "cache_hits": 0,
    "last_triage_decision": "None",
    "avg_triage_latency_ms": 0.0,
    "avg_proxy_latency_ms": 0.0,
    "total_triage_time_ms": 0.0,
    "total_proxy_time_ms": 0.0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "tool_tokens": {
        "tree": 0,
        "shell": 0,
        "write": 0,
        "view": 0,
        "other": 0
    },
    "routing_paths": {"google_oauth_direct": 0, "litellm_fallback": 0},
    "timeline": []
}

@pytest.fixture
def reset_stats():
    """Fixture to reset the global stats dict, mock disk writes, and reset throttle timestamps."""
    original_stats = router_main.stats
    router_main.stats = copy.deepcopy(INITIAL_STATS)
    
    # Reset throttle timestamps to prevent state leakage between tests
    original_last_stats_save = getattr(router_main, "_last_stats_save", 0.0)
    original_last_save = getattr(router_main.record_tool_usage, "_last_save", 0.0)
    router_main._last_stats_save = 0.0
    router_main.record_tool_usage._last_save = 0.0

    with patch("router.main._atomic_write_json_sync") as mock_write:
        yield mock_write

    router_main.stats = original_stats
    router_main._last_stats_save = original_last_stats_save
    router_main.record_tool_usage._last_save = original_last_save

def test_record_tool_usage_basic_accumulation(reset_stats):
    """Test that tool and global token counts are updated correctly."""
    router_main.record_tool_usage(
        tool_name="shell",
        prompt_tokens=10,
        completion_tokens=20,
        model="gpt-4",
        latency_ms=100.5
    )

    assert router_main.stats["tool_tokens"]["shell"] == 30
    assert router_main.stats["prompt_tokens"] == 10
    assert router_main.stats["completion_tokens"] == 20
    assert router_main.stats["routing_paths"]["litellm_fallback"] == 1

    # Add another usage to test accumulation
    router_main.record_tool_usage(
        tool_name="shell",
        prompt_tokens=5,
        completion_tokens=15,
        model="gpt-4",
        latency_ms=50.0
    )

    assert router_main.stats["tool_tokens"]["shell"] == 50
    assert router_main.stats["prompt_tokens"] == 15
    assert router_main.stats["completion_tokens"] == 35
    assert router_main.stats["routing_paths"]["litellm_fallback"] == 2

def test_record_tool_usage_none_fallback(reset_stats):
    """Test that tool_name='none' is logged as 'other'."""
    router_main.record_tool_usage(
        tool_name="none",
        prompt_tokens=5,
        completion_tokens=5,
        model="gpt-4",
        latency_ms=10.0
    )

    assert router_main.stats["tool_tokens"]["other"] == 10
    assert "none" not in router_main.stats["tool_tokens"]

def test_record_tool_usage_routing_paths_init(reset_stats):
    """Test that routing_paths is initialized if missing and specific routes are tracked."""
    # Delete routing_paths to test initialization
    del router_main.stats["routing_paths"]

    router_main.record_tool_usage(
        tool_name="view",
        prompt_tokens=0,
        completion_tokens=0,
        model="gpt-4",
        latency_ms=0,
        route="google_oauth_direct"
    )

    assert "routing_paths" in router_main.stats
    assert router_main.stats["routing_paths"]["google_oauth_direct"] == 1
    assert router_main.stats["routing_paths"]["litellm_fallback"] == 0

@patch("router.main.time.strftime")
def test_record_tool_usage_timeline_buffer(mock_strftime, reset_stats):
    """Test that events are appended and older events are dropped when limit > 15."""
    mock_strftime.return_value = "12:00:00"

    # Add 16 events
    for i in range(16):
        router_main.record_tool_usage(
            tool_name=f"tool_{i}",
            prompt_tokens=1,
            completion_tokens=1,
            model="test-model",
            latency_ms=10.0 + i
        )

    timeline = router_main.stats["timeline"]
    assert len(timeline) == 15

    # The first event ("tool_0") should have been popped
    assert timeline[0]["tool"] == "tool_1"
    assert timeline[-1]["tool"] == "tool_15"
    assert timeline[-1]["latency_ms"] == 25
    assert timeline[-1]["timestamp"] == "12:00:00"

@patch("router.main.asyncio.get_running_loop")
def test_record_tool_usage_with_event_loop(mock_get_running_loop, reset_stats):
    """Test background task creation when event loop is running."""
    mock_loop = MagicMock()
    mock_get_running_loop.return_value = mock_loop
    mock_task = MagicMock()
    mock_loop.create_task.return_value = mock_task

    with patch("router.main._background_tasks", set()):
        router_main.record_tool_usage("shell", 1, 1, "test", 10.0)

        mock_loop.create_task.assert_called_once()
        mock_task.add_done_callback.assert_called_once()

@patch("router.main.asyncio.get_running_loop")
def test_record_tool_usage_no_event_loop(mock_get_running_loop, reset_stats):
    """Test fallback sync execution when no event loop is running."""
    mock_get_running_loop.side_effect = RuntimeError("no running event loop")
    mock_atomic_write_sync = reset_stats

    router_main.record_tool_usage("shell", 1, 1, "test", 10.0)

    assert mock_atomic_write_sync.call_count == 2 # one for stats, one for timeline

@patch("router.main.asyncio.get_running_loop")
def test_record_tool_usage_no_event_loop_fallback_error(mock_get_running_loop, reset_stats):
    """Test fallback sync execution gracefully handles errors."""
    mock_get_running_loop.side_effect = RuntimeError("no running event loop")
    mock_atomic_write_sync = reset_stats
    mock_atomic_write_sync.side_effect = Exception("write failed")

    # Should not raise
    router_main.record_tool_usage("shell", 1, 1, "test", 10.0)

    assert mock_atomic_write_sync.call_count == 2
