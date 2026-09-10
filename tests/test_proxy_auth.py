import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from router.main import app


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# /v1/memory Authentication Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE", "PATCH"])
def test_proxy_memory_unauthenticated_missing_header(client, method):
    """Test that requests to /v1/memory without Authorization header return 401."""
    resp = client.request(method, "/v1/memory/test/item")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Missing or invalid Authorization header"


@pytest.mark.parametrize(
    "auth_header",
    [
        "Basic dXNlcjpwYXNz",
        "Bearer",
        "Token some-random-token",
        "InvalidFormat",
    ],
)
def test_proxy_memory_unauthenticated_malformed_header(client, auth_header):
    """Test that requests to /v1/memory with malformed Authorization header return 401."""
    resp = client.get("/v1/memory/test/item", headers={"Authorization": auth_header})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Missing or invalid Authorization header"


def test_proxy_memory_unauthenticated_invalid_token(client):
    """Test that requests to /v1/memory with an invalid token return 401."""
    resp = client.get(
        "/v1/memory/test/item",
        headers={"Authorization": "Bearer non-existent-secret-key"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid Authorization token"


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE", "PATCH"])
def test_proxy_memory_authenticated_valid_token(client, method):
    """Test that authenticated requests with allowed token successfully proxy to LiteLLM."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "application/json"}
    mock_response.content = b'{"status": "success"}'

    mock_http_client = AsyncMock()
    mock_http_client.request.return_value = mock_response

    with patch("router.main.get_http_client", return_value=mock_http_client):
        resp = client.request(
            method,
            "/v1/memory/users/123",
            headers={"Authorization": "Bearer test-key"},
            json={"data": "test"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "success"}

        call_kwargs = mock_http_client.request.call_args.kwargs
        assert call_kwargs["url"] == "http://127.0.0.1:4000/v1/memory/users/123"
        assert call_kwargs["headers"]["Authorization"] == "Bearer sk-litellm-testkey"


def test_proxy_memory_authenticated_virtual_key(client):
    """Test that requests with valid LiteLLM virtual key successfully proxy."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "application/json"}
    mock_response.content = b'{"memories": []}'

    mock_http_client = AsyncMock()
    mock_http_client.request.return_value = mock_response

    vkey_info = {"user_id": "usr-mem", "key_alias": "mem-key", "metadata": {}}
    with (
        patch("router.main.get_http_client", return_value=mock_http_client),
        patch("router.main._validate_litellm_virtual_key", AsyncMock(return_value=vkey_info)),
    ):
        resp = client.get(
            "/v1/memory/query",
            headers={"Authorization": "Bearer sk-user-memory-virtual-key"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"memories": []}


# ---------------------------------------------------------------------------
# /v1/audio and /audio Authentication Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/v1/audio/transcriptions",
        "/v1/audio/speech",
        "/audio/transcriptions",
        "/audio/speech",
    ],
)
def test_proxy_audio_unauthenticated_missing_header(client, path):
    """Test that requests to audio proxy routes without Authorization header return 401."""
    resp = client.post(path)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Missing or invalid Authorization header"


@pytest.mark.parametrize(
    "auth_header",
    [
        "Basic YWRtaW46cGFzc3dvcmQ=",
        "Bearer",
        "Token audio-token",
        "Digest xyz",
    ],
)
def test_proxy_audio_unauthenticated_malformed_header(client, auth_header):
    """Test that audio proxy routes reject malformed Authorization headers with 401."""
    resp = client.post(
        "/v1/audio/transcriptions",
        headers={"Authorization": auth_header},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Missing or invalid Authorization header"


def test_proxy_audio_unauthenticated_invalid_token(client):
    """Test that audio proxy routes reject invalid Bearer tokens with 401."""
    resp = client.post(
        "/v1/audio/transcriptions",
        headers={"Authorization": "Bearer invalid-audio-key"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid Authorization token"


@pytest.mark.parametrize("path", ["/v1/audio/transcriptions", "/audio/transcriptions"])
def test_proxy_audio_authenticated_valid_token(client, path):
    """Test that audio proxy with valid token forwards to LiteLLM with master key."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "application/json"}
    mock_response.content = b'{"text": "test transcription"}'

    mock_http_client = AsyncMock()
    mock_http_client.request.return_value = mock_response

    with patch("router.main.get_http_client", return_value=mock_http_client):
        resp = client.post(
            path,
            headers={"Authorization": "Bearer test-key"},
            json={"model": "whisper-1"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"text": "test transcription"}

        call_kwargs = mock_http_client.request.call_args.kwargs
        assert call_kwargs["url"] == "http://127.0.0.1:4000/v1/audio/transcriptions"
        assert call_kwargs["headers"]["Authorization"] == "Bearer sk-litellm-testkey"


def test_proxy_audio_authenticated_virtual_key(client):
    """Test that audio proxy with valid LiteLLM virtual key succeeds."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "application/json"}
    mock_response.content = b'{"text": "transcribed"}'

    mock_http_client = AsyncMock()
    mock_http_client.request.return_value = mock_response

    vkey_info = {"user_id": "usr-audio", "key_alias": "audio-vkey", "metadata": {}}
    with (
        patch("router.main.get_http_client", return_value=mock_http_client),
        patch("router.main._validate_litellm_virtual_key", AsyncMock(return_value=vkey_info)),
    ):
        resp = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": "Bearer sk-audio-client-key"},
            json={"model": "whisper-1"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"text": "transcribed"}
