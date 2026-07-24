import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from router.agy_proxy import _wrap_response, _is_quota_exhausted

@pytest.mark.parametrize("text, model_name, prompt", [
    ("Hello, world!", "test-model", "Say hello"),
    ("", "", ""),
    ("a" * 1000, "super-long-model-name-" * 10, "b" * 500),
    ("Special chars!@#$%^&*()", "model-with-special-chars", "prompt with \n newlines"),
])
def test_wrap_response(text, model_name, prompt):
    mock_time = 1620000000
    with patch("time.time", return_value=mock_time):
        result = _wrap_response(text, model_name, prompt)

    assert result["id"] == "chatcmpl-agy-proxy"
    assert result["object"] == "chat.completion"
    assert result["created"] == mock_time
    assert result["model"] == f"{model_name} (via agy)"

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

@pytest.mark.asyncio
async def test_is_quota_exhausted_stderr_markers():
    markers = ["RESOURCE_EXHAUSTED", "code 429", "quota reached", "rate limit"]
    for marker in markers:
        assert await _is_quota_exhausted(0, "", marker) is True
        assert await _is_quota_exhausted(1, "", f"Error: {marker}") is True

@pytest.mark.asyncio
async def test_is_quota_exhausted_success():
    assert await _is_quota_exhausted(0, "some valid response", "") is False

@pytest.mark.asyncio
async def test_is_quota_exhausted_other_error():
    assert await _is_quota_exhausted(1, "", "some other random error") is False

@patch("aiofiles.open")
@patch("router.agy_proxy.time.time")
@pytest.mark.asyncio
async def test_is_quota_exhausted_empty_reads_log(mock_time, mock_open):
    mock_time.return_value = 1000.0

    mock_file = AsyncMock()
    mock_file.seek = AsyncMock()
    mock_file.tell = AsyncMock(return_value=100)
    mock_file.read = AsyncMock(return_value=b"line 1\nRESOURCE_EXHAUSTED info\nline 3\n")
    mock_open.return_value.__aenter__.return_value = mock_file

    with patch("router.agy_proxy._last_log_check", 0):
        assert await _is_quota_exhausted(0, "", "") is True

@patch("router.agy_proxy.time.time")
@pytest.mark.asyncio
async def test_is_quota_exhausted_empty_throttled(mock_time):
    # Set time diff to be < 2.0
    mock_time.return_value = 1001.0
    with patch("router.agy_proxy._last_log_check", 1000.0):
        # Even without reading log, falls back to True
        assert await _is_quota_exhausted(0, "", "") is True

@patch("aiofiles.open")
@patch("router.agy_proxy.time.time")
@pytest.mark.asyncio
async def test_is_quota_exhausted_empty_no_log_fallback(mock_time, mock_open):
    mock_time.return_value = 1000.0
    mock_open.side_effect = FileNotFoundError()

    with patch("router.agy_proxy._last_log_check", 0):
        assert await _is_quota_exhausted(0, "", "") is True

@patch("aiofiles.open")
@patch("router.agy_proxy.time.time")
@pytest.mark.asyncio
async def test_is_quota_exhausted_empty_log_no_markers(mock_time, mock_open):
    mock_time.return_value = 1000.0
    mock_file = AsyncMock()
    mock_file.seek = AsyncMock()
    mock_file.tell = AsyncMock(return_value=100)
    mock_file.read = AsyncMock(return_value=b"line 1\nsome other info\nline 3\n")
    mock_open.return_value.__aenter__.return_value = mock_file
    with patch("router.agy_proxy._last_log_check", 0):
        assert await _is_quota_exhausted(0, "", "") is False

@patch("aiofiles.open")
@patch("router.agy_proxy.time.time")
@pytest.mark.asyncio
async def test_is_quota_exhausted_empty_seek_oserror(mock_time, mock_open):
    mock_time.return_value = 1000.0
    mock_file = AsyncMock()
    mock_file.seek = AsyncMock(side_effect=OSError("seek failed"))
    mock_file.tell = AsyncMock(return_value=100)
    mock_open.return_value.__aenter__.return_value = mock_file
    with patch("router.agy_proxy._last_log_check", 0):
        # OSError swallowed by outer except → falls back to True
        assert await _is_quota_exhausted(0, "", "") is True
