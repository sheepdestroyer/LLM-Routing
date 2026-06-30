import os
import time
import json
import pytest
from unittest.mock import patch, mock_open

from router.main import get_gemini_oauth_status

def setup_mock_time(offset_sec: int) -> tuple[int, int]:
    current_ms = 1000000
    expiry_ms = current_ms + offset_sec * 1000
    return current_ms, expiry_ms

def test_missing_file():
    with patch("os.path.exists", return_value=False):
        result = get_gemini_oauth_status()
        assert result["status"] == "missing"
        assert result["detail"] == "No oauth_creds.json found"
        assert result["expiry_ms"] == 0

def test_missing_access_token():
    mock_data = json.dumps({"expiry_date": 1000000})
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=mock_data)):
        result = get_gemini_oauth_status()
        assert result["status"] == "missing"
        assert result["detail"] == "No access token in file"
        assert result["expiry_ms"] == 0

@pytest.mark.parametrize("offset_sec, expected_status, expected_detail", [
    (30, "valid", "Expires in 30s"),
    (610, "valid", "Expires in 10m 10s"),
    (7320, "valid", "Expires in 2h 2m"),
    (-600, "expired", "Expired 10 minutes ago"),
    (-7200, "expired", "Expired 2 hours ago"),
    (-172800, "expired", "Expired 2 days ago")
])
def test_token_time_scenarios(offset_sec, expected_status, expected_detail):
    current_ms, expiry_ms = setup_mock_time(offset_sec)
    mock_data = json.dumps({"access_token": "token", "expiry_date": expiry_ms})
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=mock_data)), \
         patch("time.time", return_value=current_ms / 1000.0):
        result = get_gemini_oauth_status()
        assert result["status"] == expected_status
        assert result["detail"] == expected_detail
        assert result["expiry_ms"] == expiry_ms

def test_exception_handling():
    with patch("os.path.exists", side_effect=Exception("Test Exception")):
        result = get_gemini_oauth_status()
        assert result["status"] == "error"
        assert result["detail"] == "Test Exception"
        assert result["expiry_ms"] == 0
