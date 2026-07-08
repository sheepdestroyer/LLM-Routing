import pytest
import time
from unittest.mock import patch, AsyncMock
from router import main
from router.main import save_persisted_stats

@pytest.fixture(autouse=True)
def reset_last_save():
    """Reset the global _last_stats_save before and after each test."""
    original = main._last_stats_save
    yield
    main._last_stats_save = original

@pytest.mark.anyio
async def test_save_persisted_stats_force():
    """Test that force=True bypasses the throttle and saves stats."""
    with patch("router.main._atomic_write_json_async", new_callable=AsyncMock) as mock_write:
        main._last_stats_save = time.monotonic()
        await save_persisted_stats(force=True)
        mock_write.assert_called_once_with(main.STATS_JSON_PATH, main.stats)

@pytest.mark.anyio
async def test_save_persisted_stats_throttle():
    """Test that disk writes are throttled when time hasn't passed."""
    with patch("router.main._atomic_write_json_async", new_callable=AsyncMock) as mock_write:
        main._last_stats_save = time.monotonic()
        await save_persisted_stats(force=False)
        mock_write.assert_not_called()

@pytest.mark.anyio
async def test_save_persisted_stats_time_passed():
    """Test that stats are saved when the throttle interval has passed."""
    with patch("router.main._atomic_write_json_async", new_callable=AsyncMock) as mock_write:
        main._last_stats_save = time.monotonic() - 3.0 # More than 2.0 seconds ago
        await save_persisted_stats(force=False)
        mock_write.assert_called_once_with(main.STATS_JSON_PATH, main.stats)

@pytest.mark.anyio
async def test_save_persisted_stats_exception():
    """Test that _last_stats_save is reset on failure to allow immediate retry."""
    with patch("router.main._atomic_write_json_async", new_callable=AsyncMock) as mock_write:
        mock_write.side_effect = Exception("Write failed")

        main._last_stats_save = time.monotonic() - 3.0

        await save_persisted_stats(force=False)

        # Verify it was reset on exception
        assert main._last_stats_save == 0.0
