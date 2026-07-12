import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import sys
import os

@pytest.mark.asyncio
async def test_get_dashboard_data_structure():
    # Ensure router directory is in sys.path
    router_path = os.path.join(os.getcwd(), "router")
    if router_path not in sys.path:
        sys.path.insert(0, router_path)

    import main

    # Mocking all I/O and external calls
    with patch("main.sync_cooldowns_from_valkey", new_callable=AsyncMock) as mock_sync, \
         patch("main.check_tcp_port", new_callable=AsyncMock) as mock_tcp, \
         patch("main.check_http_endpoint", new_callable=AsyncMock) as mock_http, \
         patch("main.get_gemini_oauth_status", new_callable=AsyncMock) as mock_oauth, \
         patch("main.get_best_free_model", new_callable=AsyncMock) as mock_best_model, \
         patch("main.get_goose_sessions") as mock_goose, \
         patch("main.get_llamacpp_metrics", new_callable=AsyncMock) as mock_llamacpp, \
         patch("main.get_pie_chart_gradient") as mock_gradient, \
         patch("main.stats") as mock_stats:

        # Setup mock return values
        mock_sync.return_value = None
        mock_tcp.return_value = True
        mock_http.return_value = True
        mock_oauth.return_value = {"status": "valid", "detail": "Expires in 1h", "expiry_ms": 123456789}
        mock_best_model.return_value = {"id": "test-model", "name": "Test Model", "score": 90.0}
        mock_goose.return_value = [{"id": 1, "name": "Session 1", "updated_at": "2023-01-01", "accumulated_total_tokens": 100}]
        mock_llamacpp.return_value = {
            "models": [{"id": "model-1", "status": "loaded", "n_params": 7e9, "n_ctx": 4096, "size_bytes": 4e9}],
            "slots": [{"id": 0, "is_processing": True, "n_prompt_processed": 10, "n_decoded": 20}],
            "build": "test-build"
        }
        mock_gradient.return_value = "conic-gradient(red 0% 100%)"

        # Mock stats behavior
        mock_stats_dict = {
            "simple_requests": 1,
            "medium_requests": 2,
            "complex_requests": 3,
            "reasoning_requests": 4,
            "advanced_requests": 5,
            "routing_paths": {"google_oauth_direct": 10, "litellm_fallback": 20},
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "avg_triage_latency_ms": 50,
            "avg_proxy_latency_ms": 150,
            "cache_hits": 5,
            "total_requests": 100,
            "last_triage_decision": "simple",
            "timeline": [],
            "tool_tokens": {"tree": 10, "shell": 20, "write": 30, "view": 40, "other": 50}
        }

        mock_stats.get.side_effect = lambda key, default=None: mock_stats_dict.get(key, default)
        mock_stats.__getitem__.side_effect = lambda key: mock_stats_dict[key]

        data = await main.get_dashboard_data()

        assert "valkey_status" in data
        assert "litellm_status" in data
        assert "best_free_model" in data
        assert "oauth_banner_html" in data
        assert "tier_table_html" in data
        assert "goose_html" in data
        assert "llamacpp_models_html" in data

        # Verify that expected mocks were called (at least once)
        assert mock_sync.called
        assert mock_tcp.called
        assert mock_http.called
        assert mock_oauth.called
        assert mock_best_model.called
        assert mock_goose.called
        assert mock_llamacpp.called
