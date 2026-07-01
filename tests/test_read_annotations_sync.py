import pytest
from unittest.mock import patch, mock_open
import copy

# Mock config so router.main can be imported
import sys
import os

# Ensure the root directory is in the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Need to set up environment or mock configuration depending on how it's done in the rest of tests
from router.main import _read_annotations_sync
import router.main

@pytest.fixture(autouse=True)
def clear_cache():
    router.main._annotations_cache.clear()
    yield

def test_read_annotations_sync_initial_read():
    fake_path = "/tmp/annotations.json"
    fake_data = {"annotation1": "data1"}

    with patch("os.path.getmtime", return_value=100.0) as mock_getmtime, \
         patch("builtins.open", mock_open(read_data='{"annotation1": "data1"}')) as mock_file, \
         patch("json.load", return_value=fake_data) as mock_json_load:

        result = _read_annotations_sync(fake_path)

        mock_getmtime.assert_called_once_with(fake_path)
        mock_file.assert_called_once_with(fake_path, "r", encoding="utf-8")
        assert result == fake_data

        # Verify cache is populated
        assert fake_path in router.main._annotations_cache
        assert router.main._annotations_cache[fake_path]["mtime"] == 100.0
        assert router.main._annotations_cache[fake_path]["data"] == fake_data

def test_read_annotations_sync_cache_hit():
    fake_path = "/tmp/annotations.json"
    fake_data = {"annotation1": "data1"}

    # Pre-populate cache
    router.main._annotations_cache[fake_path] = {"mtime": 100.0, "data": fake_data}

    with patch("os.path.getmtime", return_value=100.0) as mock_getmtime, \
         patch("builtins.open", mock_open()) as mock_file:

        result = _read_annotations_sync(fake_path)

        mock_getmtime.assert_called_once_with(fake_path)
        mock_file.assert_not_called()
        assert result == fake_data

def test_read_annotations_sync_cache_invalidation():
    fake_path = "/tmp/annotations.json"
    fake_data_old = {"annotation1": "data1"}
    fake_data_new = {"annotation2": "data2"}

    # Pre-populate cache with old mtime
    router.main._annotations_cache[fake_path] = {"mtime": 100.0, "data": fake_data_old}

    with patch("os.path.getmtime", return_value=200.0) as mock_getmtime, \
         patch("builtins.open", mock_open(read_data='{"annotation2": "data2"}')) as mock_file, \
         patch("json.load", return_value=fake_data_new) as mock_json_load:

        result = _read_annotations_sync(fake_path)

        mock_getmtime.assert_called_once_with(fake_path)
        mock_file.assert_called_once_with(fake_path, "r", encoding="utf-8")
        assert result == fake_data_new

        # Verify cache is updated
        assert router.main._annotations_cache[fake_path]["mtime"] == 200.0
        assert router.main._annotations_cache[fake_path]["data"] == fake_data_new

def test_read_annotations_sync_deepcopy():
    fake_path = "/tmp/annotations.json"
    fake_data = {"annotation1": {"nested": "value"}}

    # Pre-populate cache
    router.main._annotations_cache[fake_path] = {"mtime": 100.0, "data": fake_data}

    with patch("os.path.getmtime", return_value=100.0):
        # First read
        result1 = _read_annotations_sync(fake_path)

        # Mutate the result
        result1["annotation1"]["nested"] = "mutated"

        # Second read
        result2 = _read_annotations_sync(fake_path)

        # Verify second read returns original data, not mutated
        assert result2["annotation1"]["nested"] == "value"
        assert router.main._annotations_cache[fake_path]["data"]["annotation1"]["nested"] == "value"

def test_read_annotations_sync_file_not_found():
    fake_path = "/tmp/annotations.json"

    with patch("os.path.getmtime", side_effect=FileNotFoundError):
        with pytest.raises(FileNotFoundError):
            _read_annotations_sync(fake_path)
