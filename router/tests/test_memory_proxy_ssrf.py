import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from router.main import app, proxy_memory


@pytest.mark.asyncio
async def test_proxy_memory_ssrf_path_traversal():
    """Test that path traversal attempts (..) trigger 400 Bad Request."""
    mock_request = MagicMock(spec=Request)
    with pytest.raises(HTTPException) as exc:
        await proxy_memory(mock_request, path="../etc/passwd")
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid path"


@pytest.mark.asyncio
async def test_proxy_memory_ssrf_authority_override():
    """Test that authority override attempts (@) trigger 400 Bad Request."""
    mock_request = MagicMock(spec=Request)
    with pytest.raises(HTTPException) as exc:
        await proxy_memory(mock_request, path="@evil.com/data")
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid path"


@pytest.mark.asyncio
async def test_proxy_memory_ssrf_scheme_injection():
    """Test that scheme injection attempts (://) trigger 400 Bad Request."""
    mock_request = MagicMock(spec=Request)
    with pytest.raises(HTTPException) as exc:
        await proxy_memory(mock_request, path="/http://evil.com")
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid path"


@pytest.mark.asyncio
async def test_proxy_memory_ssrf_null_byte_injection():
    """Test that null byte injection attempts (\x00) trigger 400 Bad Request."""
    mock_request = MagicMock(spec=Request)
    with pytest.raises(HTTPException) as exc:
        await proxy_memory(mock_request, path="secret\x00.py")
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid path"


@pytest.mark.asyncio
async def test_proxy_memory_valid_request():
    """Test that a valid memory proxy request routes correctly to local LiteLLM destination."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "application/json"}
    mock_response.content = b'{"status": "ok"}'

    mock_http_client = AsyncMock()
    mock_http_client.request.return_value = mock_response

    with patch("router.main.get_http_client", return_value=mock_http_client):
        client = TestClient(app)
        response = client.get("/v1/memory/user/preferences")
        assert response.status_code == 200
        assert mock_http_client.request.called
        call_kwargs = mock_http_client.request.call_args.kwargs
        assert call_kwargs["url"] == "http://127.0.0.1:4000/v1/memory/user/preferences"
