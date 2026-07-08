import pytest
import json
from unittest.mock import patch, mock_open

from router import main

def test_read_json_file_sync_success():
    mock_data = '{"key": "value"}'
    with patch("builtins.open", mock_open(read_data=mock_data)):
        result = main._read_json_file_sync("dummy_path.json")
        assert result == {"key": "value"}

def test_read_json_file_sync_invalid_json():
    mock_data = '{"key": "value"'  # Missing closing brace
    with patch("builtins.open", mock_open(read_data=mock_data)):
        with pytest.raises(json.JSONDecodeError):
            main._read_json_file_sync("dummy_path.json")

def test_read_json_file_sync_file_not_found():
    with patch("builtins.open", side_effect=FileNotFoundError("No such file or directory: 'dummy_path.json'")):
        with pytest.raises(FileNotFoundError):
            main._read_json_file_sync("dummy_path.json")

def test_read_json_file_sync_permission_error():
    with patch("builtins.open", side_effect=PermissionError("Permission denied: 'dummy_path.json'")):
        with pytest.raises(PermissionError):
            main._read_json_file_sync("dummy_path.json")
