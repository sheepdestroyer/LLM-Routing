import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import Response, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from router.main import responses_api


@pytest.mark.anyio
async def test_responses_api_direct_model_non_streaming():
    """Test POST /v1/responses with direct model (gpt-4o-mini) non-streaming."""
    mock_request = MagicMock()
    mock_request.json = AsyncMock(return_value={
        "model": "gpt-4o-mini",
        "input": "Hello from Home Assistant config flow!"
    })
    mock_request.headers = {"content-type": "application/json", "Authorization": "Bearer test-token"}

    mock_lite_resp = MagicMock()
    mock_lite_resp.status_code = 200
    mock_lite_resp.content = json.dumps({
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
                "status": "completed"
            }
        ]
    }).encode("utf-8")
    mock_lite_resp.headers = {"content-type": "application/json"}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_lite_resp

    with patch("router.main.get_http_client", return_value=mock_client), \
         patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()):
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
    mock_request.json = AsyncMock(return_value={
        "model": "llm-routing-auto-free",
        "input": "Fix typo in variable name x = 1"
    })
    mock_request.headers = {"content-type": "application/json"}

    mock_lite_resp = MagicMock()
    mock_lite_resp.status_code = 200
    mock_lite_resp.content = json.dumps({
        "id": "resp_triage123",
        "object": "response",
        "model": "agent-simple-core",
        "status": "completed",
        "output": []
    }).encode("utf-8")
    mock_lite_resp.headers = {"content-type": "application/json"}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_lite_resp

    with patch("router.main.classify_request", new=AsyncMock(return_value=("agent-simple-core", 10.0, False, "simple"))), \
         patch("router.main.get_http_client", return_value=mock_client), \
         patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()):
        response = await responses_api(mock_request)
        assert isinstance(response, Response)
        assert response.status_code == 200
        # Verify body sent to LiteLLM was updated with target_model
        called_args, called_kwargs = mock_client.post.call_args
        assert called_kwargs["json"]["model"] == "agent-simple-core"


@pytest.mark.anyio
async def test_responses_api_with_tools():
    """Test POST /v1/responses with Home Assistant style tool definitions."""
    mock_request = MagicMock()
    mock_request.json = AsyncMock(return_value={
        "model": "local-qwen-3.6-hass",
        "input": "Turn on living room light",
        "tools": [
            {
                "type": "function",
                "name": "HassTurnOn",
                "parameters": {"type": "object", "properties": {"domain": {"type": "string"}}}
            },
            {"type": "code_interpreter"},
            {"type": "web_search"}
        ]
    })
    mock_request.headers = {"content-type": "application/json"}

    mock_lite_resp = MagicMock()
    mock_lite_resp.status_code = 200
    mock_lite_resp.content = json.dumps({
        "id": "resp_tool123",
        "object": "response",
        "model": "local-qwen-3.6-hass",
        "status": "completed",
        "output": [
            {
                "id": "fc_123",
                "type": "function_call",
                "name": "HassTurnOn",
                "arguments": '{"domain": "light"}',
                "status": "completed"
            }
        ]
    }).encode("utf-8")
    mock_lite_resp.headers = {"content-type": "application/json"}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_lite_resp

    with patch("router.main.get_http_client", return_value=mock_client), \
         patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()):
        response = await responses_api(mock_request)
        assert isinstance(response, Response)
        assert response.status_code == 200
        data = json.loads(response.body)
        assert data["output"][0]["type"] == "function_call"
        assert data["output"][0]["name"] == "HassTurnOn"


@pytest.mark.anyio
async def test_responses_api_invalid_json():
    """Test POST /v1/responses with malformed JSON body."""
    mock_request = MagicMock()
    mock_request.json = AsyncMock(side_effect=ValueError("Invalid JSON"))

    with pytest.raises(HTTPException) as exc_info:
        await responses_api(mock_request)
    assert exc_info.value.status_code == 400
    assert "Invalid JSON payload" in exc_info.value.detail


@pytest.mark.anyio
async def test_responses_api_streaming():
    """Test POST /v1/responses with stream=True."""
    mock_request = MagicMock()
    mock_request.json = AsyncMock(return_value={
        "model": "gpt-4o-mini",
        "input": "Stream test",
        "stream": True
    })
    mock_request.headers = {"content-type": "application/json"}

    async def mock_aiter_bytes():
        yield b'data: {"type":"response.created"}\n\n'
        yield b'data: [DONE]\n\n'

    mock_stream_ctx = MagicMock()
    mock_stream_ctx.__aenter__ = AsyncMock(return_value=MagicMock(aiter_bytes=mock_aiter_bytes))
    mock_stream_ctx.__aexit__ = AsyncMock()

    mock_client = AsyncMock()
    mock_client.stream.return_value = mock_stream_ctx

    with patch("router.main.get_http_client", return_value=mock_client), \
         patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()):
        response = await responses_api(mock_request)
        assert isinstance(response, StreamingResponse)
        assert response.media_type == "text/event-stream"
