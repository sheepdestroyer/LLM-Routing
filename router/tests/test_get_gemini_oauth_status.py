import os
import time
import json
import pytest
from unittest.mock import patch, mock_open

from router.main import get_gemini_oauth_status

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

def test_valid_token_less_than_60s():
    current_ms = 1000000
    # diff_sec = 30 -> 30s
    expiry_ms = current_ms + 30 * 1000
    mock_data = json.dumps({"access_token": "token", "expiry_date": expiry_ms})
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=mock_data)), \
         patch("time.time", return_value=current_ms / 1000.0):
        result = get_gemini_oauth_status()
        assert result["status"] == "valid"
        assert result["detail"] == "Expires in 30s"
        assert result["expiry_ms"] == expiry_ms

def test_valid_token_less_than_3600s():
    current_ms = 1000000
    # diff_sec = 610 -> 10m 10s
    expiry_ms = current_ms + 610 * 1000
    mock_data = json.dumps({"access_token": "token", "expiry_date": expiry_ms})
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=mock_data)), \
         patch("time.time", return_value=current_ms / 1000.0):
        result = get_gemini_oauth_status()
        assert result["status"] == "valid"
        assert result["detail"] == "Expires in 10m 10s"
        assert result["expiry_ms"] == expiry_ms

def test_valid_token_greater_than_3600s():
    current_ms = 1000000
    # diff_sec = 7320 -> 2h 2m
    expiry_ms = current_ms + 7320 * 1000
    mock_data = json.dumps({"access_token": "token", "expiry_date": expiry_ms})
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=mock_data)), \
         patch("time.time", return_value=current_ms / 1000.0):
        result = get_gemini_oauth_status()
        assert result["status"] == "valid"
        assert result["detail"] == "Expires in 2h 2m"
        assert result["expiry_ms"] == expiry_ms

def test_expired_token_less_than_3600s():
    current_ms = 1000000
    # diff_sec = -600 -> 10 minutes ago
    expiry_ms = current_ms - 600 * 1000
    mock_data = json.dumps({"access_token": "token", "expiry_date": expiry_ms})
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=mock_data)), \
         patch("time.time", return_value=current_ms / 1000.0):
        result = get_gemini_oauth_status()
        assert result["status"] == "expired"
        assert result["detail"] == "Expired 10 minutes ago"
        assert result["expiry_ms"] == expiry_ms

def test_expired_token_less_than_86400s():
    current_ms = 1000000
    # diff_sec = -7200 -> 2 hours ago
    expiry_ms = current_ms - 7200 * 1000
    mock_data = json.dumps({"access_token": "token", "expiry_date": expiry_ms})
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=mock_data)), \
         patch("time.time", return_value=current_ms / 1000.0):
        result = get_gemini_oauth_status()
        assert result["status"] == "expired"
        assert result["detail"] == "Expired 2 hours ago"
        assert result["expiry_ms"] == expiry_ms

def test_expired_token_greater_than_86400s():
    current_ms = 1000000
    # diff_sec = -172800 -> 2 days ago
    expiry_ms = current_ms - 172800 * 1000
    mock_data = json.dumps({"access_token": "token", "expiry_date": expiry_ms})
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=mock_data)), \
         patch("time.time", return_value=current_ms / 1000.0):
        result = get_gemini_oauth_status()
        assert result["status"] == "expired"
        assert result["detail"] == "Expired 2 days ago"
        assert result["expiry_ms"] == expiry_ms

def test_exception_handling():
    with patch("os.path.exists", side_effect=Exception("Test Exception")):
        result = get_gemini_oauth_status()
        assert result["status"] == "error"
        assert result["detail"] == "Test Exception"
        assert result["expiry_ms"] == 0
