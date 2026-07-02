import json
import os
import pytest
from unittest.mock import patch, mock_open, MagicMock
import sys
import time

# Import the module to test
import sync_gemini_token

@pytest.fixture
def mock_subprocess():
    with patch('sync_gemini_token.subprocess.run') as mock_run:
        yield mock_run

@pytest.fixture
def mock_os_makedirs():
    with patch('sync_gemini_token.os.makedirs') as mock_makedirs:
        yield mock_makedirs

@pytest.fixture
def mock_time():
    with patch('sync_gemini_token.time.time') as mock_t:
        mock_t.return_value = 1600000000.0
        yield mock_t

def test_happy_path(mock_subprocess, mock_os_makedirs, mock_time, capsys):
    mock_result = MagicMock()
    mock_result.returncode = 0

    # Valid JSON with expiry containing offset and nanoseconds
    valid_json = {
        "token": {
            "access_token": "test_access",
            "refresh_token": "test_refresh",
            "token_type": "Bearer",
            "expiry": "2026-06-06T18:14:35.496934445+02:00"
        }
    }
    mock_result.stdout = json.dumps(valid_json)
    mock_subprocess.return_value = mock_result

    m_open = mock_open()
    with patch('builtins.open', m_open):
        sync_gemini_token.main()

    mock_subprocess.assert_called_once_with(
        ['secret-tool', 'lookup', 'service', 'gemini', 'username', 'antigravity'],
        capture_output=True,
        text=True
    )

    mock_os_makedirs.assert_called_once_with(os.path.dirname(sync_gemini_token.TARGET_PATH), exist_ok=True)
    m_open.assert_called_once_with(sync_gemini_token.TARGET_PATH, "w")

    # Check the written file
    handle = m_open()
    written_data = "".join(call.args[0] for call in handle.write.call_args_list)
    parsed_written_data = json.loads(written_data)

    assert parsed_written_data["access_token"] == "test_access"
    assert parsed_written_data["refresh_token"] == "test_refresh"
    assert parsed_written_data["token_type"] == "Bearer"
    assert "expiry_date" in parsed_written_data

    # Assert standard output messages using capsys
    captured = capsys.readouterr()
    assert "✓ Success: Synced fresh token. Expires in" in captured.out

def test_secret_tool_failure(mock_subprocess, capsys):
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "Command failed"
    mock_subprocess.return_value = mock_result

    with pytest.raises(SystemExit) as excinfo:
        sync_gemini_token.main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Error: secret-tool failed with return code 1" in captured.err
    assert "Command failed" in captured.err

def test_empty_output(mock_subprocess, capsys):
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "   \n"
    mock_subprocess.return_value = mock_result

    with pytest.raises(SystemExit) as excinfo:
        sync_gemini_token.main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Error: No keyring credentials found" in captured.err

def test_missing_token_key(mock_subprocess, capsys):
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({"other_key": "value"})
    mock_subprocess.return_value = mock_result

    with pytest.raises(SystemExit) as excinfo:
        sync_gemini_token.main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Error: Keyring response missing 'token' key" in captured.err

def test_missing_access_token(mock_subprocess, capsys):
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({"token": {"refresh_token": "abc"}})
    mock_subprocess.return_value = mock_result

    with pytest.raises(SystemExit) as excinfo:
        sync_gemini_token.main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Error: Missing access_token in keyring data" in captured.err

def test_fallback_expiry(mock_subprocess, mock_os_makedirs, mock_time, capsys):
    mock_result = MagicMock()
    mock_result.returncode = 0
    # Provide an invalid expiry date string
    valid_json = {
        "token": {
            "access_token": "test_access",
            "expiry": "invalid-date"
        }
    }
    mock_result.stdout = json.dumps(valid_json)
    mock_subprocess.return_value = mock_result

    m_open = mock_open()
    with patch('builtins.open', m_open):
        sync_gemini_token.main()

    mock_os_makedirs.assert_called_once_with(os.path.dirname(sync_gemini_token.TARGET_PATH), exist_ok=True)

    # Assert standard output/error messages using capsys
    captured = capsys.readouterr()
    assert "Warning: Failed to parse expiry date 'invalid-date'" in captured.err
    assert "Defaulting to 1 hour from now." in captured.err
    assert "✓ Success: Synced fresh token. Expires in 60m 0s" in captured.out

    handle = m_open()
    written_data = "".join(call.args[0] for call in handle.write.call_args_list)
    parsed_written_data = json.loads(written_data)

    expected_expiry_ms = int((1600000000.0 + 3600) * 1000)
    assert parsed_written_data["expiry_date"] == expected_expiry_ms

def test_expired_token(mock_subprocess, mock_os_makedirs, mock_time, capsys):
    mock_result = MagicMock()
    mock_result.returncode = 0

    # Expiry is in the past: September 13, 2020 12:00:00 UTC = 1599998400.0 seconds
    # which is 1600 seconds before mock_time (1600000000.0)
    expired_json = {
        "token": {
            "access_token": "test_access",
            "refresh_token": "test_refresh",
            "token_type": "Bearer",
            "expiry": "2020-09-13T12:00:00+00:00"
        }
    }
    mock_result.stdout = json.dumps(expired_json)
    mock_subprocess.return_value = mock_result

    m_open = mock_open()
    with patch('builtins.open', m_open):
        sync_gemini_token.main()

    mock_os_makedirs.assert_called_once_with(os.path.dirname(sync_gemini_token.TARGET_PATH), exist_ok=True)

    # Assert standard output message using capsys for expired token
    captured = capsys.readouterr()
    # 1600 seconds = 26m 40s ago
    assert "✓ Success: Synced expired token (expired 26m ago)" in captured.out

def test_malformed_json(mock_subprocess, capsys):
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "{invalid-json"
    mock_subprocess.return_value = mock_result

    with pytest.raises(SystemExit) as excinfo:
        sync_gemini_token.main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Exception: " in captured.err

def test_general_exception(mock_subprocess, capsys):
    # Make subprocess.run raise an exception directly
    mock_subprocess.side_effect = Exception("Unexpected error")

    with pytest.raises(SystemExit) as excinfo:
        sync_gemini_token.main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Exception: Unexpected error" in captured.err
