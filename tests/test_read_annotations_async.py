import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the root directory is in the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import router.main
from router.main import _read_annotations_async


def make_mock_aiofiles_open(content: bytes | str):
    mock_file = AsyncMock()
    mock_file.read.return_value = content
    mock_context_manager = MagicMock()
    mock_context_manager.__aenter__ = AsyncMock(return_value=mock_file)
    mock_context_manager.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=mock_context_manager)


def make_mock_stat(mtime_ns: int = 100_000_000_000, size: int = 25, ino: int = 12345):
    stat = MagicMock()
    stat.st_mtime_ns = mtime_ns
    stat.st_size = size
    stat.st_ino = ino
    return stat


@pytest.fixture(autouse=True)
def clear_cache():
    router.main._annotations_cache.clear()
    yield


@pytest.mark.asyncio
async def test_read_annotations_async_initial_read():
    fake_path = "/tmp/annotations.json"
    fake_data = {"annotation1": "data1"}

    mock_aiofiles_open = make_mock_aiofiles_open(b'{"annotation1": "data1"}')
    mock_stat = make_mock_stat(mtime_ns=100_000_000_000, size=25, ino=12345)

    with (
        patch("os.stat", return_value=mock_stat) as mock_stat_fn,
        patch("aiofiles.open", mock_aiofiles_open) as mock_open,
    ):
        result = await _read_annotations_async(fake_path)

        mock_stat_fn.assert_called_once_with(fake_path)
        mock_open.assert_called_once_with(fake_path, "rb")
        assert result == fake_data

        # Verify cache is populated with raw bytes and stats
        assert fake_path in router.main._annotations_cache
        assert router.main._annotations_cache[fake_path]["mtime_ns"] == 100_000_000_000
        assert router.main._annotations_cache[fake_path]["size"] == 25
        assert router.main._annotations_cache[fake_path]["ino"] == 12345
        assert router.main._annotations_cache[fake_path]["bytes"] == b'{"annotation1": "data1"}'


@pytest.mark.asyncio
async def test_read_annotations_async_cache_hit():
    fake_path = "/tmp/annotations.json"
    fake_data = {"annotation1": "data1"}

    # Pre-populate cache with raw bytes
    router.main._annotations_cache[fake_path] = {
        "mtime_ns": 100_000_000_000,
        "size": 25,
        "ino": 12345,
        "bytes": b'{"annotation1": "data1"}',
    }

    mock_aiofiles_open = MagicMock()
    mock_stat = make_mock_stat(mtime_ns=100_000_000_000, size=25, ino=12345)

    with (
        patch("os.stat", return_value=mock_stat) as mock_stat_fn,
        patch("aiofiles.open", mock_aiofiles_open) as mock_open,
    ):
        result = await _read_annotations_async(fake_path)

        mock_stat_fn.assert_called_once_with(fake_path)
        mock_open.assert_not_called()
        assert result == fake_data


@pytest.mark.asyncio
async def test_read_annotations_async_cache_invalidation_mtime():
    fake_path = "/tmp/annotations.json"
    fake_data_new = {"annotation2": "data2"}

    # Pre-populate cache with old mtime_ns
    router.main._annotations_cache[fake_path] = {
        "mtime_ns": 100_000_000_000,
        "size": 25,
        "ino": 12345,
        "bytes": b'{"annotation1": "data1"}',
    }

    mock_aiofiles_open = make_mock_aiofiles_open(b'{"annotation2": "data2"}')
    mock_stat = make_mock_stat(mtime_ns=200_000_000_000, size=25, ino=12345)

    with (
        patch("os.stat", return_value=mock_stat) as mock_stat_fn,
        patch("aiofiles.open", mock_aiofiles_open) as mock_open,
    ):
        result = await _read_annotations_async(fake_path)

        mock_stat_fn.assert_called_once_with(fake_path)
        mock_open.assert_called_once_with(fake_path, "rb")
        assert result == fake_data_new

        # Verify cache is updated
        assert router.main._annotations_cache[fake_path]["mtime_ns"] == 200_000_000_000
        assert router.main._annotations_cache[fake_path]["size"] == 25
        assert router.main._annotations_cache[fake_path]["ino"] == 12345
        assert router.main._annotations_cache[fake_path]["bytes"] == b'{"annotation2": "data2"}'


@pytest.mark.asyncio
async def test_read_annotations_async_cache_invalidation_size():
    fake_path = "/tmp/annotations.json"
    fake_data_new = {"annotation2": "data2"}

    # Pre-populate cache with same mtime but different size
    router.main._annotations_cache[fake_path] = {
        "mtime_ns": 100_000_000_000,
        "size": 25,
        "ino": 12345,
        "bytes": b'{"annotation1": "data1"}',
    }

    mock_aiofiles_open = make_mock_aiofiles_open(b'{"annotation2": "data2"}')
    mock_stat = make_mock_stat(mtime_ns=100_000_000_000, size=50, ino=12345)

    with (
        patch("os.stat", return_value=mock_stat) as mock_stat_fn,
        patch("aiofiles.open", mock_aiofiles_open) as mock_open,
    ):
        result = await _read_annotations_async(fake_path)

        mock_stat_fn.assert_called_once_with(fake_path)
        mock_open.assert_called_once_with(fake_path, "rb")
        assert result == fake_data_new

        # Verify cache is updated
        assert router.main._annotations_cache[fake_path]["mtime_ns"] == 100_000_000_000
        assert router.main._annotations_cache[fake_path]["size"] == 50
        assert router.main._annotations_cache[fake_path]["bytes"] == b'{"annotation2": "data2"}'


@pytest.mark.asyncio
async def test_read_annotations_async_cache_invalidation_ino():
    fake_path = "/tmp/annotations.json"
    fake_data_new = {"annotation3": "data3"}

    # Pre-populate cache with old inode
    router.main._annotations_cache[fake_path] = {
        "mtime_ns": 100_000_000_000,
        "size": 25,
        "ino": 12345,
        "bytes": b'{"annotation1": "data1"}',
    }

    mock_aiofiles_open = make_mock_aiofiles_open(b'{"annotation3": "data3"}')
    # Same mtime and size, but different inode (from atomic replace)
    mock_stat = make_mock_stat(mtime_ns=100_000_000_000, size=25, ino=99999)

    with (
        patch("os.stat", return_value=mock_stat) as mock_stat_fn,
        patch("aiofiles.open", mock_aiofiles_open) as mock_open,
    ):
        result = await _read_annotations_async(fake_path)

        mock_stat_fn.assert_called_once_with(fake_path)
        mock_open.assert_called_once_with(fake_path, "rb")
        assert result == fake_data_new
        assert router.main._annotations_cache[fake_path]["ino"] == 99999


@pytest.mark.asyncio
async def test_read_annotations_async_independent_objects_mutation():
    fake_path = "/tmp/annotations.json"
    # Pre-populate cache with raw bytes
    router.main._annotations_cache[fake_path] = {
        "mtime_ns": 100_000_000_000,
        "size": 35,
        "ino": 12345,
        "bytes": b'{"annotation1": {"nested": "value"}}',
    }

    mock_stat = make_mock_stat(mtime_ns=100_000_000_000, size=35, ino=12345)
    with patch("os.stat", return_value=mock_stat):
        # First read
        result1 = await _read_annotations_async(fake_path)
        assert result1["annotation1"]["nested"] == "value"

        # Mutate the returned result
        result1["annotation1"]["nested"] = "mutated"

        # Second read (cache hit with fresh orjson deserialization)
        result2 = await _read_annotations_async(fake_path)

        # Verify second read returns independent original object without mutation
        assert result2["annotation1"]["nested"] == "value"


@pytest.mark.asyncio
async def test_read_annotations_async_file_not_found():
    fake_path = "/tmp/annotations.json"

    with patch("os.stat", side_effect=FileNotFoundError):
        with pytest.raises(FileNotFoundError):
            await _read_annotations_async(fake_path)


@pytest.mark.asyncio
async def test_read_annotations_async_corrupt_json_no_poisoning(caplog):
    fake_path = "/tmp/annotations.json"
    mock_aiofiles_open = make_mock_aiofiles_open(b"invalid-json-content")
    mock_stat = make_mock_stat(mtime_ns=100_000_000_000, size=20, ino=12345)

    with (
        patch("os.stat", return_value=mock_stat),
        patch("aiofiles.open", mock_aiofiles_open),
    ):
        result = await _read_annotations_async(fake_path)
        assert result == {}
        assert "Failed to parse annotations JSON" in caplog.text
        # CRITICAL: Verify corrupt content is NOT in cache (no poisoning)
        assert fake_path not in router.main._annotations_cache


@pytest.mark.asyncio
async def test_read_annotations_async_non_dict_json_no_poisoning(caplog):
    fake_path = "/tmp/annotations.json"
    mock_aiofiles_open = make_mock_aiofiles_open(b'["item1", "item2"]')
    mock_stat = make_mock_stat(mtime_ns=100_000_000_000, size=18, ino=12345)

    with (
        patch("os.stat", return_value=mock_stat),
        patch("aiofiles.open", mock_aiofiles_open),
    ):
        result = await _read_annotations_async(fake_path)
        assert result == {}
        assert "does not contain a JSON object" in caplog.text
        # CRITICAL: Verify non-dict content is NOT in cache (no poisoning)
        assert fake_path not in router.main._annotations_cache


@pytest.mark.asyncio
async def test_read_annotations_async_cache_hit_corrupted_eviction():
    fake_path = "/tmp/annotations.json"
    # Artificially insert corrupt bytes in cache hit position
    router.main._annotations_cache[fake_path] = {
        "mtime_ns": 100_000_000_000,
        "size": 10,
        "ino": 12345,
        "bytes": b"corrupt!",
    }

    mock_stat = make_mock_stat(mtime_ns=100_000_000_000, size=10, ino=12345)
    with patch("os.stat", return_value=mock_stat):
        result = await _read_annotations_async(fake_path)
        assert result == {}
        # Verify it gets evicted on parse failure
        assert fake_path not in router.main._annotations_cache


@pytest.mark.asyncio
async def test_read_annotations_async_cache_hit_non_dict_eviction():
    fake_path = "/tmp/annotations.json"
    # Artificially insert non-dict bytes in cache hit position
    router.main._annotations_cache[fake_path] = {
        "mtime_ns": 100_000_000_000,
        "size": 4,
        "ino": 12345,
        "bytes": b"null",
    }

    mock_stat = make_mock_stat(mtime_ns=100_000_000_000, size=4, ino=12345)
    with patch("os.stat", return_value=mock_stat):
        result = await _read_annotations_async(fake_path)
        assert result == {}
        # Verify it gets evicted
        assert fake_path not in router.main._annotations_cache


@pytest.mark.asyncio
async def test_read_annotations_async_str_content_handling():
    fake_path = "/tmp/annotations.json"
    # Mock returning str to test str -> bytes encoding branch
    mock_aiofiles_open = make_mock_aiofiles_open('{"annotation1": "data1"}')
    mock_stat = make_mock_stat(mtime_ns=100_000_000_000, size=25, ino=12345)

    with (
        patch("os.stat", return_value=mock_stat),
        patch("aiofiles.open", mock_aiofiles_open),
    ):
        result = await _read_annotations_async(fake_path)
        assert result == {"annotation1": "data1"}
        assert router.main._annotations_cache[fake_path]["bytes"] == b'{"annotation1": "data1"}'
