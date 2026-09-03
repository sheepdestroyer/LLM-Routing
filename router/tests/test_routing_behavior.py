import os
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

os.environ.setdefault("CONFIG_PATH", "router/config.yaml")
os.environ.setdefault("ROUTER_API_KEY", "test-key")
os.environ.setdefault("ROUTER_API_BASE", "http://localhost:8080/v1")
os.environ.setdefault("ROUTER_MODEL_NAME", "qwen-test")
os.environ.setdefault("LITELLM_MASTER_KEY", "test-master-key")

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
    with patch("router.main.get_classifier_client", return_value=mock_client), \
         patch.dict(os.environ, {}, clear=False):
        # We verify behavior with default (no env var set -> defaults to 300)
        long_prompt = "a" * 500
        # Check that CLASSIFIER_INPUT_MAX_CHARS env var is not set, so it uses default 300
        if "CLASSIFIER_INPUT_MAX_CHARS" in os.environ:
            del os.environ["CLASSIFIER_INPUT_MAX_CHARS"]

        decision, _, _, _ = await classify_request(long_prompt, bypass_cache=True)

        assert decision == "agent-medium-core"
        # Verify the client post payload content contains qwen system prompt template + truncated prompt (300 'a's)
        _called_args, called_kwargs = mock_client.post.call_args
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

    with patch("router.main.get_classifier_client", return_value=mock_client), \
         patch.dict(os.environ, {"CLASSIFIER_INPUT_MAX_CHARS": "10"}):
        long_prompt = "a" * 500
        decision, _, _, _ = await classify_request(long_prompt, bypass_cache=True)

        assert decision == "agent-complex-core"
        # Verify the client post payload content contains qwen system prompt template + truncated prompt (10 'a's)
        _called_args, called_kwargs = mock_client.post.call_args
        json_payload = called_kwargs["json"]
        sent_content = json_payload["messages"][0]["content"]
        assert sent_content.endswith("a" * 10)
        assert not sent_content.endswith("a" * 11)


def test_llm_routing_agy_proxied_to_litellm():
    """Verify that a request for 'llm-routing-agy' is proxied to LiteLLM with model='llm-routing-agy'."""
    client = TestClient(app)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "completed response"}}]
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    with patch("router.main.get_http_client", return_value=mock_client), \
         patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-key"}):
        payload = {
            "model": "llm-routing-agy",
            "messages": [{"role": "user", "content": "hello"}],
        }
        
        response = client.post("/v1/chat/completions", json=payload, headers={"Authorization": "Bearer test-key"})
        
        assert response.status_code == 200
        assert response.json() == {"choices": [{"message": {"content": "completed response"}}]}
        assert "x-session-id" in response.headers
        
        # Verify the outgoing request had model set to llm-routing-agy
        mock_client.post.assert_called_once()
        _called_args, called_kwargs = mock_client.post.call_args
        json_payload = called_kwargs["json"]
        assert json_payload["model"] == "llm-routing-agy"
        assert "session_id" in json_payload["metadata"]
        assert called_kwargs["headers"]["x-session-id"] == response.headers["x-session-id"]

def test_session_id_synthesis_deterministic():
    """Verify Option C1: session ID is deterministically synthesized from initial messages."""
    client = TestClient(app)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "turn response"}}]
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    with patch("router.main.get_http_client", return_value=mock_client), \
         patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-key"}):
        payload1 = {
            "model": "agent-simple-core",
            "messages": [
                {"role": "system", "content": "You are a coding assistant."},
                {"role": "user", "content": "Write a python script"},
            ],
        }
        resp1 = client.post("/v1/chat/completions", json=payload1, headers={"Authorization": "Bearer test-key"})
        sess1 = resp1.headers["x-session-id"]

        # Turn 2: same conversation root + continuation
        payload2 = {
            "model": "agent-simple-core",
            "messages": [
                {"role": "system", "content": "You are a coding assistant."},
                {"role": "user", "content": "Write a python script"},
                {"role": "assistant", "content": "Here is the code"},
                {"role": "user", "content": "Now add tests"},
            ],
        }
        resp2 = client.post("/v1/chat/completions", json=payload2, headers={"Authorization": "Bearer test-key"})
        sess2 = resp2.headers["x-session-id"]

        # Must have the exact same synthesized session ID across multi-turn conversation
        assert sess1 == sess2
        assert sess1.startswith("sess-")

def test_session_id_explicit_header_preserved():
    """Verify explicit x-session-id header is preserved and forwarded."""
    client = TestClient(app)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "turn response"}}]
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    with patch("router.main.get_http_client", return_value=mock_client), \
         patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-key"}):
        payload = {
            "model": "agent-simple-core",
            "messages": [{"role": "user", "content": "ping"}],
        }
        resp = client.post(
            "/v1/chat/completions",
            json=payload,
            headers={"Authorization": "Bearer test-key", "X-Session-ID": "hermes-session-custom-99"},
        )
        assert resp.headers["x-session-id"] == "hermes-session-custom-99"

        _called_args, called_kwargs = mock_client.post.call_args
        assert called_kwargs["headers"]["x-session-id"] == "hermes-session-custom-99"
        assert called_kwargs["json"]["metadata"]["session_id"] == "hermes-session-custom-99"

@pytest.mark.asyncio
async def test_classify_request_exception():
    mock_client = AsyncMock()
    mock_client.post.side_effect = Exception("Simulated connection error")

    with patch("router.main.get_http_client", return_value=mock_client), \
         patch.dict(os.environ, {}, clear=False):
        decision, latency, was_cache_hit, raw_result = await classify_request("test prompt", bypass_cache=True)
        assert decision == "agent-advanced-core"
        assert raw_result == "advanced (exception)"
        assert was_cache_hit is False
        assert latency >= 0.0

@pytest.mark.asyncio
async def test_classify_request_value_error_max_chars():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "agent-medium-core"}}]
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    with patch("router.main.get_classifier_client", return_value=mock_client), \
         patch.dict(os.environ, {"CLASSIFIER_INPUT_MAX_CHARS": "invalid_int"}):
        decision, latency, was_cache_hit, raw_result = await classify_request("test prompt", bypass_cache=True)
        assert decision == "agent-medium-core"

@pytest.mark.asyncio
async def test_classify_request_langfuse_exceptions():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "agent-medium-core"}}]
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    mock_lf = MagicMock()
    mock_lf.start_observation.side_effect = Exception("Langfuse start error")

    with patch("router.main.get_classifier_client", return_value=mock_client), \
         patch("router.main.get_langfuse", return_value=mock_lf):
        decision, latency, was_cache_hit, raw_result = await classify_request("test prompt", bypass_cache=True, langfuse_trace_id="test_trace")
        assert decision == "agent-medium-core"

    mock_span = MagicMock()
    mock_span.end.side_effect = Exception("Langfuse end error")
    mock_lf.start_observation.side_effect = None
    mock_lf.start_observation.return_value = mock_span

    with patch("router.main.get_classifier_client", return_value=mock_client), \
         patch("router.main.get_langfuse", return_value=mock_lf):
        decision, latency, was_cache_hit, raw_result = await classify_request("test prompt", bypass_cache=True, langfuse_trace_id="test_trace")
        assert decision == "agent-medium-core"
        mock_span.end.assert_called_once()

    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"
    mock_span.end.reset_mock()
    with patch("router.main.get_classifier_client", return_value=mock_client), \
         patch("router.main.get_langfuse", return_value=mock_lf):
        decision, latency, was_cache_hit, raw_result = await classify_request("test prompt", bypass_cache=True, langfuse_trace_id="test_trace")
        assert decision == "agent-advanced-core"
        assert raw_result == "advanced (fallback)"
        mock_span.end.assert_called_once()


@pytest.mark.parametrize("model_name", [
    "openrouter-gpt-5.6-luna",
    "openrouter-gpt-5.6-luna-max",
    "gpt-5.6-luna",
])
def test_direct_routing_openrouter_gpt_5_6_luna_variants(model_name):
    """Verify that direct requests for openrouter-gpt-5.6-luna variants bypass the classifier and proxy to LiteLLM."""
    client = TestClient(app)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "luna completion"}}]
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    with patch("router.main.get_http_client", return_value=mock_client), \
         patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-key"}):
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": "hello"}],
        }
        response = client.post("/v1/chat/completions", json=payload, headers={"Authorization": "Bearer test-key"})
        assert response.status_code == 200
        assert response.json() == {"choices": [{"message": {"content": "luna completion"}}]}

        mock_client.post.assert_called_once()
        _called_args, called_kwargs = mock_client.post.call_args
        assert called_kwargs["json"]["model"] == model_name


@pytest.mark.parametrize("model_name", [
    "ollama/GPT-5.6 Luna (max)",
    "ollama-gpt-5.6-luna-max",
    "ollama/gpt-5.6-luna",
    "ollama-gpt-5.6-luna",
])
def test_direct_routing_ollama_gpt_5_6_luna_variants(model_name):
    """Verify that direct requests for ollama GPT-5.6 Luna variants bypass the classifier and proxy to LiteLLM."""
    client = TestClient(app)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "ollama luna completion"}}]
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    with patch("router.main.get_http_client", return_value=mock_client), \
         patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-key"}):
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": "hello"}],
        }
        response = client.post("/v1/chat/completions", json=payload, headers={"Authorization": "Bearer test-key"})
        assert response.status_code == 200
        assert response.json() == {"choices": [{"message": {"content": "ollama luna completion"}}]}

        mock_client.post.assert_called_once()
        _called_args, called_kwargs = mock_client.post.call_args
        assert called_kwargs["json"]["model"] == model_name


def test_direct_routing_arbitrary_custom_db_model():
    """Verify that any custom in-DB model (not listed in static YAML) is directly forwarded to LiteLLM."""
    client = TestClient(app)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "custom db model completion"}}]
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    custom_model = "custom-arbitrary-db-model-v1"
    with patch("router.main.get_http_client", return_value=mock_client), \
         patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-key"}):
        payload = {
            "model": custom_model,
            "messages": [{"role": "user", "content": "test payload"}],
        }
        response = client.post("/v1/chat/completions", json=payload, headers={"Authorization": "Bearer test-key"})
        assert response.status_code == 200
        assert response.json() == {"choices": [{"message": {"content": "custom db model completion"}}]}

        mock_client.post.assert_called_once()
        _called_args, called_kwargs = mock_client.post.call_args
        assert called_kwargs["json"]["model"] == custom_model


def test_chat_completions_missing_auth():
    """Verify that /v1/chat/completions rejects unauthenticated requests with 401."""
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "test", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert response.status_code == 401
    assert "Missing or invalid Authorization header" in response.text


def test_chat_completions_invalid_auth():
    """Verify that /v1/chat/completions rejects invalid tokens with 401."""
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer invalid-token"}
    )
    assert response.status_code == 401
    assert "Invalid Authorization token" in response.text


def test_chat_completions_case_insensitive_auth():
    """Verify that /v1/chat/completions accepts case-insensitive 'bearer <token>'."""
    client = TestClient(app)
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    with patch("router.main.get_http_client", return_value=mock_client), \
         patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-key"}):
        response = client.post(
            "/v1/chat/completions",
            json={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
            headers={"authorization": "bearer test-key"}
        )
        assert response.status_code == 200



