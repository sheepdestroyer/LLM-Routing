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
@pytest.mark.parametrize("model_alias", [
    "local-qwen-3.6-hass",
    "gpt-4o-mini",
    "gpt-4o",
])
async def test_responses_api_with_tools(model_alias):
    """Test POST /v1/responses with Home Assistant style tool definitions for all HA model aliases."""
    mock_request = MagicMock()
    mock_request.json = AsyncMock(return_value={
        "model": model_alias,
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
        "model": model_alias,
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
        assert data["model"] == model_alias


@pytest.mark.anyio
@pytest.mark.parametrize("model_alias", [
    "local-qwen-3.6-hass",
    "gpt-4o-mini",
    "gpt-4o",
])
async def test_responses_api_streaming_tool_calls(model_alias):
    """Test POST /v1/responses streaming SSE tool call event conversion for HA models."""
    mock_request = MagicMock()
    mock_request.json = AsyncMock(return_value={
        "model": model_alias,
        "input": "Turn on kitchen light",
        "stream": True,
        "tools": [
            {
                "type": "function",
                "name": "HassTurnOn",
                "parameters": {"type": "object", "properties": {"domain": {"type": "string"}}}
            }
        ]
    })
    mock_request.headers = {"content-type": "application/json"}

    sse_data = json.dumps({
        "type": "response.output_item.done",
        "item": {
            "id": "fc_stream999",
            "type": "function_call",
            "name": "HassTurnOn",
            "arguments": '{"domain": "light", "entity_id": "light.kitchen"}'
        }
    })

    async def mock_aiter_bytes():
        yield f"data: {sse_data}\n\n".encode("utf-8")
        yield b'data: [DONE]\n\n'

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.aiter_bytes = mock_aiter_bytes
    mock_resp.aclose = AsyncMock(return_value=None)

    mock_client = MagicMock()
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_resp)

    with patch("router.main.get_http_client", return_value=mock_client), \
         patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()):
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

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.aiter_bytes = mock_aiter_bytes
    mock_resp.aclose = AsyncMock()

    mock_client = AsyncMock()
    mock_client.build_request.return_value = MagicMock()
    mock_client.send.return_value = mock_resp

    with patch("router.main.get_http_client", return_value=mock_client), \
         patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()):
        response = await responses_api(mock_request)
        assert isinstance(response, StreamingResponse)
        assert response.media_type == "text/event-stream"


@pytest.mark.anyio
async def test_responses_api_streaming_error():
    """Test POST /v1/responses streaming when upstream returns non-200 error status."""
    mock_request = MagicMock()
    mock_request.json = AsyncMock(return_value={
        "model": "gpt-4o-mini",
        "input": "Stream error test",
        "stream": True
    })
    mock_request.headers = {"content-type": "application/json"}

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.aread = AsyncMock(return_value=b'{"error": "Internal Server Error"}')
    mock_resp.aclose = AsyncMock(return_value=None)

    mock_client = MagicMock()
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_resp)

    with patch("router.main.get_http_client", return_value=mock_client), \
         patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()):
        with pytest.raises(HTTPException) as exc_info:
            await responses_api(mock_request)
        assert exc_info.value.status_code == 500
        assert "Responses proxy failed" in exc_info.value.detail


@pytest.mark.anyio
async def test_responses_api_input_text_extraction():
    """Test last user message extraction with input_text content parts and reverse traversal."""
    mock_request = MagicMock()
    mock_request.json = AsyncMock(return_value={
        "model": "llm-routing-auto-free",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Old turn"}
                ]
            },
            {
                "role": "assistant",
                "content": "Previous assistant answer"
            },
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Part 1"},
                    {"type": "text", "text": "Part 2"}
                ]
            }
        ]
    })
    mock_request.headers = {"content-type": "application/json"}

    mock_lite_resp = MagicMock()
    mock_lite_resp.status_code = 200
    mock_lite_resp.content = b'{}'
    mock_lite_resp.headers = {"content-type": "application/json"}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_lite_resp

    classify_mock = AsyncMock(return_value=("agent-simple-core", 10.0, False, "simple"))

    with patch("router.main.classify_request", classify_mock), \
         patch("router.main.get_http_client", return_value=mock_client), \
         patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()):
        response = await responses_api(mock_request)
        assert isinstance(response, Response)
        # Verify classify_request was called with extracted last user turn: "Part 1 Part 2"
        classify_mock.assert_called_once()
        last_msg = classify_mock.call_args[0][0]
        assert last_msg == "Part 1 Part 2"

