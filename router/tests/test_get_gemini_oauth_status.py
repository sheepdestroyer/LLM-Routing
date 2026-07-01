import pytest
import os
import sys
import json
from unittest.mock import patch, mock_open

# Ensure router directory is in sys.path
router_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if router_path not in sys.path:
    sys.path.insert(0, router_path)

import main

def test_get_gemini_oauth_status_missing_file():
    with patch("os.path.exists", return_value=False):
        result = main.get_gemini_oauth_status()
        assert result == {"status": "missing", "detail": "No oauth_creds.json found", "expiry_ms": 0}

def test_get_gemini_oauth_status_no_access_token():
    mock_data = {"expiry_date": 1234567890000}
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=json.dumps(mock_data))):
        result = main.get_gemini_oauth_status()
        assert result == {"status": "missing", "detail": "No access token in file", "expiry_ms": 0}

def test_get_gemini_oauth_status_valid_less_than_60s():
    current_time_ms = 1000000000000
    expiry_ms = current_time_ms + 45000  # 45 seconds from now
    mock_data = {"access_token": "test_token", "expiry_date": expiry_ms}
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=json.dumps(mock_data))), \
         patch("time.time", return_value=current_time_ms / 1000.0):
        result = main.get_gemini_oauth_status()
        assert result == {"status": "valid", "detail": "Expires in 45s", "expiry_ms": expiry_ms}

def test_get_gemini_oauth_status_valid_less_than_3600s():
    current_time_ms = 1000000000000
    expiry_ms = current_time_ms + 1500000  # 25 minutes from now (25 * 60 = 1500 seconds)
    mock_data = {"access_token": "test_token", "expiry_date": expiry_ms}
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=json.dumps(mock_data))), \
         patch("time.time", return_value=current_time_ms / 1000.0):
        result = main.get_gemini_oauth_status()
        assert result == {"status": "valid", "detail": "Expires in 25m 0s", "expiry_ms": expiry_ms}

def test_get_gemini_oauth_status_valid_more_than_3600s():
    current_time_ms = 1000000000000
    expiry_ms = current_time_ms + 7500000  # 2 hours, 5 minutes from now (2 * 3600 + 5 * 60 = 7500)
    mock_data = {"access_token": "test_token", "expiry_date": expiry_ms}
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=json.dumps(mock_data))), \
         patch("time.time", return_value=current_time_ms / 1000.0):
        result = main.get_gemini_oauth_status()
        assert result == {"status": "valid", "detail": "Expires in 2h 5m", "expiry_ms": expiry_ms}

def test_get_gemini_oauth_status_expired_less_than_3600s():
    current_time_ms = 1000000000000
    expiry_ms = current_time_ms - 1500000  # Expired 25 minutes ago
    mock_data = {"access_token": "test_token", "expiry_date": expiry_ms}
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=json.dumps(mock_data))), \
         patch("time.time", return_value=current_time_ms / 1000.0):
        result = main.get_gemini_oauth_status()
        assert result == {"status": "expired", "detail": "Expired 25 minutes ago", "expiry_ms": expiry_ms}

def test_get_gemini_oauth_status_expired_less_than_86400s():
    current_time_ms = 1000000000000
    expiry_ms = current_time_ms - 7500000  # Expired 2 hours ago
    mock_data = {"access_token": "test_token", "expiry_date": expiry_ms}
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=json.dumps(mock_data))), \
         patch("time.time", return_value=current_time_ms / 1000.0):
        result = main.get_gemini_oauth_status()
        assert result == {"status": "expired", "detail": "Expired 2 hours ago", "expiry_ms": expiry_ms}

def test_get_gemini_oauth_status_expired_more_than_86400s():
    current_time_ms = 1000000000000
    expiry_ms = current_time_ms - 172800000  # Expired 2 days ago (2 * 86400 * 1000 = 172800000)
    mock_data = {"access_token": "test_token", "expiry_date": expiry_ms}
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=json.dumps(mock_data))), \
         patch("time.time", return_value=current_time_ms / 1000.0):
        result = main.get_gemini_oauth_status()
        assert result == {"status": "expired", "detail": "Expired 2 days ago", "expiry_ms": expiry_ms}

def test_get_gemini_oauth_status_exception():
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", side_effect=Exception("Test error")):
        result = main.get_gemini_oauth_status()
        assert result == {"status": "error", "detail": "Test error", "expiry_ms": 0}
