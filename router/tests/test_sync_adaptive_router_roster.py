import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.main import sync_adaptive_router_roster

@pytest.fixture
def mock_http_client():
    mock_client = AsyncMock()

    # Setup mock get response
    mock_get_response = MagicMock()
    mock_get_response.status_code = 200
    mock_get_response.json.return_value = {
        "data": [
            {
                "id": "good-model-1",
                "supported_parameters": ["tools"],
                "pricing": {"prompt": "0", "completion": "0"},
                "context_length": 128000
            },
            {
                "id": "good-model-2",
                "supported_parameters": ["tools"],
                "pricing": {"prompt": "0", "completion": "0"},
                "context_length": 8192
            },
            {
                "id": "bad-no-tools",
                "supported_parameters": [],
                "pricing": {"prompt": "0", "completion": "0"}
            },
            {
                "id": "meta-llama/bad-denylist",
                "supported_parameters": ["tools"],
                "pricing": {"prompt": "0", "completion": "0"}
            },
            {
                "id": "paid-model",
                "supported_parameters": ["tools"],
                "pricing": {"prompt": "0.01", "completion": "0.02"}
            }
        ]
    }
    mock_client.get.return_value = mock_get_response

    # Setup mock post response
    mock_post_response = MagicMock()
    mock_post_response.status_code = 200
    mock_client.post.return_value = mock_post_response

    return mock_client


@pytest.mark.asyncio
@patch("router.main.get_http_client")
async def test_sync_no_master_key(mock_get_http_client):
    """Test early return when no master_key is provided."""
    await sync_adaptive_router_roster("")
    mock_get_http_client.assert_not_called()


@pytest.mark.asyncio
@patch("router.main.get_http_client")
async def test_sync_http_get_fails(mock_get_http_client):
    """Test when fetching models returns a non-200 status."""
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_client.get.return_value = mock_response
    mock_get_http_client.return_value = mock_client

    await sync_adaptive_router_roster("dummy-key")

    mock_client.get.assert_called_once()
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
@patch("router.main.get_http_client")
async def test_sync_http_get_exception(mock_get_http_client):
    """Test when fetching models throws an exception."""
    mock_client = AsyncMock()
    mock_client.get.side_effect = Exception("API error")
    mock_get_http_client.return_value = mock_client

    await sync_adaptive_router_roster("dummy-key")

    mock_client.get.assert_called_once()
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
@patch("router.main.get_http_client")
async def test_sync_no_free_models(mock_get_http_client):
    """Test early return when no free models are found."""
    mock_client = AsyncMock()


@pytest.mark.asyncio
@patch("router.main.get_http_client")
async def test_sync_free_models_all_filtered(mock_get_http_client):
    """Test early return when free models exist but are all filtered out by validation."""
    mock_client = AsyncMock()

    # Only free models returned, but all are invalid for routing (e.g., no tools support)
    mock_get_response = MagicMock()
    mock_get_response.status_code = 200
    mock_get_response.json.return_value = {
        "data": [
            {
                "id": "free-invalid-1",
                "supported_parameters": [],
                "pricing": {"prompt": "0", "completion": "0"},
            },
            {
                "id": "free-invalid-2",
                # missing required tools / supported parameters for routing
                "supported_parameters": ["unsupported-capability"],
                "pricing": {"prompt": "0", "completion": "0"},
            },
        ]
    }
    mock_client.get.return_value = mock_get_response

    # POST should not be called if all free models are filtered out
    mock_post_response = MagicMock()
    mock_post_response.status_code = 200
    mock_client.post.return_value = mock_post_response

    mock_get_http_client.return_value = mock_client

    await sync_adaptive_router_roster("dummy-key")

    mock_client.get.assert_called_once()
    mock_client.post.assert_not_called()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [
            {
                "id": "paid-model",
                "supported_parameters": ["tools"],
                "pricing": {"prompt": "0.01", "completion": "0.02"}
            }
        ]
    }
    mock_client.get.return_value = mock_response
    mock_get_http_client.return_value = mock_client

    await sync_adaptive_router_roster("dummy-key")

    mock_client.get.assert_called_once()
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
@patch("router.main._purge_stale_deployments", new_callable=AsyncMock)
@patch("router.main.get_http_client")
@patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"})
async def test_sync_success(mock_get_http_client, mock_purge, mock_http_client):
    """Test successful roster sync with cascading tiers and DB purge."""
    mock_get_http_client.return_value = mock_http_client

    await sync_adaptive_router_roster("dummy-key")

    # Verify OpenRouter GET
    mock_http_client.get.assert_called_once()
    mock_http_client.get.assert_called_once_with("https://openrouter.ai/api/v1/models", timeout=5.0)
    # Verify purge is called since DATABASE_URL is mocked
    mock_purge.assert_called_once_with("postgresql://test", "agent-%")

    # Verify POST requests for model registration
    assert mock_http_client.post.call_count > 0

    # Check that bad models were skipped by verifying the payloads
    posted_models = []
    for call in mock_http_client.post.call_args_list:
        kwargs = call[1]
        json_payload = kwargs.get("json", {})
        litellm_params = json_payload.get("litellm_params", {})
        model_name = litellm_params.get("model", "")
        posted_models.append(model_name)

    # Ensure good models are posted, bad models are skipped
    assert any("good-model-1" in m for m in posted_models)
    assert any("good-model-2" in m for m in posted_models)
    assert not any("bad-no-tools" in m for m in posted_models)
    assert not any("meta-llama" in m for m in posted_models)
    assert not any("paid-model" in m for m in posted_models)
