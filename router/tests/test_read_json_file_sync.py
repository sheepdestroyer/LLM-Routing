import json
from unittest.mock import mock_open, patch
import pytest

from router import main


def test_read_json_file_sync_success():
    mock_data = '{"key": "value"}'
    with patch("builtins.open", mock_open(read_data=mock_data)):
        result = main._read_json_file_sync("dummy_path.json")
        assert result == {"key": "value"}


@pytest.mark.parametrize(
    "mock_kwargs, expected_exc, match_msg",
    [
        (
            {"new": mock_open(read_data='{"key": "value"')},
            json.JSONDecodeError,
            r"Unterminated string starting at|Expecting",
        ),
        (
            {"side_effect": FileNotFoundError("No such file or directory: 'dummy_path.json'")},
            FileNotFoundError,
            r"No such file or directory",
        ),
        (
            {"side_effect": PermissionError("Permission denied: 'dummy_path.json'")},
            PermissionError,
            r"Permission denied",
        ),
    ],
)
def test_read_json_file_sync_errors(mock_kwargs, expected_exc, match_msg):
    with patch("builtins.open", **mock_kwargs):
        with pytest.raises(expected_exc, match=match_msg):
            main._read_json_file_sync("dummy_path.json")
