import os
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

# We patch environment variables before importing main to prevent actual connections
with patch.dict(os.environ, {
    "CONFIG_PATH": "router/config.yaml",
    "ROUTER_API_KEY": "test-key",
    "ROUTER_API_BASE": "http://localhost:8080/v1",
    "ROUTER_MODEL_NAME": "qwen-test",
}):
    from router.main import app, classify_request


@pytest.mark.asyncio
async def test_classify_request_truncation_default():
    """Verify that classify_request truncates the user prompt based on CLASSIFIER_INPUT_MAX_CHARS (default 300)."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "agent-medium-core"}}]
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    # Force bypass_cache=True to ensure classify_request always hits llama-server
    with patch("router.main.get_http_client", return_value=mock_client), \
         patch.dict(os.environ, {}, clear=False):
        # We verify behavior with default (no env var set -> defaults to 300)
        long_prompt = "a" * 500
        # Check that CLASSIFIER_INPUT_MAX_CHARS env var is not set, so it uses default 300
        if "CLASSIFIER_INPUT_MAX_CHARS" in os.environ:
            del os.environ["CLASSIFIER_INPUT_MAX_CHARS"]

        decision, _, _, _ = await classify_request(long_prompt, bypass_cache=True)

        assert decision == "agent-medium-core"
        # Verify the client post payload content contains qwen system prompt template + truncated prompt (300 'a's)
        called_args, called_kwargs = mock_client.post.call_args
        json_payload = called_kwargs["json"]
        sent_content = json_payload["messages"][0]["content"]
        assert sent_content.endswith("a" * 300)
        assert not sent_content.endswith("a" * 301)


@pytest.mark.asyncio
async def test_classify_request_truncation_custom_env():
    """Verify that classify_request respects CLASSIFIER_INPUT_MAX_CHARS environment variable."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "agent-complex-core"}}]
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    with patch("router.main.get_http_client", return_value=mock_client), \
         patch.dict(os.environ, {"CLASSIFIER_INPUT_MAX_CHARS": "10"}):
        long_prompt = "a" * 500
        decision, _, _, _ = await classify_request(long_prompt, bypass_cache=True)

        assert decision == "agent-complex-core"
        # Verify the client post payload content contains qwen system prompt template + truncated prompt (10 'a's)
        called_args, called_kwargs = mock_client.post.call_args
        json_payload = called_kwargs["json"]
        sent_content = json_payload["messages"][0]["content"]
        assert sent_content.endswith("a" * 10)
        assert not sent_content.endswith("a" * 11)


def test_llm_routing_agy_fallback_to_advanced_core():
    """Verify that if a direct request for 'llm-routing-agy' fails or is skipped, the target_model is rewritten to 'agent-advanced-core'."""
    client = TestClient(app)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "completed response"}}]
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    # Patch try_agy_proxy to raise exception to simulate failure / unavailability
    # Patch get_http_client to capture the outgoing request to the LiteLLM backend
    with patch("agy_proxy.try_agy_proxy", side_effect=Exception("Agy unavailable"), create=True), \
         patch("router.main.get_http_client", return_value=mock_client):
        payload = {
            "model": "llm-routing-agy",
            "messages": [{"role": "user", "content": "hello"}],
        }
        
        response = client.post("/v1/chat/completions", json=payload)
        
        assert response.status_code == 200
        assert response.json() == {"choices": [{"message": {"content": "completed response"}}]}
        
        # Verify the outgoing request had model set to agent-advanced-core
        mock_client.post.assert_called_once()
        called_args, called_kwargs = mock_client.post.call_args
        json_payload = called_kwargs["json"]
        assert json_payload["model"] == "agent-advanced-core"
