import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import os

from router.main import _register_openrouter_models_in_db


@pytest.fixture
def mock_env():
    with patch.dict(os.environ, {
        "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
        "ROUTER_API_KEY": "test_api_key"
    }, clear=False):
        yield


@pytest.mark.asyncio
async def test_register_openrouter_models_no_master_key(mock_env, caplog):
    await _register_openrouter_models_in_db(None)
    assert "No LiteLLM master key provided" in caplog.text


@pytest.mark.asyncio
@patch("router.main.get_http_client")
@patch("router.main._purge_stale_deployments")
@patch("builtins.open", side_effect=FileNotFoundError("Mocked file not found"))
async def test_register_openrouter_models_static_fallback(mock_open, mock_purge, mock_get_client, mock_env):
    mock_client = AsyncMock()
    mock_get_client.return_value = mock_client

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.post.return_value = mock_response

    await _register_openrouter_models_in_db("test_master_key")

    # Should attempt to purge DB for openrouter-% and non-openrouter- prefixed models
    assert mock_purge.call_count == 2
    purge_calls = {(call.args[0], call.args[1]) for call in mock_purge.call_args_list}
    expected_purge_calls = {
        ("postgresql://test:test@localhost:5432/test", "openrouter-%"),
        ("postgresql://test:test@localhost:5432/test", "gpt-5.6-luna"),
    }
    assert purge_calls == expected_purge_calls

    # Should post for openrouter-auto, openrouter-gpt-5.6-luna, openrouter-gpt-5.6-luna-max, and gpt-5.6-luna static fallback
    expected_models = {
        "openrouter-auto",
        "openrouter-gpt-5.6-luna",
        "openrouter-gpt-5.6-luna-max",
        "gpt-5.6-luna",
    }
    assert mock_client.post.call_count == len(expected_models)
    posted_models = {
        call.kwargs.get("json", {}).get("model_name") or call[1].get("json", {}).get("model_name")
        for call in mock_client.post.call_args_list
    }
    assert posted_models == expected_models


@pytest.mark.asyncio
@patch("router.main.get_http_client")
@patch("router.main._purge_stale_deployments")
async def test_register_openrouter_models_from_config(mock_purge, mock_get_client, mock_env):
    mock_client = AsyncMock()
    mock_get_client.return_value = mock_client

    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_client.post.return_value = mock_response

    mock_config = {
        "model_list": [
            {
                "model_name": "openrouter-auto",
                "litellm_params": {"model": "openrouter/openrouter/auto"}
            },
            {
                "model_name": "gpt-4o-mini-tts",
                "litellm_params": {"model": "openrouter/openai/tts-1"}
            },
            {
                "model_name": "local-qwen",
                "litellm_params": {"model": "openai/local-qwen"}
            }
        ]
    }

    with patch("router.main.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
        mock_to_thread.return_value = mock_config
        await _register_openrouter_models_in_db("test_master_key")

        assert mock_to_thread.call_count > 0

    assert mock_client.post.call_count == 2
    payload_names = [call[1]['json']['model_name'] for call in mock_client.post.call_args_list]
    assert "openrouter-auto" in payload_names
    assert "gpt-4o-mini-tts" in payload_names
