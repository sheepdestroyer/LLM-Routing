import pytest
import json
from unittest.mock import patch, mock_open

import main

@pytest.mark.asyncio
async def test_get_gemini_oauth_status_missing_file():
    with patch("os.path.exists", return_value=False):
        result = await main.get_gemini_oauth_status()
        assert result == {"status": "missing", "detail": "No oauth_creds.json found", "expiry_ms": 0}

@pytest.mark.asyncio
async def test_get_gemini_oauth_status_no_access_token():
    mock_data = {"expiry_date": 1234567890000}
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=json.dumps(mock_data))):
        result = await main.get_gemini_oauth_status()
        assert result == {"status": "missing", "detail": "No access token in file", "expiry_ms": 0}

@pytest.mark.parametrize("delta, expected_status, expected_detail", [
    (45000, "valid", "Expires in 45s"),
    (1500000, "valid", "Expires in 25m 0s"),
    (7500000, "valid", "Expires in 2h 5m"),
    (-1500000, "expired", "Expired 25 minutes ago"),
    (-7500000, "expired", "Expired 2 hours ago"),
    (-172800000, "expired", "Expired 2 days ago"),
])
@pytest.mark.asyncio
async def test_get_gemini_oauth_status_scenarios(delta, expected_status, expected_detail):
    current_time_ms = 1000000000000
    expiry_ms = current_time_ms + delta
    mock_data = {"access_token": "test_token", "expiry_date": expiry_ms}
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=json.dumps(mock_data))), \
         patch("time.time", return_value=current_time_ms / 1000.0):
        result = await main.get_gemini_oauth_status()
        assert result == {"status": expected_status, "detail": expected_detail, "expiry_ms": expiry_ms}

@pytest.mark.asyncio
async def test_get_gemini_oauth_status_exception():
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", side_effect=Exception("Test error")):
        result = await main.get_gemini_oauth_status()
        assert result == {"status": "error", "detail": "Test error", "expiry_ms": 0}
