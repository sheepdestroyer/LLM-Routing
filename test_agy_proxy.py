import pytest
from unittest.mock import patch, mock_open
import router.agy_proxy
from router.agy_proxy import _is_quota_exhausted

@pytest.fixture(autouse=True)
def reset_global_throttle():
    """Reset the global throttle variable before each test."""
    router.agy_proxy._last_log_check = 0.0

def test_direct_stderr_exhaustion():
    assert _is_quota_exhausted(1, "", "RESOURCE_EXHAUSTED: out of quota") == True
    assert _is_quota_exhausted(1, "", "error code 429 received") == True
    assert _is_quota_exhausted(1, "", "your quota reached the limit") == True
    assert _is_quota_exhausted(1, "", "rate limit exceeded") == True

def test_success_response():
    assert _is_quota_exhausted(0, "success response", "") == False
    assert _is_quota_exhausted(0, "success", "some warning") == False

def test_non_quota_error():
    assert _is_quota_exhausted(1, "", "syntax error") == False
    assert _is_quota_exhausted(127, "", "command not found") == False

@patch("os.path.exists")
@patch("time.time")
def test_silent_failure_fallback(mock_time, mock_exists):
    # Empty stdout/stderr with rc=0 but no log file
    mock_exists.return_value = False
    mock_time.return_value = 100.0

    assert _is_quota_exhausted(0, "", "") == True

@patch("builtins.open", new_callable=mock_open, read_data="some log\nRESOURCE_EXHAUSTED\n")
@patch("os.path.exists")
@patch("time.time")
def test_silent_failure_with_log_exhaustion(mock_time, mock_exists, mock_file):
    mock_exists.return_value = True
    mock_time.return_value = 100.0

    assert _is_quota_exhausted(0, "", "") == True
    mock_file.assert_called_once()

@patch("builtins.open", new_callable=mock_open, read_data="normal log\nno errors\n")
@patch("os.path.exists")
@patch("time.time")
def test_silent_failure_with_log_no_exhaustion(mock_time, mock_exists, mock_file):
    mock_exists.return_value = True
    mock_time.return_value = 100.0

    # Returns True even if log doesn't have it because "Empty stdout+stderr with rc=0 strongly suggests quota exhaustion"
    assert _is_quota_exhausted(0, "", "") == True
    mock_file.assert_called_once()

@patch("builtins.open", new_callable=mock_open, read_data="RESOURCE_EXHAUSTED\n")
@patch("os.path.exists")
@patch("time.time")
def test_throttling_behavior(mock_time, mock_exists, mock_file):
    mock_exists.return_value = True
    mock_time.return_value = 100.0

    # First call - should read file
    assert _is_quota_exhausted(0, "", "") == True
    assert mock_file.call_count == 1

    # Second call immediately after (time = 101.0, diff = 1s < 2s)
    mock_time.return_value = 101.0
    assert _is_quota_exhausted(0, "", "") == True
    assert mock_file.call_count == 1 # File not read again due to throttle

    # Third call after 2 seconds (time = 103.0, diff = 3s > 2s)
    mock_time.return_value = 103.0
    assert _is_quota_exhausted(0, "", "") == True
    assert mock_file.call_count == 2 # File read again

@patch("builtins.open")
@patch("os.path.exists")
@patch("time.time")
def test_log_read_exception(mock_time, mock_exists, mock_file):
    mock_exists.return_value = True
    mock_time.return_value = 100.0
    mock_file.side_effect = Exception("Permission denied")

    # Should catch exception and still return True as fallback
    assert _is_quota_exhausted(0, "", "") == True
