import pytest
from unittest.mock import patch, AsyncMock
from fastapi import Request, Response, HTTPException
from router.main import proxy_memory
import os

@pytest.fixture
def mock_request():
    request = AsyncMock(spec=Request)
    request.method = "POST"
    request.query_params = {"foo": "bar"}
    request.body = AsyncMock(return_value=b'{"key": "value"}')
    request.headers = {"content-type": "application/json", "custom-header": "custom-value"}
    return request

@pytest.fixture
def mock_response():
    response = AsyncMock()
    response.status_code = 200
    response.content = b'{"result": "success"}'
    response.headers = {
        "content-type": "application/json",
        "content-encoding": "gzip",
        "content-length": "100",
        "transfer-encoding": "chunked",
        "connection": "keep-alive",
        "custom-response-header": "test"
    }
    return response

@pytest.mark.asyncio
async def test_proxy_memory_success(mock_request, mock_response):
    with patch("router.main.get_http_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)
        mock_get_client.return_value = mock_client

        with patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test_key"}):
            response = await proxy_memory(mock_request, path="/test")

        assert isinstance(response, Response)
        assert response.status_code == 200
        assert response.body == b'{"result": "success"}'

        # Verify client.request was called correctly
        mock_client.request.assert_called_once_with(
            method="POST",
            url="http://127.0.0.1:4000/v1/memory/test",
            params={"foo": "bar"},
            content=b'{"key": "value"}',
            headers={
                "Authorization": "Bearer test_key",
                "Content-Type": "application/json"
            },
            timeout=30.0
        )

        # Verify filtered headers
        assert "content-encoding" not in response.headers
        # assert "content-length" not in response.headers (FastAPI manages it)
        assert "transfer-encoding" not in response.headers
        assert "connection" not in response.headers

        # Verify preserved headers
        assert response.headers["custom-response-header"] == "test"

@pytest.mark.asyncio
async def test_proxy_memory_exception(mock_request):
    with patch("router.main.get_http_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=Exception("Test error"))
        mock_get_client.return_value = mock_client

        with patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test_key"}):
            with pytest.raises(HTTPException) as exc_info:
                await proxy_memory(mock_request, path="/test")

            assert exc_info.value.status_code == 502
            assert "Memory proxy failed" in str(exc_info.value.detail)
            assert "Test error" in str(exc_info.value.detail)
