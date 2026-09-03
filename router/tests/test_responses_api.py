import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import Response, HTTPException
from fastapi.responses import StreamingResponse

from router.main import responses_api, _validate_litellm_master_key


@pytest.mark.anyio
async def test_responses_api_direct_model_non_streaming():
    """Test POST /v1/responses with direct model (gpt-4o-mini) non-streaming."""
    mock_request = MagicMock()
    mock_request.json = AsyncMock(
        return_value={"model": "gpt-4o-mini", "input": "Hello from Home Assistant config flow!"}
    )
    mock_request.headers = {"content-type": "application/json", "Authorization": "Bearer test-token"}

    mock_lite_resp = MagicMock()
    mock_lite_resp.status_code = 200
    mock_lite_resp.content = json.dumps(
        {
            "id": "resp_test123",
            "object": "response",
            "model": "gpt-4o-mini",
            "status": "completed",
            "output": [
                {
                    "id": "msg_123",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello there!"}],
                    "status": "completed",
                }
            ],
        }
    ).encode("utf-8")
    mock_lite_resp.headers = {"content-type": "application/json"}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_lite_resp

    with (
        patch("router.main.get_http_client", return_value=mock_client),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-master-key"}),
    ):
        response = await responses_api(mock_request)
        assert isinstance(response, Response)
        assert response.status_code == 200
        data = json.loads(response.body)
        assert data["id"] == "resp_test123"
        assert data["model"] == "gpt-4o-mini"
        assert data["output"][0]["content"][0]["text"] == "Hello there!"


@pytest.mark.anyio
async def test_responses_api_auto_model_triage():
    """Test POST /v1/responses with auto model (llm-routing-auto-free) triggers classifier."""
    mock_request = MagicMock()
    mock_request.json = AsyncMock(
        return_value={"model": "llm-routing-auto-free", "input": "Fix typo in variable name x = 1"}
    )
    mock_request.headers = {"content-type": "application/json", "Authorization": "Bearer test-token"}

    mock_lite_resp = MagicMock()
    mock_lite_resp.status_code = 200
    mock_lite_resp.content = json.dumps(
        {
            "id": "resp_triage123",
            "object": "response",
            "model": "agent-simple-core",
            "status": "completed",
            "output": [],
        }
    ).encode("utf-8")
    mock_lite_resp.headers = {"content-type": "application/json"}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_lite_resp

    with (
        patch("router.main.classify_request", new=AsyncMock(return_value=("agent-simple-core", 10.0, False, "simple"))),
        patch("router.main.get_http_client", return_value=mock_client),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-master-key"}),
    ):
        response = await responses_api(mock_request)
        assert isinstance(response, Response)
        assert response.status_code == 200
        # Verify body sent to LiteLLM was updated with target_model
        called_args, called_kwargs = mock_client.post.call_args
        assert called_kwargs["json"]["model"] == "agent-simple-core"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "model_alias",
    [
        "locallama-qwen-hass",
        "gpt-4o-mini",
        "gpt-4o",
    ],
)
async def test_responses_api_with_tools(model_alias):
    """Test POST /v1/responses with Home Assistant style tool definitions for all HA model aliases."""
    mock_request = MagicMock()
    mock_request.json = AsyncMock(
        return_value={
            "model": model_alias,
            "input": "Turn on living room light",
            "tools": [
                {
                    "type": "function",
                    "name": "HassTurnOn",
                    "parameters": {"type": "object", "properties": {"domain": {"type": "string"}}},
                },
                {"type": "code_interpreter"},
                {"type": "web_search"},
            ],
        }
    )
    mock_request.headers = {"content-type": "application/json", "Authorization": "Bearer test-token"}

    mock_lite_resp = MagicMock()
    mock_lite_resp.status_code = 200
    mock_lite_resp.content = json.dumps(
        {
            "id": "resp_tool123",
            "object": "response",
            "model": model_alias,
            "status": "completed",
            "output": [
                {
                    "id": "fc_123",
                    "type": "function_call",
                    "name": "HassTurnOn",
                    "arguments": '{"domain": "light"}',
                    "status": "completed",
                }
            ],
        }
    ).encode("utf-8")
    mock_lite_resp.headers = {"content-type": "application/json"}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_lite_resp

    with (
        patch("router.main.get_http_client", return_value=mock_client),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-master-key"}),
    ):
        response = await responses_api(mock_request)
        assert isinstance(response, Response)
        assert response.status_code == 200
        data = json.loads(response.body)
        assert data["output"][0]["type"] == "function_call"
        assert data["output"][0]["name"] == "HassTurnOn"
        assert data["model"] == model_alias


@pytest.mark.anyio
@pytest.mark.parametrize(
    "model_alias",
    [
        "locallama-qwen-hass",
        "gpt-4o-mini",
        "gpt-4o",
    ],
)
async def test_responses_api_streaming_tool_calls(model_alias):
    """Test POST /v1/responses streaming SSE tool call event conversion for HA models."""
    mock_request = MagicMock()
    mock_request.json = AsyncMock(
        return_value={
            "model": model_alias,
            "input": "Turn on kitchen light",
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": "HassTurnOn",
                    "parameters": {"type": "object", "properties": {"domain": {"type": "string"}}},
                }
            ],
        }
    )
    mock_request.headers = {"content-type": "application/json", "Authorization": "Bearer test-token"}

    sse_data = json.dumps(
        {
            "type": "response.output_item.done",
            "item": {
                "id": "fc_stream999",
                "type": "function_call",
                "name": "HassTurnOn",
                "arguments": '{"domain": "light", "entity_id": "light.kitchen"}',
            },
        }
    )

    async def mock_aiter_bytes():
        yield f"data: {sse_data}\n\n".encode()
        yield b"data: [DONE]\n\n"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.aiter_bytes = mock_aiter_bytes
    mock_resp.aclose = AsyncMock(return_value=None)

    mock_client = MagicMock()
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_resp)

    with (
        patch("router.main.get_http_client", return_value=mock_client),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-master-key"}),
    ):
        response = await responses_api(mock_request)
        assert isinstance(response, StreamingResponse)

        # Collect streamed bytes
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
        full_stream = "".join(chunks)

        # Verify delta and done tool call events were generated for Home Assistant
        assert "response.function_call_arguments.delta" in full_stream
        assert "response.function_call_arguments.done" in full_stream
        assert "fc_stream999" in full_stream
        assert "HassTurnOn" in full_stream


@pytest.mark.anyio
async def test_responses_api_invalid_json():
    """Test POST /v1/responses with malformed JSON body."""
    mock_request = MagicMock()
    mock_request.json = AsyncMock(side_effect=ValueError("Invalid JSON"))
    mock_request.headers = {"content-type": "application/json", "Authorization": "Bearer test-token"}

    with pytest.raises(HTTPException) as exc_info:
        await responses_api(mock_request)
    assert exc_info.value.status_code == 400
    assert "Invalid JSON payload" in exc_info.value.detail


@pytest.mark.anyio
async def test_responses_api_streaming():
    """Test POST /v1/responses with stream=True."""
    mock_request = MagicMock()
    mock_request.json = AsyncMock(return_value={"model": "gpt-4o-mini", "input": "Stream test", "stream": True})
    mock_request.headers = {"content-type": "application/json", "Authorization": "Bearer test-token"}

    async def mock_aiter_bytes():
        yield b'data: {"type":"response.created"}\n\n'
        yield b"data: [DONE]\n\n"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.aiter_bytes = mock_aiter_bytes
    mock_resp.aclose = AsyncMock()

    mock_client = AsyncMock()
    mock_client.build_request.return_value = MagicMock()
    mock_client.send.return_value = mock_resp

    with (
        patch("router.main.get_http_client", return_value=mock_client),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-master-key"}),
    ):
        response = await responses_api(mock_request)
        assert isinstance(response, StreamingResponse)
        assert response.media_type == "text/event-stream"
        chunks = [chunk async for chunk in response.body_iterator]
        assert len(chunks) == 4


@pytest.mark.anyio
async def test_responses_api_streaming_error():
    """Test POST /v1/responses streaming when upstream returns non-200 error status."""
    mock_request = MagicMock()
    mock_request.json = AsyncMock(return_value={"model": "gpt-4o-mini", "input": "Stream error test", "stream": True})
    mock_request.headers = {"content-type": "application/json", "Authorization": "Bearer test-token"}

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.aread = AsyncMock(return_value=b'{"error": "Internal Server Error"}')
    mock_resp.aclose = AsyncMock(return_value=None)

    mock_client = MagicMock()
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_resp)

    with (
        patch("router.main.get_http_client", return_value=mock_client),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-master-key"}),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await responses_api(mock_request)
        assert exc_info.value.status_code == 500
        assert "Responses proxy failed" in exc_info.value.detail


@pytest.mark.anyio
async def test_responses_api_input_text_extraction():
    """Test last user message extraction with input_text content parts and reverse traversal."""
    mock_request = MagicMock()
    mock_request.json = AsyncMock(
        return_value={
            "model": "llm-routing-auto-free",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "Old turn"}]},
                {"role": "assistant", "content": "Previous assistant answer"},
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Part 1"}, {"type": "text", "text": "Part 2"}],
                },
            ],
        }
    )
    mock_request.headers = {"content-type": "application/json", "Authorization": "Bearer test-token"}

    mock_lite_resp = MagicMock()
    mock_lite_resp.status_code = 200
    mock_lite_resp.content = b"{}"
    mock_lite_resp.headers = {"content-type": "application/json"}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_lite_resp

    classify_mock = AsyncMock(return_value=("agent-simple-core", 10.0, False, "simple"))

    with (
        patch("router.main.classify_request", classify_mock),
        patch("router.main.get_http_client", return_value=mock_client),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-master-key"}),
    ):
        response = await responses_api(mock_request)
        assert isinstance(response, Response)
        # Verify classify_request was called with extracted last user turn: "Part 1 Part 2"
        classify_mock.assert_called_once()
        last_msg = classify_mock.call_args[0][0]
        assert last_msg == "Part 1 Part 2"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "missing_or_invalid_auth",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Basic invalidtoken"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer invalid_secret_token_123"},
    ],
)
async def test_responses_api_enforces_client_auth(missing_or_invalid_auth):
    """Verify POST /v1/responses rejects requests missing valid Bearer authorization with 401."""
    mock_request = MagicMock()
    mock_request.headers = missing_or_invalid_auth

    with patch.dict(os.environ, {"ROUTER_API_KEY": "valid-key"}):
        with pytest.raises(HTTPException) as exc_info:
            await responses_api(mock_request)
        assert exc_info.value.status_code == 401


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_master_key",
    [
        "",
        "DYNAMIC_LITELLM_MASTER_KEY_PLACEHOLDER",
        "LITELLM_MASTER_KEY_PLACEHOLDER",
        "os.environ/LITELLM_MASTER_KEY",
        "YOUR_LITELLM_MASTER_KEY",
    ],
)
async def test_responses_api_invalid_master_key_fail_fast(invalid_master_key):
    """Verify POST /v1/responses fails fast with 500 if server LITELLM_MASTER_KEY is unconfigured or placeholder."""
    mock_request = MagicMock()
    mock_request.json = AsyncMock(return_value={"model": "gpt-4o-mini", "input": "hi"})
    mock_request.headers = {"content-type": "application/json", "Authorization": "Bearer gateway-pass"}

    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": invalid_master_key}),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await responses_api(mock_request)
        assert exc_info.value.status_code == 500
        assert "LiteLLM master key" in exc_info.value.detail


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_master_key",
    [
        "",
        "DYNAMIC_LITELLM_MASTER_KEY_PLACEHOLDER",
        "LITELLM_MASTER_KEY_PLACEHOLDER",
    ],
)
def test_validate_litellm_master_key_raises_http_500(invalid_master_key):
    """Verify _validate_litellm_master_key helper raises 500 on invalid keys."""
    with patch.dict(os.environ, {"LITELLM_MASTER_KEY": invalid_master_key}):
        with pytest.raises(HTTPException) as exc_info:
            _validate_litellm_master_key()
        assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_authenticate_client_request_case_insensitive():
    """Verify _authenticate_client_request accepts case-insensitive 'bearer' prefix."""
    from router.main import _authenticate_client_request

    mock_request = MagicMock()
    mock_request.headers = {"Authorization": "bearer my-valid-key"}
    with patch.dict(os.environ, {"ROUTER_API_KEY": "my-valid-key"}):
        token = await _authenticate_client_request(mock_request)
        assert token == "my-valid-key"


@pytest.mark.asyncio
async def test_authenticate_client_request_fail_closed_when_empty_keys():
    """Verify _authenticate_client_request raises 401 when no valid server keys are configured."""
    from router.main import _authenticate_client_request
    import sys

    mock_request = MagicMock()
    mock_request.headers = {"Authorization": "Bearer any-token"}
    # Temporarily remove pytest from sys.modules during this test or mock empty valid_keys
    with (
        patch.dict(os.environ, {"ROUTER_API_KEY": "", "LITELLM_MASTER_KEY": "", "GATEWAY_KEY": ""}, clear=True),
        patch.dict(sys.modules, {"pytest": None}),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _authenticate_client_request(mock_request)
        assert exc_info.value.status_code == 401
        assert "Invalid Authorization token" in exc_info.value.detail


@pytest.mark.asyncio
async def test_authenticate_client_request_litellm_virtual_key_valid():
    """Verify _authenticate_client_request validates and caches LiteLLM virtual keys."""
    from router.main import _authenticate_client_request, _VIRTUAL_KEY_CACHE
    import sys

    _VIRTUAL_KEY_CACHE.clear()
    mock_request = MagicMock()
    mock_request.headers = {"Authorization": "Bearer sk-valid-vkey-123"}
    mock_request.state = MagicMock()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "info": {
            "key_alias": "hermes-agent-boy",
            "user_id": "boy-hermes",
            "models": ["all-team-models"],
            "metadata": {"app": "hermes"},
        }
    }

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp

    with (
        patch.dict(os.environ, {"ROUTER_API_KEY": "router-secret", "LITELLM_MASTER_KEY": "sk-master"}, clear=True),
        patch.dict(sys.modules, {"pytest": None}),
        patch("router.main.get_http_client", return_value=mock_client),
    ):
        token = await _authenticate_client_request(mock_request)
        assert token == "sk-valid-vkey-123"
        assert mock_request.state.auth_user_id == "boy-hermes"
        assert mock_request.state.auth_key_alias == "hermes-agent-boy"
        assert "sk-valid-vkey-123" in _VIRTUAL_KEY_CACHE

        # Second call should hit the cache without calling LiteLLM again
        mock_client.get.reset_mock()
        token2 = await _authenticate_client_request(mock_request)
        assert token2 == "sk-valid-vkey-123"
        mock_client.get.assert_not_called()


@pytest.mark.asyncio
async def test_authenticate_client_request_litellm_virtual_key_invalid():
    """Verify _authenticate_client_request rejects invalid LiteLLM virtual keys with 401."""
    from router.main import _authenticate_client_request, _VIRTUAL_KEY_CACHE
    import sys

    _VIRTUAL_KEY_CACHE.clear()
    mock_request = MagicMock()
    mock_request.headers = {"Authorization": "Bearer sk-invalid-vkey-456"}
    mock_request.state = MagicMock()

    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.json.return_value = {"error": "Key not found"}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp

    with (
        patch.dict(os.environ, {"ROUTER_API_KEY": "router-secret", "LITELLM_MASTER_KEY": "sk-master"}, clear=True),
        patch.dict(sys.modules, {"pytest": None}),
        patch("router.main.get_http_client", return_value=mock_client),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _authenticate_client_request(mock_request)
        assert exc_info.value.status_code == 401
        assert "Invalid Authorization token" in exc_info.value.detail
