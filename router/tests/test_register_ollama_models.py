import pytest
from unittest.mock import patch, MagicMock, AsyncMock, mock_open
import os
import json

from router import main as router_main
from router.main import _register_ollama_models_in_db

@pytest.mark.asyncio
async def test_register_ollama_models_in_db_no_master_key():
    """Test that it skips execution if master_key is empty."""
    with patch("router.main.logger.warning") as mock_warning:
        await _register_ollama_models_in_db("")
        mock_warning.assert_called_once_with("No LiteLLM master key provided — skipping Ollama DB registration")

@pytest.mark.asyncio
@patch("router.main.get_http_client")
@patch("router.main._purge_stale_deployments", new_callable=AsyncMock)
@patch("os.getenv")
@patch("os.path.exists")
async def test_register_ollama_models_in_db_static_fallback_success(
    mock_exists, mock_getenv, mock_purge, mock_get_client
):
    """Test static fallback when config file doesn't exist, and successful POST requests."""
    mock_getenv.side_effect = lambda k, default=None: "postgres://fake" if k == "DATABASE_URL" else default
    mock_exists.return_value = False # No config file found

    mock_client = MagicMock()
    mock_post = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response
    mock_client.post = mock_post
    mock_get_client.return_value = mock_client

    with patch("router.main.logger.info") as mock_info:
        await _register_ollama_models_in_db("fake-key")

        # 2 models in static fallback
        assert mock_post.call_count == 2

        # Verify headers and correct url
        call_args = mock_post.call_args_list[0]
        assert call_args[0][0].endswith("/model/new")
        assert call_args[1]["headers"] == {"Authorization": "Bearer fake-key", "Content-Type": "application/json"}
        assert "ollama-deepseek-v4-pro" in call_args[1]["json"]["model_name"]

        mock_purge.assert_called_once_with("postgres://fake", "ollama-deepseek-%")
        mock_info.assert_any_call("📊 Ollama DB registration: 2 registered, 0 failed")


@pytest.mark.asyncio
@patch("router.main.get_http_client")
@patch("router.main._purge_stale_deployments", new_callable=AsyncMock)
@patch("os.getenv")
@patch("os.path.exists")
async def test_register_ollama_models_in_db_load_from_config(
    mock_exists, mock_getenv, mock_purge, mock_get_client
):
    """Test loading models from config file."""
    mock_getenv.return_value = None # No db url
    mock_exists.return_value = True # Config file exists

    mock_yaml_data = """
model_list:
  - model_name: ollama-deepseek-test
    litellm_params:
      model: ollama_chat/test
  - model_name: other-model
    litellm_params:
      model: other
"""

    mock_client = MagicMock()
    mock_post = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_post.return_value = mock_response
    mock_client.post = mock_post
    mock_get_client.return_value = mock_client

    with patch("builtins.open", mock_open(read_data=mock_yaml_data)):
        with patch("router.main.logger.info") as mock_info:
            await _register_ollama_models_in_db("fake-key")

            # Only ollama-deepseek-* is registered
            assert mock_post.call_count == 1
            call_args = mock_post.call_args_list[0]
            assert call_args[1]["json"]["model_name"] == "ollama-deepseek-test"

            mock_purge.assert_not_called()
            mock_info.assert_any_call("📊 Ollama DB registration: 1 registered, 0 failed")


@pytest.mark.asyncio
@patch("router.main.get_http_client")
@patch("router.main._purge_stale_deployments", new_callable=AsyncMock)
@patch("os.path.exists")
async def test_register_ollama_models_in_db_post_failures(
    mock_exists, mock_purge, mock_get_client
):
    """Test handling of failed POST requests."""
    mock_exists.return_value = False # Static fallback

    mock_client = MagicMock()
    mock_post = AsyncMock()

    # First call returns 400, second call raises exception
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = "Bad Request"

    mock_post.side_effect = [mock_response, Exception("Network Error")]
    mock_client.post = mock_post
    mock_get_client.return_value = mock_client

    with patch("router.main.logger.warning") as mock_warning:
        with patch("router.main.logger.info") as mock_info:
            await _register_ollama_models_in_db("fake-key")

            assert mock_post.call_count == 2
            mock_info.assert_any_call("📊 Ollama DB registration: 0 registered, 2 failed")

            warnings = [call[0][0] for call in mock_warning.call_args_list]
            assert any("HTTP 400" in w for w in warnings)
            assert any("Failed to register" in w and "Network Error" in w for w in warnings)


@pytest.mark.asyncio
@patch("router.main.get_http_client")
@patch("router.main._purge_stale_deployments", new_callable=AsyncMock)
@patch("os.getenv")
@patch("os.path.exists")
async def test_register_ollama_models_in_db_purge_failure(
    mock_exists, mock_getenv, mock_purge, mock_get_client
):
    """Test handling of DB purge failure."""
    mock_getenv.side_effect = lambda k, default=None: "postgres://fake" if k == "DATABASE_URL" else default
    mock_exists.return_value = False

    mock_purge.side_effect = Exception("DB Timeout")

    mock_client = MagicMock()
    mock_post = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response
    mock_client.post = mock_post
    mock_get_client.return_value = mock_client

    with patch("router.main.logger.warning") as mock_warning:
        await _register_ollama_models_in_db("fake-key")

        # Should continue even if purge fails
        assert mock_post.call_count == 2

        warnings = [call[0][0] for call in mock_warning.call_args_list]
        assert any("Failed to purge stale ollama DB entries" in w and "DB Timeout" in w for w in warnings)
