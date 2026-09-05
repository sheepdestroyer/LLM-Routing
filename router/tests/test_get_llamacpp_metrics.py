import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import router.main
from router.main import get_llamacpp_metrics


@pytest.fixture(autouse=True)
def reset_llamacpp_cache():
    router.main.llamacpp_metrics_cache = {"data": None, "last_fetched": 0.0}
    yield
    router.main.llamacpp_metrics_cache = {"data": None, "last_fetched": 0.0}


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
                "meta": {"n_params": 1000000, "n_ctx": 2048, "size": 2000000, "n_embd": 512},
            }
        ]
    }

    # 2. /props response
    props_response = MagicMock(status_code=200)
    props_response.json.return_value = {"build_info": "1.0.0-mock"}

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
            "speculative": False,
        },
        {
            "id": 2,
            "next_token": [{"n_decoded": 20}],  # test list format
        },
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
    # Test when models endpoint works with loaded model, but props and slots fail
    models_response = MagicMock(status_code=200)
    models_response.json.return_value = {"data": [{"id": "model-1", "status": {"value": "loaded"}}]}

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
    assert result["models"][0]["status"] == "loaded"
    assert len(result["slots"]) == 0


@pytest.mark.asyncio
async def test_get_llamacpp_metrics_unloaded_model_skips_slots(mock_http_client):
    # Verify that when models are unloaded, /slots is never queried to prevent triggering model load
    models_response = MagicMock(status_code=200)
    models_response.json.return_value = {"data": [{"id": "model-1", "status": {"value": "unloaded"}}]}
    props_response = MagicMock(status_code=200)
    props_response.json.return_value = {"build_info": "1.0.0"}

    def mock_get(url, *args, **kwargs):
        if url.endswith("/v1/models"):
            return models_response
        elif url.endswith("/props"):
            return props_response
        else:
            return MagicMock(status_code=500)

    mock_http_client.get.side_effect = mock_get

    result = await get_llamacpp_metrics()

    assert result["build"] == "1.0.0"
    assert len(result["models"]) == 1
    assert result["models"][0]["status"] == "unloaded"
    assert len(result["slots"]) == 0
    # Should only call /v1/models and /props, never /slots
    called_urls = [call[0][0] for call in mock_http_client.get.call_args_list]
    assert not any("/slots" in u for u in called_urls)


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


@pytest.mark.asyncio
async def test_get_llamacpp_metrics_cache_hit_and_force_refresh(mock_http_client):
    models_response = MagicMock(status_code=200)
    models_response.json.return_value = {"data": [{"id": "model-1", "status": {"value": "loaded"}}]}
    props_response = MagicMock(status_code=200)
    props_response.json.return_value = {"build_info": "v1"}
    slots_response = MagicMock(status_code=200)
    slots_response.json.return_value = []

    def mock_get(url, *args, **kwargs):
        if url.endswith("/v1/models"):
            return models_response
        elif url.endswith("/props"):
            return props_response
        elif "/slots" in url:
            return slots_response
        return MagicMock(status_code=404)

    mock_http_client.get.side_effect = mock_get

    # First call - populates cache
    res1 = await get_llamacpp_metrics()
    assert res1["build"] == "v1"
    initial_call_count = mock_http_client.get.call_count
    assert initial_call_count > 0

    # Second call within TTL - returns cached without extra network calls
    res2 = await get_llamacpp_metrics()
    assert res2 == res1
    assert mock_http_client.get.call_count == initial_call_count

    # Third call with force_refresh=True - bypasses cache
    res3 = await get_llamacpp_metrics(force_refresh=True)
    assert res3["build"] == "v1"
    assert mock_http_client.get.call_count > initial_call_count


@pytest.mark.asyncio
async def test_get_llamacpp_metrics_cache_expiry(mock_http_client):
    models_response = MagicMock(status_code=200)
    models_response.json.return_value = {"data": []}
    mock_http_client.get.return_value = models_response

    # Populate cache with old timestamp
    res1 = await get_llamacpp_metrics()
    router.main.llamacpp_metrics_cache["last_fetched"] = 100.0  # long ago
    initial_count = mock_http_client.get.call_count

    with patch("time.time", return_value=200.0):  # 100 seconds later > TTL
        res2 = await get_llamacpp_metrics()
        assert mock_http_client.get.call_count > initial_count


@pytest.mark.asyncio
async def test_get_llamacpp_metrics_exception_fallback_to_cache(mock_http_client):
    # Set up existing cached data
    cached_data = {"models": [{"id": "cached-model"}], "slots": [], "build": "v-cached"}
    router.main.llamacpp_metrics_cache["data"] = cached_data
    router.main.llamacpp_metrics_cache["last_fetched"] = 0.0

    mock_http_client.get.side_effect = Exception("Temporary network glitch")

    with patch("router.main.logger.warning") as mock_logger:
        result = await get_llamacpp_metrics(force_refresh=True)
        mock_logger.assert_called_once()
        assert result == cached_data
