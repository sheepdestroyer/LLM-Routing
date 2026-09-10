import os
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from router.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def isolate_master_key():
    with patch.dict(os.environ, {"LITELLM_MASTER_KEY": "sk-litellm-testkey"}):
        yield


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


def test_proxy_audio_multipart_upload(client):
    """Test that audio proxy forwards multipart/form-data preserving content-type and boundary."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "application/json"}
    mock_response.content = b'{"text": "transcribed audio"}'

    mock_http_client = AsyncMock()
    mock_http_client.request.return_value = mock_response

    with patch("router.main.get_http_client", return_value=mock_http_client):
        files = {"file": ("test.wav", b"RIFF....WAVEfmt", "audio/wav")}
        data = {"model": "whisper-1"}
        resp = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": "Bearer test-key"},
            files=files,
            data=data,
        )
        assert resp.status_code == 200
        assert resp.json() == {"text": "transcribed audio"}
        call_kwargs = mock_http_client.request.call_args.kwargs
        assert "multipart/form-data" in call_kwargs["headers"]["Content-Type"]


def test_proxy_memory_master_key_unconfigured_returns_500(client):
    """Test that proxy_memory returns 500 when LITELLM_MASTER_KEY is unconfigured."""
    with patch.dict(os.environ, {"LITELLM_MASTER_KEY": "PLACEHOLDER_KEY"}):
        resp = client.get("/v1/memory", headers={"Authorization": "Bearer test-key"})
        assert resp.status_code == 500
        assert "LiteLLM master key is missing" in resp.json()["detail"]


def test_proxy_audio_master_key_unconfigured_returns_500(client):
    """Test that proxy_audio returns 500 when LITELLM_MASTER_KEY is unconfigured."""
    with patch.dict(os.environ, {"LITELLM_MASTER_KEY": "sk-1234"}):
        resp = client.get("/v1/audio/transcriptions", headers={"Authorization": "Bearer test-key"})
        assert resp.status_code == 500
        assert "LiteLLM master key is missing" in resp.json()["detail"]


def test_proxy_memory_bodyless_no_content_type(client):
    """Test that bodyless GET request does not inject spurious application/json Content-Type."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.content = b'{"memories": []}'

    mock_http_client = AsyncMock()
    mock_http_client.request.return_value = mock_response

    with patch("router.main.get_http_client", return_value=mock_http_client):
        resp = client.get("/v1/memory", headers={"Authorization": "Bearer test-key"})
        assert resp.status_code == 200
        call_kwargs = mock_http_client.request.call_args.kwargs
        assert "Content-Type" not in call_kwargs["headers"]


def test_proxy_memory_body_without_content_type_defaults_to_json(client):
    """Test that proxy_memory defaults to application/json when body is provided without Content-Type."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.content = b'{"success": true}'

    mock_http_client = AsyncMock()
    mock_http_client.request.return_value = mock_response

    with patch("router.main.get_http_client", return_value=mock_http_client):
        resp = client.post(
            "/v1/memory",
            content=b'{"key": "value"}',
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 200
        call_kwargs = mock_http_client.request.call_args.kwargs
        assert call_kwargs["headers"]["Content-Type"] == "application/json"


def test_proxy_audio_body_without_content_type_defaults_to_json(client):
    """Test that proxy_audio defaults to application/json when body is provided without Content-Type."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.content = b'{"text": "transcription"}'

    mock_http_client = AsyncMock()
    mock_http_client.request.return_value = mock_response

    with patch("router.main.get_http_client", return_value=mock_http_client):
        resp = client.post(
            "/v1/audio/transcriptions",
            content=b'{"audio": "rawdata"}',
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 200
        call_kwargs = mock_http_client.request.call_args.kwargs
        assert call_kwargs["headers"]["Content-Type"] == "application/json"


def test_proxy_memory_authenticated_with_memory_api_key(client):
    """Test that requests authenticated with MEMORY_API_KEY succeed."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "application/json"}
    mock_response.content = b'{"status": "success"}'

    mock_http_client = AsyncMock()
    mock_http_client.request.return_value = mock_response

    with (
        patch.dict(os.environ, {"MEMORY_API_KEY": "custom-memory-key-123"}),
        patch("router.main.get_http_client", return_value=mock_http_client),
    ):
        resp = client.get("/v1/memory", headers={"Authorization": "Bearer custom-memory-key-123"})
        assert resp.status_code == 200


def test_proxy_memory_double_encoded_traversal_blocked(client):
    """Test that double-encoded path traversal on /v1/memory returns 400."""
    resp = client.get(
        "/v1/memory/%252e%252e%252f%252e%252e%252fkey/generate",
        headers={"Authorization": "Bearer test-key"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid path"


def test_proxy_audio_double_encoded_traversal_blocked(client):
    """Test that double-encoded path traversal on /v1/audio returns 400."""
    resp = client.get(
        "/v1/audio/%252e%252e%252f%252e%252e%252fkey/generate",
        headers={"Authorization": "Bearer test-key"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid path"


def test_proxy_memory_root_url_no_trailing_slash(client):
    """Test that root /v1/memory proxies without an unintended trailing slash."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.content = b'{"memories": []}'

    mock_http_client = AsyncMock()
    mock_http_client.request.return_value = mock_response

    with patch("router.main.get_http_client", return_value=mock_http_client):
        resp = client.get("/v1/memory", headers={"Authorization": "Bearer test-key"})
        assert resp.status_code == 200
        call_kwargs = mock_http_client.request.call_args.kwargs
        assert call_kwargs["url"] == "http://127.0.0.1:4000/v1/memory"


def test_sanitize_proxy_path_multi_unquote():
    """Test that deeply encoded paths decode properly through the loop."""
    from router.main import _sanitize_proxy_path

    # %252561 -> %2561 -> %61 -> a (exhausts the 3 iterations of decoding)
    res = _sanitize_proxy_path("%252561", "/v1/test")
    assert res == "/a"
