import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import httpx

from router.main import get_llamacpp_metrics

@pytest.fixture
def mock_http_client():
    with patch("router.main.get_llama_client") as mock:
        client = AsyncMock()
        mock.return_value = client
        yield client

@pytest.mark.asyncio
async def test_get_llamacpp_metrics_success(mock_http_client):
    # Mock responses for all endpoints

    # 1. /v1/models response
    models_response = MagicMock(status_code=200)
    models_response.json.return_value = {
        "data": [
            {
                "id": "model-1",
                "status": {"value": "loaded"},
                "meta": {
                    "n_params": 1000000,
                    "n_ctx": 2048,
                    "size": 2000000,
                    "n_embd": 512
                }
            }
        ]
    }

    # 2. /props response
    props_response = MagicMock(status_code=200)
    props_response.json.return_value = {
        "build_info": "1.0.0-mock"
    }

    # 3. /slots response
    slots_response = MagicMock(status_code=200)
    slots_response.json.return_value = [
        {
            "id": 1,
            "is_processing": True,
            "n_ctx": 2048,
            "n_prompt_tokens": 100,
            "n_prompt_tokens_processed": 50,
            "next_token": {"n_decoded": 10},
            "speculative": False
        },
        {
             "id": 2,
             "next_token": [{"n_decoded": 20}] # test list format
        }
    ]

    # 4. /metrics response (to satisfy potential legacy references)
    metrics_response = MagicMock(status_code=200)
    metrics_response.json.return_value = {}

    def mock_get(url, *args, **kwargs):
        if url.endswith("/v1/models"):
            return models_response
        elif url.endswith("/props"):
            return props_response
        elif url.endswith("/slots?model=model-1"):
            return slots_response
        elif url.endswith("/metrics"):
            return metrics_response
        else:
            return MagicMock(status_code=404)

    mock_http_client.get.side_effect = mock_get

    result = await get_llamacpp_metrics()

    assert result["build"] == "1.0.0-mock"
    assert len(result["models"]) == 1
    assert result["models"][0]["id"] == "model-1"
    assert result["models"][0]["status"] == "loaded"
    assert result["models"][0]["n_params"] == 1000000

    assert len(result["slots"]) == 2
    assert result["slots"][0]["id"] == 1
    assert result["slots"][0]["is_processing"] is True
    assert result["slots"][0]["n_decoded"] == 10
    assert result["slots"][1]["n_decoded"] == 20

@pytest.mark.asyncio
async def test_get_llamacpp_metrics_partial(mock_http_client):
    # Test when only models endpoint works, others fail

    # 1. /v1/models response
    models_response = MagicMock(status_code=200)
    models_response.json.return_value = {
        "data": [
            {
                "id": "model-1",
                "status": {"value": "unloaded"}
            }
        ]
    }

    def mock_get(url, *args, **kwargs):
        if url.endswith("/v1/models"):
            return models_response
        else:
            return MagicMock(status_code=500)

    mock_http_client.get.side_effect = mock_get

    result = await get_llamacpp_metrics()

    assert result["build"] == "unknown"
    assert len(result["models"]) == 1
    assert result["models"][0]["id"] == "model-1"
    assert result["models"][0]["status"] == "unloaded"

    assert len(result["slots"]) == 0

@pytest.mark.asyncio
async def test_get_llamacpp_metrics_no_models(mock_http_client):
    models_response = MagicMock(status_code=200)
    models_response.json.return_value = {"data": []}

    def mock_get(url, *args, **kwargs):
        return models_response

    mock_http_client.get.side_effect = mock_get

    result = await get_llamacpp_metrics()
    assert result["models"] == []
    assert result["slots"] == []

@pytest.mark.asyncio
async def test_get_llamacpp_metrics_exception(mock_http_client):
    # Test when an exception is raised (e.g., network timeout)

    mock_http_client.get.side_effect = Exception("Connection error")

    with patch("router.main.logger.warning") as mock_logger:
        result = await get_llamacpp_metrics()

        # Verify the exception was caught and logged
        mock_logger.assert_called_once()
        assert "Failed to fetch llama.cpp metrics: Connection error" in mock_logger.call_args[0][0]

        # Verify it returns the default structure
        assert result == {"models": [], "slots": [], "build": "unknown"}
