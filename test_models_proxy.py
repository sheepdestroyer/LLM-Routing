import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import Response
from fastapi.responses import JSONResponse

# Set CONFIG_PATH for import
os.environ["CONFIG_PATH"] = "router/config.yaml"

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "router"))

from main import get_http_client, proxy_models, HTTP_MAX_CONNECTIONS, HTTP_MAX_KEEPALIVE_CONNECTIONS, HTTP_KEEPALIVE_EXPIRY

def test_http_client_limits():
    # Verify that get_http_client initializes with configured limits using public mocks
    import main
    import httpx
<<<<<<< HEAD

    original_init = httpx.Limits.__init__
    calls = []

    def spy_init(self, *args, **kwargs):
        calls.append((args, kwargs))
        original_init(self, *args, **kwargs)

=======

    original_init = httpx.Limits.__init__
    calls = []

    def spy_init(self, *args, **kwargs):
        calls.append((args, kwargs))
        original_init(self, *args, **kwargs)

>>>>>>> origin/master
    original_client = main._http_client
    main._http_client = None
    try:
        with patch.object(httpx.Limits, "__init__", new=spy_init):
            main.get_http_client()
            assert len(calls) == 1
            args, kwargs = calls[0]
            assert kwargs.get("max_connections") == main.HTTP_MAX_CONNECTIONS
            assert kwargs.get("max_keepalive_connections") == main.HTTP_MAX_KEEPALIVE_CONNECTIONS
            assert kwargs.get("keepalive_expiry") == main.HTTP_KEEPALIVE_EXPIRY
    finally:
        main._http_client = original_client



@pytest.mark.anyio
async def test_proxy_models_success():
    # Mock the AsyncClient.get to return a successful mock response
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": [{"id": "model-a", "object": "model"}]}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp

    with patch("main.get_http_client", return_value=mock_client):
        response = await proxy_models()
        assert isinstance(response, JSONResponse)
        assert response.status_code == 200

        # Verify that the response contains injected models
        import json
        body = json.loads(response.body)
        model_ids = [m["id"] for m in body["data"]]
        assert "llm-routing-auto-free" in model_ids
        assert "llm-routing-auto-agy" in model_ids
        assert "model-a" in model_ids

@pytest.mark.anyio
async def test_proxy_models_error_status():
    # LiteLLM returns a 500 error
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.content = b"Internal Server Error"
    mock_resp.headers = {"Content-Type": "text/plain"}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp

    with patch("main.get_http_client", return_value=mock_client):
        response = await proxy_models()
        assert isinstance(response, Response)
        assert response.status_code == 500
        assert response.body == b"Internal Server Error"

@pytest.mark.anyio
async def test_proxy_models_invalid_json():
    # LiteLLM returns 200 but invalid/malformed JSON structure
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = ValueError("Invalid JSON")
    mock_resp.content = b"not a json"
    mock_resp.headers = {"Content-Type": "text/plain"}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp

    with patch("main.get_http_client", return_value=mock_client):
        response = await proxy_models()
        assert isinstance(response, Response)
        assert response.status_code == 200
        assert response.body == b"not a json"
