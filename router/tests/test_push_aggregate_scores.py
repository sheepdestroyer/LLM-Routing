import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
import sys
import os

# Ensure router directory is in sys.path
router_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if router_path not in sys.path:
    sys.path.insert(0, router_path)

import main

@pytest.mark.asyncio
async def test_push_aggregate_scores_success():
    mock_lf = MagicMock()
    mock_lf.create_trace_id.return_value = "trace_123"

    mock_router = MagicMock()
    mock_router.google.tier = 1
    mock_router.vendor.tier = 2

    mock_stats = {
        "total_requests": 100,
        "simple_requests": 20,
        "medium_requests": 30,
        "complex_requests": 40,
        "reasoning_requests": 5,
        "advanced_requests": 5,
        "cache_hits": 10,
        "avg_triage_latency_ms": 150.0,
        "avg_proxy_latency_ms": 800.0,
        "routing_paths": {"google_oauth_direct": 25},
    }

    with patch("main.get_langfuse", return_value=mock_lf), \
         patch("main.get_breaker", return_value=mock_router), \
         patch.dict("main.stats", mock_stats, clear=True), \
         patch("main.logger") as mock_logger, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        # Make sleep raise CancelledError on second call to break the infinite loop
        mock_sleep.side_effect = [None, asyncio.CancelledError()]

        try:
            await main.push_aggregate_scores()
        except asyncio.CancelledError:
            pass

        assert mock_sleep.call_count == 2
        mock_lf.create_trace_id.assert_called_once()
        mock_lf.start_observation.assert_called_once()
        assert mock_lf.create_score.call_count == 12  # 12 scores pushed
        mock_lf.flush.assert_called_once()
        mock_logger.info.assert_called_once()

@pytest.mark.asyncio
async def test_push_aggregate_scores_no_langfuse():
    with patch("main.get_langfuse", return_value=None), \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        mock_sleep.side_effect = [None, asyncio.CancelledError()]

        try:
            await main.push_aggregate_scores()
        except asyncio.CancelledError:
            pass

        assert mock_sleep.call_count == 2
        # Should not throw exception and should just continue

@pytest.mark.asyncio
async def test_push_aggregate_scores_zero_requests():
    mock_lf = MagicMock()
    mock_stats = {"total_requests": 0}

    with patch("main.get_langfuse", return_value=mock_lf), \
         patch.dict("main.stats", mock_stats, clear=True), \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        mock_sleep.side_effect = [None, asyncio.CancelledError()]

        try:
            await main.push_aggregate_scores()
        except asyncio.CancelledError:
            pass

        assert mock_sleep.call_count == 2
        mock_lf.create_trace_id.assert_not_called()

@pytest.mark.asyncio
async def test_push_aggregate_scores_exception_handling():
    mock_lf = MagicMock()
    mock_lf.create_trace_id.side_effect = Exception("Langfuse error")

    mock_router = MagicMock()

    mock_stats = {
        "total_requests": 100,
        "simple_requests": 20,
        "medium_requests": 30,
        "complex_requests": 40,
        "reasoning_requests": 5,
        "advanced_requests": 5,
        "cache_hits": 10,
        "avg_triage_latency_ms": 150.0,
        "avg_proxy_latency_ms": 800.0,
        "routing_paths": {"google_oauth_direct": 25},
    }

    with patch("main.get_langfuse", return_value=mock_lf), \
         patch("main.get_breaker", return_value=mock_router), \
         patch.dict("main.stats", mock_stats, clear=True), \
         patch("main.logger") as mock_logger, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        mock_sleep.side_effect = [None, asyncio.CancelledError()]

        try:
            await main.push_aggregate_scores()
        except asyncio.CancelledError:
            pass

        mock_logger.warning.assert_called_once()
        assert "Langfuse error" in mock_logger.warning.call_args[0][0]
