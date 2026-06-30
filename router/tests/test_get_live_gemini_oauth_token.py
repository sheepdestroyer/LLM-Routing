import os
import sys
import json
import time
import pytest
from unittest.mock import patch, mock_open

# Add router to sys.path properly
router_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if router_path not in sys.path:
    sys.path.insert(0, router_path)

import main

def test_get_live_gemini_oauth_token_valid():
    """Test retrieving a valid, unexpired token."""
    mock_data = {
        "access_token": "valid_token_123",
        "expiry_date": int(time.time() * 1000) + 3600000  # 1 hour in the future
    }
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=json.dumps(mock_data))), \
         patch("main.logger.info") as mock_logger:
        token = main.get_live_gemini_oauth_token()
        assert token == "valid_token_123"
        mock_logger.assert_called_with("🔑 Found valid, unexpired Gemini OAuth token from host!")

def test_get_live_gemini_oauth_token_expired():
    """Test that an expired token returns None."""
    mock_data = {
        "access_token": "expired_token_456",
        "expiry_date": int(time.time() * 1000) - 3600000  # 1 hour in the past
    }
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=json.dumps(mock_data))), \
         patch("main.logger.debug") as mock_logger:
        token = main.get_live_gemini_oauth_token()
        assert token is None
        mock_logger.assert_called_with("Gemini OAuth token on disk is expired — agy uses system keyring instead.")

def test_get_live_gemini_oauth_token_missing_file():
    """Test behavior when the credentials file does not exist."""
    with patch("os.path.exists", return_value=False):
        token = main.get_live_gemini_oauth_token()
        assert token is None

def test_get_live_gemini_oauth_token_invalid_json():
    """Test behavior when the credentials file contains invalid JSON."""
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data="invalid json")), \
         patch("main.logger.error") as mock_logger:
        token = main.get_live_gemini_oauth_token()
        assert token is None
        mock_logger.assert_called()

def test_get_live_gemini_oauth_token_missing_access_token():
    """Test behavior when the JSON does not contain an access_token."""
    mock_data = {
        "expiry_date": int(time.time() * 1000) + 3600000
    }
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=json.dumps(mock_data))), \
         patch("main.logger.debug") as mock_logger:
        token = main.get_live_gemini_oauth_token()
        assert token is None
        mock_logger.assert_called_with("Gemini OAuth token on disk is expired — agy uses system keyring instead.")
