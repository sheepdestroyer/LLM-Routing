import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import Response
from fastapi.responses import JSONResponse
import httpx

from router.main import (
    get_http_client,
    proxy_models,
    HTTP_MAX_CONNECTIONS,
    HTTP_MAX_KEEPALIVE_CONNECTIONS,
    HTTP_KEEPALIVE_EXPIRY,
)


def test_http_client_limits():
    # Verify that get_http_client initializes with configured limits using public mocks
    from router import main

    original_init = httpx.Limits.__init__
    calls = []

    def spy_init(self, *args, **kwargs):
        calls.append((args, kwargs))
        original_init(self, *args, **kwargs)

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
async def test_get_http_client_singleton_identity():
    """Verify that calling get_http_client() twice in succession returns the exact same object reference."""
    from router import main

    original_client = main._http_client
    main._http_client = None
    try:
        client1 = main.get_http_client()
        client2 = main.get_http_client()

        assert client1 is client2
        assert isinstance(client1, httpx.AsyncClient)
    finally:
        if main._http_client is not None and main._http_client is not original_client:
            await main._http_client.aclose()
        main._http_client = original_client


@pytest.mark.anyio
async def test_get_http_client_reset_reinitialization():
    """Verify that when main._http_client is reset to None, calling get_http_client() creates and returns a fresh client instance with proper limits."""
    from router import main

    original_client = main._http_client
    created_clients = []
    try:
        main._http_client = None
        client1 = main.get_http_client()
        created_clients.append(client1)

        # Reset singleton to None
        main._http_client = None

        # Call get_http_client() again; must create and return a fresh client instance
        client2 = main.get_http_client()
        created_clients.append(client2)

        # Assert fresh instance identity
        assert client1 is not client2
        assert isinstance(client2, httpx.AsyncClient)

        # Assert consecutive calls return the new singleton instance
        client3 = main.get_http_client()
        assert client3 is client2

        # Assert proper connection limits configured on the fresh instance
        pool = client2._transport._pool
        assert pool._max_connections == main.HTTP_MAX_CONNECTIONS
        assert pool._max_keepalive_connections == main.HTTP_MAX_KEEPALIVE_CONNECTIONS
        assert pool._keepalive_expiry == main.HTTP_KEEPALIVE_EXPIRY
        assert client2.timeout.read == 3600.0
    finally:
        for c in created_clients:
            if c is not original_client:
                await c.aclose()
        main._http_client = original_client


@pytest.mark.anyio
async def test_proxy_models_success():
    # Mock the AsyncClient.get to return a successful mock response
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": [{"id": "model-a", "object": "model"}]}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp

    with patch("router.main.get_http_client", return_value=mock_client):
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

    with patch("router.main.get_http_client", return_value=mock_client):
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

    with patch("router.main.get_http_client", return_value=mock_client):
        response = await proxy_models()
        assert isinstance(response, Response)
        assert response.status_code == 200
        assert response.body == b"not a json"


def test_litellm_config_local_model_context_limits():
    """Verify local models in litellm/config.yaml have correct context limits (240896)."""
    import yaml
    from pathlib import Path

    config_path = Path(__file__).resolve().parent.parent / "litellm" / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    models_by_name = {m["model_name"]: m for m in config.get("model_list", []) if "model_name" in m}

    assert "locallama-qwen" in models_by_name
    assert models_by_name["locallama-qwen"]["model_info"]["max_tokens"] == 240896
    assert models_by_name["locallama-qwen"]["model_info"]["max_input_tokens"] == 240896

    assert "locallama-qwen-hass" in models_by_name
    assert models_by_name["locallama-qwen-hass"]["model_info"]["max_tokens"] == 240896
    assert models_by_name["locallama-qwen-hass"]["model_info"]["max_input_tokens"] == 240896

    assert "gpt-4o-mini" in models_by_name
    assert models_by_name["gpt-4o-mini"]["model_info"]["max_tokens"] == 240896

    assert "gpt-4o" in models_by_name
    assert models_by_name["gpt-4o"]["model_info"]["max_tokens"] == 240896

    assert "locallama-qwen-routing" in models_by_name
    assert models_by_name["locallama-qwen-routing"]["model_info"]["max_tokens"] == 8192

    assert "locallama-qwen-vl" in models_by_name
    assert models_by_name["locallama-qwen-vl"]["model_info"]["max_tokens"] == 65536
    assert models_by_name["locallama-qwen-vl"]["model_info"]["supports_vision"] is True

    assert "local-qwen" in models_by_name
    assert models_by_name["local-qwen"]["model_info"]["max_tokens"] == 240896
    assert models_by_name["local-qwen"]["model_info"]["max_input_tokens"] == 240896

    assert "local-qwen-hass" in models_by_name
    assert models_by_name["local-qwen-hass"]["model_info"]["max_tokens"] == 240896
    assert models_by_name["local-qwen-hass"]["model_info"]["max_input_tokens"] == 240896

    assert "local-qwen-routing" in models_by_name
    assert models_by_name["local-qwen-routing"]["model_info"]["max_tokens"] == 8192

    assert "locallama-nomic-embed" in models_by_name
    assert models_by_name["locallama-nomic-embed"]["model_info"]["mode"] == "embedding"

    assert "local-nomic-embed" in models_by_name
    assert models_by_name["local-nomic-embed"]["model_info"]["mode"] == "embedding"

    assert "local-qwen-vl" in models_by_name
    assert models_by_name["local-qwen-vl"]["model_info"]["max_tokens"] == 65536
    assert models_by_name["local-qwen-vl"]["model_info"]["supports_vision"] is True
