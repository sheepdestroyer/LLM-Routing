import pytest
import sys
import os
import json
from pathlib import Path
from unittest.mock import patch, mock_open

# Set CONFIG_PATH for import
os.environ["CONFIG_PATH"] = str(Path(__file__).resolve().parent.parent / "config.yaml")

# Add the parent directory to the path so we can import from router
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import load_persisted_stats

@patch("main.logger.error")
@patch("os.path.exists")
@patch("builtins.open", new_callable=mock_open)
@patch("json.load")
def test_load_persisted_stats_error_path(mock_json_load, mock_open_file, mock_exists, mock_logger_error):
    # Setup mock to simulate that the file exists
    mock_exists.return_value = True

    # Setup json.load to raise an Exception
    error_message = "Mocked JSON decode error"
    mock_json_load.side_effect = Exception(error_message)

    # Call the function
    load_persisted_stats()

    # Verify that the exception was caught and logged
    mock_logger_error.assert_called_once()
    assert error_message in mock_logger_error.call_args[0][0]
