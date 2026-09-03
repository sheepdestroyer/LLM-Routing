import pytest
import asyncio
import time
import os
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_maybe_trigger_roster_sync():
    import router.main as main

    now = time.monotonic()

    with (
        patch.object(main, "_last_roster_sync", now - 1000.0),
        patch.object(main, "sync_adaptive_router_roster", new_callable=AsyncMock) as mock_sync,
        patch("router.main.logger") as mock_logger,
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-key"}),
    ):
        # Test forced trigger (force=True)
        await main.maybe_trigger_roster_sync(force=True)
        mock_sync.assert_awaited_once_with("test-key")
        mock_logger.info.assert_called_with("Triggering opportunistic roster sync (force=True)")

        # Reset mock
        mock_sync.reset_mock()
        mock_logger.reset_mock()

        # Test non-forced trigger, immediately after
        # Should not trigger because _last_roster_sync was updated
        await main.maybe_trigger_roster_sync(force=False)
        mock_sync.assert_not_awaited()

        # Mock time to pass min_interval (300.0)
        with patch("time.monotonic", return_value=main._last_roster_sync + 301.0):
            await main.maybe_trigger_roster_sync(force=False)
            mock_sync.assert_awaited_once_with("test-key")


@pytest.mark.asyncio
async def test_maybe_trigger_roster_sync_no_master_key():
    import router.main as main

    now = time.monotonic()

    with (
        patch.object(main, "_last_roster_sync", now - 1000.0),
        patch.object(main, "sync_adaptive_router_roster", new_callable=AsyncMock) as mock_sync,
        patch.dict(os.environ, {}, clear=True),
    ):
        await main.maybe_trigger_roster_sync(force=True)
        mock_sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_trigger_roster_sync_lock():
    import router.main as main

    now = time.monotonic()

    with (
        patch.object(main, "_last_roster_sync", now - 1000.0),
        patch.object(main, "sync_adaptive_router_roster", new_callable=AsyncMock) as mock_sync,
    ):
        # Simulate lock being acquired
        await main._roster_sync_lock.acquire()
        try:
            await main.maybe_trigger_roster_sync(force=True)
            mock_sync.assert_not_awaited()
        finally:
            main._roster_sync_lock.release()
