import os
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from router.main import app, proxy_audio


@pytest.mark.asyncio
async def test_proxy_audio_ssrf_path_traversal():
    """Test that path traversal attempts (..) trigger 400 Bad Request."""
    mock_request = MagicMock(spec=Request)
    mock_request.headers = {"Authorization": "Bearer test-key"}
    with pytest.raises(HTTPException) as exc:
        await proxy_audio(mock_request, path="../etc/passwd")
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid path"


@pytest.mark.asyncio
async def test_proxy_audio_ssrf_authority_override():
    """Test that authority override attempts (@) trigger 400 Bad Request."""
    mock_request = MagicMock(spec=Request)
    mock_request.headers = {"Authorization": "Bearer test-key"}
    with pytest.raises(HTTPException) as exc:
        await proxy_audio(mock_request, path="@evil.com/data")
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid path"


@pytest.mark.asyncio
async def test_proxy_audio_ssrf_scheme_injection():
    """Test that scheme injection attempts (://) trigger 400 Bad Request."""
    mock_request = MagicMock(spec=Request)
    mock_request.headers = {"Authorization": "Bearer test-key"}
    with pytest.raises(HTTPException) as exc:
        await proxy_audio(mock_request, path="/http://evil.com")
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid path"


@pytest.mark.asyncio
async def test_proxy_audio_ssrf_null_byte_injection():
    """Test that null byte injection attempts (\x00) trigger 400 Bad Request."""
    mock_request = MagicMock(spec=Request)
    mock_request.headers = {"Authorization": "Bearer test-key"}
    with pytest.raises(HTTPException) as exc:
        await proxy_audio(mock_request, path="secret\x00.py")
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid path"


@pytest.mark.asyncio
async def test_proxy_audio_valid_request():
    """Test that a valid audio proxy request routes correctly to local LiteLLM destination."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "application/json"}
    mock_response.content = b'{"status": "ok"}'

    mock_http_client = AsyncMock()
    mock_http_client.request.return_value = mock_response

    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "sk-litellm-testkey"}),
        patch("router.main.get_http_client", return_value=mock_http_client),
    ):
        client = TestClient(app)
        response = client.get("/v1/audio/transcriptions", headers={"Authorization": "Bearer test-key"})
        assert response.status_code == 200
        mock_http_client.request.assert_awaited_once()
        call_kwargs = mock_http_client.request.call_args.kwargs
        assert call_kwargs["url"] == "http://127.0.0.1:4000/v1/audio/transcriptions"
        assert call_kwargs["headers"]["Authorization"] == "Bearer sk-litellm-testkey"


@pytest.mark.asyncio
async def test_proxy_audio_failure_502():
    """Test that if the proxy client request fails, we return a 502 HTTP exception."""
    mock_http_client = AsyncMock()
    mock_http_client.request.side_effect = Exception("Connection reset by peer")

    with patch("router.main.get_http_client", return_value=mock_http_client):
        client = TestClient(app)
        response = client.get("/v1/audio/transcriptions", headers={"Authorization": "Bearer test-key"})
        assert response.status_code == 502
        assert response.json()["detail"] == "Audio proxy failed"


def test_proxy_audio_unauthenticated_missing_header():
    """Test that missing Authorization header returns 401 Unauthorized."""
    client = TestClient(app)
    response = client.get("/v1/audio/transcriptions")
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing or invalid Authorization header"


def test_proxy_audio_unauthenticated_malformed_header():
    """Test that malformed Authorization header returns 401 Unauthorized."""
    client = TestClient(app)
    response = client.get("/v1/audio/transcriptions", headers={"Authorization": "Basic invalid"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing or invalid Authorization header"


def test_proxy_audio_unauthenticated_invalid_token():
    """Test that invalid Bearer token returns 401 Unauthorized."""
    client = TestClient(app)
    response = client.get("/v1/audio/transcriptions", headers={"Authorization": "Bearer completely-unknown-token"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid Authorization token"


@pytest.mark.asyncio
async def test_proxy_audio_unprefixed_audio_route_auth():
    """Test /audio prefix enforces auth and forwards with valid credentials."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "application/json"}
    mock_response.content = b'{"status": "ok"}'

    mock_http_client = AsyncMock()
    mock_http_client.request.return_value = mock_response

    client = TestClient(app)
    unauth_resp = client.post("/audio/speech")
    assert unauth_resp.status_code == 401

    with patch("router.main.get_http_client", return_value=mock_http_client):
        auth_resp = client.post(
            "/audio/speech",
            headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
            json={"input": "hello"},
        )
        assert auth_resp.status_code == 200


@pytest.mark.asyncio
async def test_proxy_audio_virtual_key_auth():
    """Test proxy_audio with a valid LiteLLM virtual key."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "application/json"}
    mock_response.content = b'{"text": "transcribed audio"}'

    mock_http_client = AsyncMock()
    mock_http_client.request.return_value = mock_response

    vkey_info = {"user_id": "usr-123", "key_alias": "test-vkey", "metadata": {}}
    with (
        patch("router.main.get_http_client", return_value=mock_http_client),
        patch("router.main._validate_litellm_virtual_key", AsyncMock(return_value=vkey_info)),
    ):
        client = TestClient(app)
        response = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": "Bearer sk-client-virtual-key"},
            json={},
        )
        assert response.status_code == 200
        assert response.json() == {"text": "transcribed audio"}
