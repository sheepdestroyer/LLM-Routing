import json
import subprocess
import time
from unittest.mock import mock_open

import sync_gemini_token

def test_sync_gemini_token_invalid_expiry_date(capsys, monkeypatch):
    mock_json = {
        "token": {
            "access_token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "token_type": "Bearer",
            "expiry": "this-is-not-a-valid-date"
        }
    }

    mock_result = subprocess.CompletedProcess(
        args=['secret-tool', 'lookup', 'service', 'gemini', 'username', 'antigravity'],
        returncode=0,
        stdout=json.dumps(mock_json)
    )

    def mock_run(*args, **kwargs):
        return mock_result

    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setattr(time, "time", lambda: 1000.0)

    m_open = mock_open()
    monkeypatch.setattr("builtins.open", m_open)
    monkeypatch.setattr("os.makedirs", lambda *args, **kwargs: None)

    sync_gemini_token.main()

    # Check that open was called with correct arguments
    m_open.assert_called_once_with(sync_gemini_token.TARGET_PATH, "w")

    # Get the data written to the file
    written_data = "".join(call.args[0] for call in m_open().write.call_args_list)
    written_json = json.loads(written_data)

    assert written_json["access_token"] == "fake-access-token"
    # (1000.0 + 3600) * 1000 = 4600000
    assert written_json["expiry_date"] == 4600000

    # Check stderr for the expected warning
    captured = capsys.readouterr()
    assert "Warning: Failed to parse expiry date 'this-is-not-a-valid-date'" in captured.err
    assert "Defaulting to 1 hour from now." in captured.err
