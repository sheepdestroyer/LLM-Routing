import pytest
import sys
import os
import json
import copy
from pathlib import Path
from unittest.mock import patch, mock_open

# Set CONFIG_PATH for import
os.environ["CONFIG_PATH"] = str(Path(__file__).resolve().parent.parent / "config.yaml")

# Add the parent directory to the path so we can import from router
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main

@pytest.fixture
def reset_stats(monkeypatch):
    original_stats = copy.deepcopy(main.stats)
    monkeypatch.setattr(main, 'stats', original_stats)
    yield

def test_load_persisted_stats_exists_valid(reset_stats):
    mock_stats = {
        "total_requests": 999,
        "tool_tokens": {"tree": 50, "other": 10},
        "new_key": "new_value"
    }

    mock_timeline = [{"event": "start"}]

    timeline_path = os.path.join(os.path.dirname(main.CONFIG_PATH), "router_timeline.json")

    def mock_exists(path):
        if path == main.STATS_JSON_PATH:
            return True
        if path == timeline_path:
            return True
        return False

    def mock_open_func(path, *args, **kwargs):
        if path == main.STATS_JSON_PATH:
            return mock_open(read_data=json.dumps(mock_stats))()
        if path == timeline_path:
            return mock_open(read_data=json.dumps(mock_timeline))()
        return mock_open(read_data="")()

    with patch('os.path.exists', side_effect=mock_exists):
        with patch('builtins.open', side_effect=mock_open_func):
            main.load_persisted_stats()

    assert main.stats["total_requests"] == 999
    assert main.stats["tool_tokens"]["tree"] == 50
    assert main.stats["tool_tokens"]["other"] == 10
    assert main.stats["new_key"] == "new_value"
    assert main.stats["timeline"] == [{"event": "start"}]

def test_load_persisted_stats_no_file(reset_stats):
    original = copy.deepcopy(main.stats)

    with patch('os.path.exists', return_value=False):
        main.load_persisted_stats()

    assert main.stats == original

def test_load_persisted_stats_invalid_json(reset_stats):
    original = copy.deepcopy(main.stats)

    def mock_exists(path):
        return path == main.STATS_JSON_PATH

    with patch('os.path.exists', side_effect=mock_exists):
        with patch('builtins.open', mock_open(read_data="{invalid_json:")):
            main.load_persisted_stats()

    assert main.stats == original

def test_load_persisted_stats_timeline_invalid(reset_stats):
    mock_stats = {"total_requests": 123}

    timeline_path = os.path.join(os.path.dirname(main.CONFIG_PATH), "router_timeline.json")

    def mock_exists(path):
        return path in (main.STATS_JSON_PATH, timeline_path)

    def mock_open_func(path, *args, **kwargs):
        if path == main.STATS_JSON_PATH:
            return mock_open(read_data=json.dumps(mock_stats))()
        if path == timeline_path:
            return mock_open(read_data="[invalid timeline")()
        return mock_open(read_data="")()

    with patch('os.path.exists', side_effect=mock_exists):
        with patch('builtins.open', side_effect=mock_open_func):
            main.load_persisted_stats()

    assert main.stats["total_requests"] == 123
    assert "timeline" not in main.stats or main.stats["timeline"] != "[invalid timeline"
