import pytest
from unittest.mock import patch, AsyncMock
from fastapi import HTTPException
from fastapi.testclient import TestClient
from router.main import app
import os
import httpx

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture
def mock_response():
    return httpx.Response(
        status_code=200,
        content=b'{"result": "success"}',
        headers={
            "content-type": "application/json",
            "content-encoding": "identity",
            "content-length": "100",
            "transfer-encoding": "chunked",
            "connection": "keep-alive",
            "custom-response-header": "test"
        }
    )

def test_proxy_memory_success(client, mock_response):
    with patch("router.main.get_http_client") as mock_get_client:
        mock_http_client = AsyncMock()
        mock_http_client.request = AsyncMock(return_value=mock_response)
        mock_get_client.return_value = mock_http_client

        with patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test_key"}):
            # Send the request through the TestClient
            response = client.post(
                "/v1/memory/test?foo=bar",
                json={"key": "value"},
                headers={"custom-header": "custom-value"}
            )

        assert response.status_code == 200
        assert response.content == b'{"result": "success"}'

        # Verify client.request was called correctly
        mock_http_client.request.assert_called_once_with(
            method="POST",
            url="http://127.0.0.1:4000/v1/memory/test",
            params={"foo": "bar"},
            content=b'{"key":"value"}',
            headers={
                "Authorization": "Bearer test_key",
                "Content-Type": "application/json"
            },
            timeout=30.0
        )

        # Verify filtered headers
        assert "content-encoding" not in response.headers
        # FastAPI/Starlette will inject content-length, so we do not assert it is absent
        assert "transfer-encoding" not in response.headers
        assert "connection" not in response.headers

        # Verify preserved headers
        assert response.headers["custom-response-header"] == "test"

def test_proxy_memory_exception(client):
    with patch("router.main.get_http_client") as mock_get_client:
        mock_http_client = AsyncMock()
        mock_http_client.request = AsyncMock(side_effect=Exception("Test error"))
        mock_get_client.return_value = mock_http_client

        with patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test_key"}):
            # Send the request through the TestClient
            response = client.post(
                "/v1/memory/test",
                json={"key": "value"}
            )

            assert response.status_code == 502
            assert "Memory proxy failed" in response.json()["detail"]
            assert "Test error" in response.json()["detail"]
