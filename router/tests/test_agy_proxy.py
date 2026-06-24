from unittest.mock import patch, MagicMock
from router.agy_proxy import _wrap_response, _is_quota_exhausted

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

def test_is_quota_exhausted_stderr_markers():
    markers = ["RESOURCE_EXHAUSTED", "code 429", "quota reached", "rate limit"]
    for marker in markers:
        assert _is_quota_exhausted(0, "", marker) is True
        assert _is_quota_exhausted(1, "", f"Error: {marker}") is True

def test_is_quota_exhausted_success():
    assert _is_quota_exhausted(0, "some valid response", "") is False

def test_is_quota_exhausted_other_error():
    assert _is_quota_exhausted(1, "", "some other random error") is False

@patch("router.agy_proxy.os.path.exists")
@patch("builtins.open")
@patch("router.agy_proxy.time.time")
def test_is_quota_exhausted_empty_reads_log(mock_time, mock_open, mock_exists):
    mock_time.return_value = 1000.0
    mock_exists.return_value = True

    mock_file = MagicMock()
    mock_file.readlines.return_value = ["line 1\n", "RESOURCE_EXHAUSTED info\n", "line 3\n"]
    mock_open.return_value.__enter__.return_value = mock_file

    with patch("router.agy_proxy._last_log_check", 0):
        assert _is_quota_exhausted(0, "", "") is True

@patch("router.agy_proxy.time.time")
def test_is_quota_exhausted_empty_throttled(mock_time):
    # Set time diff to be < 2.0
    mock_time.return_value = 1001.0
    with patch("router.agy_proxy._last_log_check", 1000.0):
        # Even without reading log, falls back to True
        assert _is_quota_exhausted(0, "", "") is True

@patch("router.agy_proxy.os.path.exists")
@patch("router.agy_proxy.time.time")
def test_is_quota_exhausted_empty_no_log_fallback(mock_time, mock_exists):
    mock_time.return_value = 1000.0
    mock_exists.return_value = False

    with patch("router.agy_proxy._last_log_check", 0):
        assert _is_quota_exhausted(0, "", "") is True
