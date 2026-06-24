from unittest.mock import patch
from router.agy_proxy import _wrap_response

def test_wrap_response_basic():
    text = "Hello, world!"
    model_name = "test-model"
    prompt = "Say hello"

    mock_time = 1620000000
    with patch("time.time", return_value=mock_time):
        result = _wrap_response(text, model_name, prompt)

    assert result["id"] == "chatcmpl-agy-proxy"
    assert result["object"] == "chat.completion"
    assert result["created"] == mock_time
    assert result["model"] == "test-model (via agy)"

    assert len(result["choices"]) == 1
    choice = result["choices"][0]
    assert choice["index"] == 0
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == text
    assert choice["finish_reason"] == "stop"

    prompt_tokens = len(prompt) // 4
    completion_tokens = len(text) // 4
    assert result["usage"]["prompt_tokens"] == prompt_tokens
    assert result["usage"]["completion_tokens"] == completion_tokens
    assert result["usage"]["total_tokens"] == prompt_tokens + completion_tokens

def test_wrap_response_empty_strings():
    text = ""
    model_name = ""
    prompt = ""

    mock_time = 1620000000
    with patch("time.time", return_value=mock_time):
        result = _wrap_response(text, model_name, prompt)

    assert result["model"] == " (via agy)"
    assert result["choices"][0]["message"]["content"] == ""
    assert result["usage"]["prompt_tokens"] == 0
    assert result["usage"]["completion_tokens"] == 0
    assert result["usage"]["total_tokens"] == 0

def test_wrap_response_long_strings():
    text = "a" * 1000
    model_name = "super-long-model-name-" * 10
    prompt = "b" * 500

    mock_time = 1620000000
    with patch("time.time", return_value=mock_time):
        result = _wrap_response(text, model_name, prompt)

    assert result["model"] == f"{model_name} (via agy)"
    assert result["choices"][0]["message"]["content"] == text
    assert result["usage"]["prompt_tokens"] == 125
    assert result["usage"]["completion_tokens"] == 250
    assert result["usage"]["total_tokens"] == 375
