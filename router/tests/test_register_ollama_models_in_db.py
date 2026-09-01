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
@patch("builtins.open", side_effect=FileNotFoundError("Mocked file not found"))
async def test_register_ollama_models_static_fallback(mock_open, mock_get_client, mock_env):
    mock_client = AsyncMock()
    mock_get_client.return_value = mock_client

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.post.return_value = mock_response

    await _register_ollama_models_in_db("test_master_key")

    # Should post for all 6 static models
    expected_models = {
        "ollama-deepseek-v4-pro",
        "ollama-deepseek-v4-flash",
        "ollama/GPT-5.6 Luna (max)",
        "ollama-gpt-5.6-luna-max",
        "ollama/gpt-5.6-luna",
        "ollama-gpt-5.6-luna",
    }
    assert mock_client.post.call_count == len(expected_models)

    posted_models = {
        call.kwargs.get("json", {}).get("model_name") or call[1].get("json", {}).get("model_name")
        for call in mock_client.post.call_args_list
    }
    assert posted_models == expected_models

@pytest.mark.asyncio
@patch("router.main.get_http_client")
async def test_register_ollama_models_from_config(mock_get_client, mock_env):
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
                "model_name": "ollama/GPT-5.6 Luna (max)",
                "litellm_params": {"model": "ollama_chat/gpt-5.6-luna"}
            },
            {
                "model_name": "ignore-this-model",
            }
        ]
    }

    with patch("router.main.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
        mock_to_thread.return_value = mock_config
        await _register_ollama_models_in_db("test_master_key")

        # Verify it attempted to load from config
        assert mock_to_thread.call_count > 0

    assert mock_client.post.call_count == 2
    posted_models = {
        call.kwargs.get("json", {}).get("model_name") or call[1].get("json", {}).get("model_name")
        for call in mock_client.post.call_args_list
    }
    assert posted_models == {"ollama-deepseek-test-model", "ollama/GPT-5.6 Luna (max)"}

@pytest.mark.asyncio
@patch("router.main.get_http_client")
@patch("builtins.open", side_effect=FileNotFoundError("Mocked file not found"))
async def test_register_ollama_models_http_failure(mock_open, mock_get_client, mock_env, caplog):
    mock_client = AsyncMock()
    mock_get_client.return_value = mock_client

    # First request fails with HTTP 500
    mock_response_fail = MagicMock()
    mock_response_fail.status_code = 500
    mock_response_fail.text = "Internal Server Error"

    # Subsequent requests fail or error
    mock_client.post.side_effect = [
        mock_response_fail,
        httpx.RequestError("Network error", request=MagicMock()),
        mock_response_fail,
        mock_response_fail,
        mock_response_fail,
        mock_response_fail,
    ]

    await _register_ollama_models_in_db("test_master_key")

    assert "HTTP 500" in caplog.text
    assert "Failed to register ollama-deepseek-v4-flash" in caplog.text
    assert mock_client.post.call_count == 6

@pytest.mark.asyncio
@patch("router.main.get_http_client")
@patch("router.main.asyncio.to_thread")
async def test_register_ollama_models_config_load_exception(mock_to_thread, mock_get_client, mock_env, caplog):
    mock_client = AsyncMock()
    mock_get_client.return_value = mock_client
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.post.return_value = mock_response

    mock_to_thread.side_effect = Exception("Config parse error")

    await _register_ollama_models_in_db("test_master_key")

    assert "Failed to load/parse LiteLLM config at" in caplog.text
    assert "Could not load Ollama models from config.yaml, falling back to static definitions" in caplog.text
    assert mock_client.post.call_count == 6 # Falls back to static
