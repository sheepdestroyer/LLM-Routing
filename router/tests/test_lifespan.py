import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi import FastAPI
import asyncio
import os

from router.main import lifespan

@pytest.mark.anyio
async def test_lifespan_happy_path():
    app = FastAPI()

    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.get.return_value = mock_response

    with patch("router.main.get_http_client", return_value=mock_client), \
         patch("router.main.sync_cooldowns_from_valkey", new_callable=AsyncMock) as mock_sync_cooldowns, \
         patch("router.main.sync_adaptive_router_roster", new_callable=AsyncMock) as mock_sync_roster, \
         patch("router.main._register_openrouter_models_in_db", new_callable=AsyncMock) as mock_register_openrouter, \
         patch("router.main._register_ollama_models_in_db", new_callable=AsyncMock) as mock_register_ollama, \
         patch("router.main.push_aggregate_scores", new_callable=AsyncMock) as mock_push_scores, \
         patch("router.main._periodic_triage_cache_cleanup", new_callable=AsyncMock) as mock_cleanup, \
         patch("asyncio.sleep", new_callable=AsyncMock), \
         patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-key"}):

        async with lifespan(app):
            pass

        mock_sync_cooldowns.assert_called_once()
        mock_client.get.assert_called_once()
        mock_sync_roster.assert_called_once_with("test-key")
        mock_register_openrouter.assert_called_once_with("test-key")
        mock_register_ollama.assert_called_once_with("test-key")

@pytest.mark.anyio
async def test_lifespan_timeout_path():
    app = FastAPI()

    mock_client = AsyncMock()
    # Mock timeout exception for client.get
    mock_client.get.side_effect = Exception("Timeout")

    with patch("router.main.get_http_client", return_value=mock_client), \
         patch("router.main.sync_cooldowns_from_valkey", new_callable=AsyncMock), \
         patch("router.main.sync_adaptive_router_roster", new_callable=AsyncMock) as mock_sync_roster, \
         patch("router.main._register_openrouter_models_in_db", new_callable=AsyncMock) as mock_register_openrouter, \
         patch("router.main._register_ollama_models_in_db", new_callable=AsyncMock) as mock_register_ollama, \
         patch("router.main.push_aggregate_scores", new_callable=AsyncMock), \
         patch("router.main._periodic_triage_cache_cleanup", new_callable=AsyncMock), \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
         patch("router.main.logger.warning") as mock_warning, \
         patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-key"}):

        async with lifespan(app):
            pass

        assert mock_client.get.call_count == 180
        # Does not sleep after the final attempt
        assert mock_sleep.call_count == 179
        mock_warning.assert_any_call("⚠️  LiteLLM not ready within timeout — proceeding without roster sync")
        mock_sync_roster.assert_not_called()
        mock_register_openrouter.assert_not_called()
        mock_register_ollama.assert_not_called()


@pytest.mark.anyio
async def test_lifespan_disabled_timeout_path():
    app = FastAPI()

    mock_client = AsyncMock()

    with patch("router.main.get_http_client", return_value=mock_client), \
         patch("router.main.sync_cooldowns_from_valkey", new_callable=AsyncMock), \
         patch("router.main.sync_adaptive_router_roster", new_callable=AsyncMock) as mock_sync_roster, \
         patch("router.main._register_openrouter_models_in_db", new_callable=AsyncMock) as mock_register_openrouter, \
         patch("router.main._register_ollama_models_in_db", new_callable=AsyncMock) as mock_register_ollama, \
         patch("router.main.push_aggregate_scores", new_callable=AsyncMock), \
         patch("router.main._periodic_triage_cache_cleanup", new_callable=AsyncMock), \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
         patch("router.main.logger.info") as mock_info, \
         patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-key", "LITELLM_READINESS_TIMEOUT": "0"}):

        async with lifespan(app):
            pass

        mock_client.get.assert_not_called()
        mock_sleep.assert_not_called()
        mock_sync_roster.assert_not_called()
        mock_register_openrouter.assert_not_called()
        mock_register_ollama.assert_not_called()
        mock_info.assert_any_call("ℹ️  LiteLLM readiness wait disabled (timeout <= 0) — skipping roster sync")
