import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import os
import httpx

from router.main import _register_ollama_models_in_db

@pytest.fixture
def mock_env():
    # Make sure we don't blow up router.main with missing env vars
    with patch.dict(os.environ, {
        "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
        "ROUTER_API_KEY": "test_api_key"
    }, clear=False):
        yield

@pytest.mark.asyncio
async def test_register_ollama_models_no_master_key(mock_env, caplog):
    await _register_ollama_models_in_db(None)
    assert "No LiteLLM master key provided" in caplog.text

@pytest.mark.asyncio
@patch("router.main.get_http_client")
@patch("router.main._purge_stale_deployments")
@patch("builtins.open", side_effect=FileNotFoundError("Mocked file not found"))
async def test_register_ollama_models_static_fallback(mock_open, mock_purge, mock_get_client, mock_env):
    mock_client = AsyncMock()
    mock_get_client.return_value = mock_client

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.post.return_value = mock_response

    await _register_ollama_models_in_db("test_master_key")

    # Should attempt to purge DB
    mock_purge.assert_called_once_with("postgresql://test:test@localhost:5432/test", "ollama-deepseek-%")

    # Should post for both static models
    assert mock_client.post.call_count == 2

    calls = mock_client.post.call_args_list
    assert "ollama-deepseek-v4-pro" in str(calls[0])
    assert "ollama-deepseek-v4-flash" in str(calls[1])

@pytest.mark.asyncio
@patch("router.main.get_http_client")
@patch("router.main._purge_stale_deployments")
async def test_register_ollama_models_from_config(mock_purge, mock_get_client, mock_env):
    mock_client = AsyncMock()
    mock_get_client.return_value = mock_client

    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_client.post.return_value = mock_response

    # Mock asyncio.to_thread to bypass open() and yaml.safe_load
    mock_config = {
        "model_list": [
            {
                "model_name": "ollama-deepseek-test-model",
                "litellm_params": {"model": "ollama_chat/deepseek-test-model"}
            },
            {
                "model_name": "ignore-this-model",
            }
        ]
    }

    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
        mock_to_thread.return_value = mock_config
        await _register_ollama_models_in_db("test_master_key")

        # Verify it attempted to load from config
        assert mock_to_thread.call_count > 0

    assert mock_client.post.call_count == 1
    call_args = mock_client.post.call_args_list[0]
    payload = call_args[1]['json']
    assert payload['model_name'] == "ollama-deepseek-test-model"

@pytest.mark.asyncio
@patch("router.main.get_http_client")
@patch("router.main._purge_stale_deployments")
@patch("builtins.open", side_effect=FileNotFoundError("Mocked file not found"))
async def test_register_ollama_models_http_failure(mock_open, mock_purge, mock_get_client, mock_env, caplog):
    mock_client = AsyncMock()
    mock_get_client.return_value = mock_client

    # First request fails with HTTP 500
    mock_response_fail = MagicMock()
    mock_response_fail.status_code = 500
    mock_response_fail.text = "Internal Server Error"

    # Second request fails with exception
    mock_client.post.side_effect = [mock_response_fail, httpx.RequestError("Network error", request=MagicMock())]

    await _register_ollama_models_in_db("test_master_key")

    assert "HTTP 500" in caplog.text
    assert "Failed to register ollama-deepseek-v4-flash" in caplog.text
    assert mock_client.post.call_count == 2

@pytest.mark.asyncio
@patch("router.main.get_http_client")
@patch("router.main._purge_stale_deployments")
@patch("builtins.open", side_effect=FileNotFoundError("Mocked file not found"))
async def test_register_ollama_models_db_url_missing(mock_open, mock_purge, mock_get_client, caplog):
    # Test skipping DB purge if DATABASE_URL is missing
    with patch.dict(os.environ, {"ROUTER_API_KEY": "test_api_key"}):
        if "DATABASE_URL" in os.environ:
            del os.environ["DATABASE_URL"]

        mock_client = AsyncMock()
        mock_get_client.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post.return_value = mock_response

        await _register_ollama_models_in_db("test_master_key")

        assert "DATABASE_URL is not set; skipping purge" in caplog.text
        mock_purge.assert_not_called()

@pytest.mark.asyncio
@patch("router.main.get_http_client")
@patch("router.main._purge_stale_deployments")
@patch("builtins.open", side_effect=FileNotFoundError("Mocked file not found"))
async def test_register_ollama_models_db_purge_error(mock_open, mock_purge, mock_get_client, mock_env, caplog):
    # Test handling DB purge exception
    mock_client = AsyncMock()
    mock_get_client.return_value = mock_client
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.post.return_value = mock_response

    mock_purge.side_effect = Exception("DB Connection Error")

    await _register_ollama_models_in_db("test_master_key")

    assert "Failed to purge stale ollama DB entries (non-fatal): DB Connection Error" in caplog.text
    assert mock_client.post.call_count == 2

@pytest.mark.asyncio
@patch("router.main.get_http_client")
@patch("router.main._purge_stale_deployments")
@patch("asyncio.to_thread")
async def test_register_ollama_models_config_load_exception(mock_to_thread, mock_purge, mock_get_client, mock_env, caplog):
    mock_client = AsyncMock()
    mock_get_client.return_value = mock_client
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.post.return_value = mock_response

    mock_to_thread.side_effect = Exception("Config parse error")

    await _register_ollama_models_in_db("test_master_key")

    assert "Failed to load/parse LiteLLM config at" in caplog.text
    assert "Could not load Ollama models from config.yaml, falling back to static definitions" in caplog.text
    assert mock_client.post.call_count == 2 # Falls back to static
