"""Tests for issue #722: best free model background refresh (no per-dashboard-poll fetch)."""

import asyncio

import pytest
from unittest.mock import AsyncMock, patch

import router.main as rm
from router.main import (
    FREE_MODEL_REFRESH_INTERVAL,
    _periodic_best_free_model_refresh,
    _refresh_best_free_model,
    get_best_free_model,
)


@pytest.fixture(autouse=True)
def reset_free_model_cache():
    """Isolate the global free-model cache between tests."""
    rm.free_model_cache["data"] = None
    rm.free_model_cache["last_fetched"] = 0.0
    yield
    rm.free_model_cache["data"] = None
    rm.free_model_cache["last_fetched"] = 0.0


@pytest.mark.asyncio
async def test_background_refresh_populates_cache():
    """A successful refresh cycle populates the cache and dashboard reads it without OpenRouter I/O."""
    fetched = [
        {
            "id": "top-free-model",
            "name": "Top Free",
            "score": 95.0,
            "context_length": 200000,
            "has_tools": True,
        },
        {"id": "second", "name": "Second", "score": 60.0, "context_length": 1000, "has_tools": False},
    ]
    with (
        patch("router.main._fetch_openrouter_free_models", return_value=fetched),
        patch("router.main._save_free_models_roster") as mock_roster,
        patch("router.main._save_best_model_to_disk") as mock_disk,
    ):
        best = await _refresh_best_free_model()

    assert best["id"] == "top-free-model"
    assert best["is_fallback"] is False
    assert rm.free_model_cache["data"]["id"] == "top-free-model"
    assert rm.free_model_cache["last_fetched"] > 0
    mock_roster.assert_called_once()
    assert mock_roster.call_args[0][0][0]["id"] == "top-free-model"
    mock_disk.assert_called_once()

    # Dashboard path: get_best_free_model serves the cache, never calls OpenRouter
    with patch("router.main._fetch_openrouter_free_models") as mock_no_fetch:
        served = await get_best_free_model()
    assert served["id"] == "top-free-model"
    mock_no_fetch.assert_not_called()


@pytest.mark.asyncio
async def test_error_path_keeps_last_known_good():
    """On fetch failure (empty result), the last-known-good cache is served and NOT overwritten on disk."""
    rm.free_model_cache["data"] = {"id": "lkg-model", "name": "LKG", "score": 80.0, "is_fallback": False}
    rm.free_model_cache["last_fetched"] = 123.0

    with (
        patch("router.main._fetch_openrouter_free_models", return_value=[]),
        patch("router.main._save_best_model_to_disk") as mock_disk,
        patch("router.main.logger.warning") as mock_warn,
        patch("router.main.logger.debug") as mock_debug,
    ):
        served = await _refresh_best_free_model()

    assert served["id"] == "lkg-model"
    assert rm.free_model_cache["data"]["id"] == "lkg-model"
    assert rm.free_model_cache["last_fetched"] == 123.0
    mock_disk.assert_not_called()
    # The fetch helper logs the single failure warning; the refresh adds no extra WARNING
    mock_warn.assert_not_called()
    assert mock_debug.call_count == 1


@pytest.mark.asyncio
async def test_fallback_only_on_empty_cache():
    """The hardcoded fallback is used (and persisted) only when the cache is empty."""
    assert rm.free_model_cache["data"] is None
    with (
        patch("router.main._fetch_openrouter_free_models", return_value=[]),
        patch("router.main._save_best_model_to_disk") as mock_disk,
        patch("router.main.logger.warning") as mock_warn,
    ):
        best = await _refresh_best_free_model()

    assert best["is_fallback"] is True
    assert best["id"] == "moonshotai/kimi-k2.6:free"
    mock_disk.assert_called_once()
    assert mock_warn.call_count == 1
    # Cache stays empty so a later successful refresh still wins
    assert rm.free_model_cache["data"] is None


@pytest.mark.asyncio
async def test_periodic_refresh_runs_immediately_and_loops():
    """The background task refreshes once at startup, then sleeps for the 15-minute interval."""
    with (
        patch("router.main._refresh_best_free_model", new_callable=AsyncMock) as mock_refresh,
        patch("router.main.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        # First cycle completes and sleeps; the sleep is cancelled -> loop breaks
        mock_sleep.side_effect = [asyncio.CancelledError()]
        try:
            await _periodic_best_free_model_refresh()
        except asyncio.CancelledError:
            pass

    assert mock_refresh.call_count == 1
    assert mock_sleep.call_count == 1
    mock_sleep.assert_any_call(FREE_MODEL_REFRESH_INTERVAL)


@pytest.mark.asyncio
async def test_periodic_refresh_survives_cycle_exception():
    """A raising cycle logs one warning and re-schedules instead of killing the task."""
    with (
        patch("router.main._refresh_best_free_model", new_callable=AsyncMock) as mock_refresh,
        patch("router.main.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        patch("router.main.logger.warning") as mock_warn,
    ):
        # Cycle 1 raises -> warning + re-schedule; cycle 2 succeeds; cancel on next sleep
        mock_refresh.side_effect = [RuntimeError("boom"), None]
        mock_sleep.side_effect = [None, asyncio.CancelledError()]
        try:
            await _periodic_best_free_model_refresh()
        except asyncio.CancelledError:
            pass

    assert mock_refresh.call_count == 2
    assert mock_warn.call_count == 1
    mock_sleep.assert_any_call(FREE_MODEL_REFRESH_INTERVAL)


@pytest.mark.asyncio
async def test_periodic_refresh_cancelled_during_fetch():
    """Cancellation during the fetch breaks the loop without sleeping."""
    with (
        patch("router.main._refresh_best_free_model", new_callable=AsyncMock) as mock_refresh,
        patch("router.main.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        mock_refresh.side_effect = asyncio.CancelledError()
        await _periodic_best_free_model_refresh()

    mock_sleep.assert_not_called()
