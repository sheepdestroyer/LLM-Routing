import pytest
import copy
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import router.main
from router.main import ToolUsageRecord

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
    router.main.record_tool_usage(ToolUsageRecord(
        tool_name="shell",
        prompt_tokens=10,
        completion_tokens=20,
        model="gpt-4",
        latency_ms=150.0
    ))

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
    router.main.record_tool_usage(ToolUsageRecord(
        tool_name="none",
        prompt_tokens=5,
        completion_tokens=5,
        model="gpt-4",
        latency_ms=100.0
    ))

    assert "none" not in router.main.stats["tool_tokens"] or router.main.stats["tool_tokens"].get("none") == 0
    assert router.main.stats["tool_tokens"]["other"] == 10

def test_record_tool_usage_accumulation():
    """Test that tokens accumulate correctly over multiple calls."""
    router.main.record_tool_usage(ToolUsageRecord(
        tool_name="write",
        prompt_tokens=10,
        completion_tokens=10,
        model="model1",
        latency_ms=50.0
    ))
    router.main.record_tool_usage(ToolUsageRecord(
        tool_name="write",
        prompt_tokens=20,
        completion_tokens=30,
        model="model2",
        latency_ms=60.0
    ))

    assert router.main.stats["tool_tokens"]["write"] == 70
    assert router.main.stats["prompt_tokens"] == 30
    assert router.main.stats["completion_tokens"] == 40
    assert len(router.main.stats["timeline"]) == 2

def test_record_tool_usage_timeline_limit():
    """Test that the timeline buffer is capped at 15 events."""
    # Add 20 events
    for i in range(20):
        router.main.record_tool_usage(ToolUsageRecord(
            tool_name=f"tool_{i}",
            prompt_tokens=1,
            completion_tokens=1,
            model="model",
            latency_ms=10.0
        ))

    assert len(router.main.stats["timeline"]) == 15
    # The first 5 events should be popped off, so the oldest event in the timeline
    # should be from tool_5 (since we started at tool_0).
    assert router.main.stats["timeline"][0]["tool"] == "tool_5"
    assert router.main.stats["timeline"][-1]["tool"] == "tool_19"

def test_record_tool_usage_custom_route():
    """Test recording tool usage with a custom route."""
    router.main.record_tool_usage(ToolUsageRecord(
        tool_name="tree",
        prompt_tokens=5,
        completion_tokens=5,
        model="gpt-4",
        latency_ms=100.0,
        route="google_oauth_direct"
    ))

    assert router.main.stats["routing_paths"]["google_oauth_direct"] == 1
    assert router.main.stats["routing_paths"]["litellm_fallback"] == 0

@pytest.mark.asyncio
async def test_record_tool_usage_async_paths():
    """Test the asynchronous and background task execution paths."""
    # Ensure stats has basic structure
    router.main.stats = {"tool_tokens": {}, "timeline": []}

    with patch("router.main.save_persisted_stats", new_callable=AsyncMock) as mock_save, \
         patch.object(asyncio.get_running_loop(), "run_in_executor") as mock_executor, \
         patch("time.monotonic", return_value=100.0):

        # We need a mock future so we can inspect add_done_callback for the executor task
        mock_future = MagicMock()
        mock_executor.return_value = mock_future

        # Force the 2.0s throttles to pass
        router.main._last_stats_save = 90.0
        router.main.record_tool_usage._last_save = 90.0

        router.main.record_tool_usage(ToolUsageRecord(
            tool_name="shell",
            prompt_tokens=10,
            completion_tokens=20,
            model="gpt-4",
            latency_ms=150.0
        ))

        # Yield to event loop so the create_task for save_persisted_stats can run
        await asyncio.sleep(0)

        # Check create_task path
        mock_save.assert_called_once()

        # Check run_in_executor path for timeline saving
        mock_executor.assert_called_once()
        mock_future.add_done_callback.assert_called_once()

        # Test the run_in_executor done_callback behavior (exception handling)
        cb = mock_future.add_done_callback.call_args[0][0]

        fail_future = MagicMock()
        fail_future.result.side_effect = Exception("test error")
        with patch("router.main.logger.warning") as mock_warning:
            cb(fail_future)
            mock_warning.assert_called_with("Failed to persist timeline in background: test error")

def test_record_tool_usage_runtime_error_stats_sync_write():
    """Test the fallback sync path when no event loop is running (stats)."""
    with patch("asyncio.get_running_loop", side_effect=RuntimeError("no loop")), \
         patch("router.main._atomic_write_json_sync") as mock_sync_write, \
         patch("time.monotonic", return_value=100.0):

        router.main._last_stats_save = 90.0 # forces write
        router.main.record_tool_usage._last_save = 100.0 # prevents timeline write

        router.main.record_tool_usage(ToolUsageRecord(
            tool_name="shell",
            prompt_tokens=10,
            completion_tokens=20,
            model="gpt-4",
            latency_ms=150.0
        ))

        mock_sync_write.assert_called_once()

def test_record_tool_usage_runtime_error_timeline_sync_write():
    """Test the fallback sync path when no event loop is running (timeline)."""
    # ensure clean state
    router.main.stats = {"tool_tokens": {}, "timeline": []}

    # We want create_task to succeed (simulating early phase where stats writes asynchronously,
    # but let's actually just raise RuntimeError for the second call to get_running_loop)
    def mock_get_running_loop():
        # First time (stats) we succeed, second time (timeline) we fail
        if mock_get_running_loop.calls == 0:
            mock_get_running_loop.calls += 1
            loop = MagicMock()
            loop.create_task = MagicMock()
            return loop
        raise RuntimeError("no loop")

    mock_get_running_loop.calls = 0

    with patch("asyncio.get_running_loop", side_effect=mock_get_running_loop), \
         patch("router.main._atomic_write_json_sync") as mock_sync_write, \
         patch("time.monotonic", return_value=100.0):

        router.main._last_stats_save = 100.0 # prevents stats write fallback
        router.main.record_tool_usage._last_save = 90.0 # forces timeline write

        router.main.record_tool_usage(ToolUsageRecord(
            tool_name="shell",
            prompt_tokens=10,
            completion_tokens=20,
            model="gpt-4",
            latency_ms=150.0
        ))

        # timeline write falls back to sync
        mock_sync_write.assert_called_once()
        assert "router_timeline.json" in mock_sync_write.call_args[0][0]


def test_record_tool_usage_stats_sync_write_exception():
    """Test the exception handling in fallback sync path (stats)."""
    with patch("asyncio.get_running_loop", side_effect=RuntimeError("no loop")), \
         patch("router.main._atomic_write_json_sync", side_effect=Exception("sync stats error")), \
         patch("router.main.logger.error") as mock_error, \
         patch("time.monotonic", return_value=100.0):

        router.main._last_stats_save = 90.0 # forces write
        router.main.record_tool_usage._last_save = 100.0 # prevents timeline write

        router.main.record_tool_usage(ToolUsageRecord(
            tool_name="shell",
            prompt_tokens=10,
            completion_tokens=20,
            model="gpt-4",
            latency_ms=150.0
        ))

        mock_error.assert_called_with("Failed to persist stats to disk: sync stats error")




def test_record_tool_usage_timeline_sync_write_exception():
    """Test the exception handling in fallback sync path (timeline)."""
    router.main.stats = {"tool_tokens": {}, "timeline": []}

    def mock_get_running_loop():
        if mock_get_running_loop.calls == 0:
            mock_get_running_loop.calls += 1
            loop = MagicMock()
            loop.create_task = MagicMock()
            return loop
        raise RuntimeError("no loop")

    mock_get_running_loop.calls = 0

    with patch("asyncio.get_running_loop", side_effect=mock_get_running_loop), \
         patch("router.main._atomic_write_json_sync", side_effect=Exception("sync timeline error")), \
         patch("router.main.logger.warning") as mock_warning, \
         patch("time.monotonic", return_value=100.0):

        router.main._last_stats_save = 100.0
        router.main.record_tool_usage._last_save = 90.0

        router.main.record_tool_usage(ToolUsageRecord(
            tool_name="shell",
            prompt_tokens=10,
            completion_tokens=20,
            model="gpt-4",
            latency_ms=150.0
        ))

        mock_warning.assert_called_with("Failed to persist timeline: sync timeline error")
