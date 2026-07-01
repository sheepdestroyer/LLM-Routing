import pytest
import os
import json
import sys
from unittest.mock import patch, mock_open

# Ensure router directory is in sys.path based on __file__ instead of getcwd
router_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if router_path not in sys.path:
    sys.path.insert(0, router_path)

import main

def test_load_persisted_stats_success():
    mock_stats = {
        "total_requests": 100,
        "some_dict": {"a": 1, "b": 2},
        "new_key": "new_value"
    }

    mock_timeline = [
        {"time": "12:00", "total_requests": 10, "avg_latency": 50.0}
    ]

    # We patch the stats dict directly to avoid replacing the reference
    # and to ensure automatic teardown even if an assertion fails.
    initial_stats = {"some_dict": {"c": 3}, "timeline": []}

    def mock_exists(path):
        if path == main.STATS_JSON_PATH:
            return True
        if path.endswith("router_timeline.json"):
            return True
        return False

    # We only intercept specific files, otherwise call the real open
    real_open = open
    def mock_open_file(file, mode="r", *args, **kwargs):
        if file == main.STATS_JSON_PATH:
            return mock_open(read_data=json.dumps(mock_stats))()
        if type(file) is str and file.endswith("router_timeline.json"):
            return mock_open(read_data=json.dumps(mock_timeline))()
        return real_open(file, mode, *args, **kwargs)

    with patch.dict('main.stats', initial_stats, clear=True), \
         patch('os.path.exists', side_effect=mock_exists), \
         patch('builtins.open', side_effect=mock_open_file):

        main.load_persisted_stats()

        # Verify stats updated correctly
        assert main.stats["total_requests"] == 100
        # Check dictionary merging
        assert "some_dict" in main.stats
        assert main.stats["some_dict"]["a"] == 1
        assert main.stats["some_dict"]["c"] == 3
        # Check new key added
        assert main.stats["new_key"] == "new_value"
        # Check timeline loaded
        assert main.stats["timeline"] == mock_timeline

def test_load_persisted_stats_files_missing():
    initial_stats = {"total_requests": 50}

    def mock_exists(path):
        return False

    with patch.dict('main.stats', initial_stats, clear=True), \
         patch('os.path.exists', side_effect=mock_exists):

        main.load_persisted_stats()

        # Verify stats didn't change
        assert main.stats == initial_stats

def test_load_persisted_stats_invalid_json():
    initial_stats = {"total_requests": 50}

    def mock_exists(path):
        if path == main.STATS_JSON_PATH:
            return True
        return False

    real_open = open
    def mock_open_file(file, mode="r", *args, **kwargs):
        if file == main.STATS_JSON_PATH:
            return mock_open(read_data="invalid json")()
        return real_open(file, mode, *args, **kwargs)

    with patch.dict('main.stats', initial_stats, clear=True), \
         patch('os.path.exists', side_effect=mock_exists), \
         patch('builtins.open', side_effect=mock_open_file), \
         patch('main.logger.error') as mock_logger:

        main.load_persisted_stats()

        assert main.stats == initial_stats
        mock_logger.assert_called_once()
        assert "Failed to load persisted stats" in mock_logger.call_args[0][0]
