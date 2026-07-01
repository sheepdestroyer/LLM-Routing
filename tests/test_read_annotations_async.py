import pytest
from unittest.mock import patch, AsyncMock, MagicMock
import copy
import sys
import os
import json
import asyncio

# Ensure the root directory is in the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import router.main
from router.main import _read_annotations_async

@pytest.fixture(autouse=True)
def clear_cache():
    router.main._annotations_cache.clear()
    yield

@pytest.mark.asyncio
async def test_read_annotations_async_initial_read():
    fake_path = "/tmp/annotations.json"
    fake_data = {"annotation1": "data1"}

    # Mock aiofiles.open
    mock_file = AsyncMock()
    mock_file.read.return_value = '{"annotation1": "data1"}'
    
    mock_context_manager = MagicMock()
    mock_context_manager.__aenter__ = AsyncMock(return_value=mock_file)
    mock_context_manager.__aexit__ = AsyncMock(return_value=False)
    
    mock_aiofiles_open = MagicMock(return_value=mock_context_manager)

    with patch("os.path.getmtime", return_value=100.0) as mock_getmtime, \
         patch("aiofiles.open", mock_aiofiles_open) as mock_open:

        result = await _read_annotations_async(fake_path)

        mock_getmtime.assert_called_once_with(fake_path)
        mock_open.assert_called_once_with(fake_path, "r", encoding="utf-8")
        assert result == fake_data

        # Verify cache is populated
        assert fake_path in router.main._annotations_cache
        assert router.main._annotations_cache[fake_path]["mtime"] == 100.0
        assert router.main._annotations_cache[fake_path]["data"] == fake_data

@pytest.mark.asyncio
async def test_read_annotations_async_cache_hit():
    fake_path = "/tmp/annotations.json"
    fake_data = {"annotation1": "data1"}

    # Pre-populate cache
    router.main._annotations_cache[fake_path] = {"mtime": 100.0, "data": fake_data}

    # Mock aiofiles.open (should NOT be called)
    mock_aiofiles_open = MagicMock()

    with patch("os.path.getmtime", return_value=100.0) as mock_getmtime, \
         patch("aiofiles.open", mock_aiofiles_open) as mock_open:

        result = await _read_annotations_async(fake_path)

        mock_getmtime.assert_called_once_with(fake_path)
        mock_open.assert_not_called()
        assert result == fake_data

@pytest.mark.asyncio
async def test_read_annotations_async_cache_invalidation():
    fake_path = "/tmp/annotations.json"
    fake_data_old = {"annotation1": "data1"}
    fake_data_new = {"annotation2": "data2"}

    # Pre-populate cache with old mtime
    router.main._annotations_cache[fake_path] = {"mtime": 100.0, "data": fake_data_old}

    mock_file = AsyncMock()
    mock_file.read.return_value = '{"annotation2": "data2"}'
    
    mock_context_manager = MagicMock()
    mock_context_manager.__aenter__ = AsyncMock(return_value=mock_file)
    mock_context_manager.__aexit__ = AsyncMock(return_value=False)
    
    mock_aiofiles_open = MagicMock(return_value=mock_context_manager)

    with patch("os.path.getmtime", return_value=200.0) as mock_getmtime, \
         patch("aiofiles.open", mock_aiofiles_open) as mock_open:

        result = await _read_annotations_async(fake_path)

        mock_getmtime.assert_called_once_with(fake_path)
        mock_open.assert_called_once_with(fake_path, "r", encoding="utf-8")
        assert result == fake_data_new

        # Verify cache is updated
        assert router.main._annotations_cache[fake_path]["mtime"] == 200.0
        assert router.main._annotations_cache[fake_path]["data"] == fake_data_new

@pytest.mark.asyncio
async def test_read_annotations_async_deepcopy():
    fake_path = "/tmp/annotations.json"
    fake_data = {"annotation1": {"nested": "value"}}

    # Pre-populate cache
    router.main._annotations_cache[fake_path] = {"mtime": 100.0, "data": fake_data}

    with patch("os.path.getmtime", return_value=100.0):
        # First read
        result1 = await _read_annotations_async(fake_path)

        # Mutate the result
        result1["annotation1"]["nested"] = "mutated"

        # Second read
        result2 = await _read_annotations_async(fake_path)

        # Verify second read returns original data, not mutated
        assert result2["annotation1"]["nested"] == "value"
        assert router.main._annotations_cache[fake_path]["data"]["annotation1"]["nested"] == "value"

@pytest.mark.asyncio
async def test_read_annotations_async_file_not_found():
    fake_path = "/tmp/annotations.json"

    with patch("os.path.getmtime", side_effect=FileNotFoundError):
        with pytest.raises(FileNotFoundError):
            await _read_annotations_async(fake_path)
