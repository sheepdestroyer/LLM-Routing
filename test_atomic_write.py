import asyncio
import json
import os
import pytest
from unittest.mock import patch

from router.main import _atomic_write_json_sync, _atomic_write_json_async

def test_atomic_write_json_sync_success(tmp_path):
    """Test successful synchronous atomic JSON write."""
    target_dir = tmp_path / "subdir"
    target_file = target_dir / "data.json"

    data = {"key": "value"}

    _atomic_write_json_sync(str(target_file), data)

    assert target_file.exists()
    assert target_dir.exists()

    with open(target_file, "r", encoding="utf-8") as f:
        loaded_data = json.load(f)

    assert loaded_data == data

@patch("router.main.os.replace")
def test_atomic_write_json_sync_replace_error(mock_replace, tmp_path):
    """Test error handling when os.replace fails."""
    target_file = tmp_path / "data.json"
    data = {"key": "value"}

    # Simulate os.replace failure
    mock_replace.side_effect = OSError("Mocked replace error")

    with pytest.raises(OSError, match="Mocked replace error"):
        _atomic_write_json_sync(str(target_file), data)

    # Verify the target file was not created (or overwritten)
    assert not target_file.exists()

    # Verify temp file was cleaned up. tmp_path should be empty
    # because the target wasn't written, and the tmp file was unlinked.
    assert list(tmp_path.iterdir()) == []

def test_atomic_write_json_sync_dump_error(tmp_path):
    """Test error handling when json.dump fails."""
    target_file = tmp_path / "data.json"

    # Object that cannot be serialized to JSON
    class Unserializable:
        pass

    data = {"key": Unserializable()}

    with pytest.raises(TypeError):
        _atomic_write_json_sync(str(target_file), data)

    assert not target_file.exists()
    assert list(tmp_path.iterdir()) == []

@patch("router.main.os.fdopen")
def test_atomic_write_json_sync_fdopen_error(mock_fdopen, tmp_path):
    """Test error handling when os.fdopen fails."""
    target_file = tmp_path / "data.json"
    data = {"key": "value"}

    mock_fdopen.side_effect = OSError("Mocked fdopen error")

    with pytest.raises(OSError, match="Mocked fdopen error"):
        _atomic_write_json_sync(str(target_file), data)

    assert not target_file.exists()
    assert list(tmp_path.iterdir()) == []

@pytest.mark.anyio
async def test_atomic_write_json_async_success(tmp_path):
    """Test successful asynchronous atomic JSON write."""
    target_file = tmp_path / "data.json"
    data = {"key": "value"}

    await _atomic_write_json_async(str(target_file), data)

    assert target_file.exists()

    with open(target_file, "r", encoding="utf-8") as f:
        loaded_data = json.load(f)

    assert loaded_data == data

def test_atomic_write_json_sync_overwrite_success(tmp_path):
    """Test that atomic write successfully overwrites an existing file."""
    target_file = tmp_path / "data.json"
    old_data = {"old": "data"}
    new_data = {"new": "data"}

    # Write initial data
    _atomic_write_json_sync(str(target_file), old_data)
    assert target_file.exists()

    # Overwrite with new data
    _atomic_write_json_sync(str(target_file), new_data)

    with open(target_file, "r", encoding="utf-8") as f:
        loaded_data = json.load(f)
    assert loaded_data == new_data

def test_atomic_write_json_sync_overwrite_failure_keeps_original(tmp_path):
    """Test that if replace fails during overwrite, the existing target file remains intact."""
    target_file = tmp_path / "data.json"
    old_data = {"old": "data"}
    new_data = {"new": "data"}

    # Write initial data
    _atomic_write_json_sync(str(target_file), old_data)
    assert target_file.exists()

    # Simulate replace failure during overwrite
    with patch("router.main.os.replace") as mock_replace:
        mock_replace.side_effect = OSError("Mocked replace error")

        with pytest.raises(OSError, match="Mocked replace error"):
            _atomic_write_json_sync(str(target_file), new_data)

    # Verify the original file was NOT deleted or modified
    with open(target_file, "r", encoding="utf-8") as f:
        loaded_data = json.load(f)
    assert loaded_data == old_data

def test_atomic_write_json_sync_overwrite_dump_failure_keeps_original(tmp_path):
    """Test that if dump fails during overwrite, the existing target file remains intact."""
    target_file = tmp_path / "data.json"
    old_data = {"old": "data"}

    # Write initial data
    _atomic_write_json_sync(str(target_file), old_data)
    assert target_file.exists()

    # Object that cannot be serialized to JSON
    class Unserializable:
        pass

    with pytest.raises(TypeError):
        _atomic_write_json_sync(str(target_file), {"new": Unserializable()})

    # Verify the original file was NOT deleted or modified
    with open(target_file, "r", encoding="utf-8") as f:
        loaded_data = json.load(f)
    assert loaded_data == old_data

@pytest.mark.anyio
async def test_atomic_write_json_async_overwrite_success(tmp_path):
    """Test that async atomic write successfully overwrites an existing file."""
    target_file = tmp_path / "data.json"
    old_data = {"old": "data"}
    new_data = {"new": "data"}

    # Write initial data
    await _atomic_write_json_async(str(target_file), old_data)
    assert target_file.exists()

    # Overwrite with new data
    await _atomic_write_json_async(str(target_file), new_data)

    with open(target_file, "r", encoding="utf-8") as f:
        loaded_data = json.load(f)
    assert loaded_data == new_data

@pytest.mark.anyio
async def test_atomic_write_json_async_overwrite_failure_keeps_original(tmp_path):
    """Test that if replace fails during async write, the existing target file remains intact."""
    target_file = tmp_path / "data.json"
    old_data = {"old": "data"}
    new_data = {"new": "data"}

    # Write initial data
    await _atomic_write_json_async(str(target_file), old_data)
    assert target_file.exists()

    # Simulate replace failure during overwrite
    with patch("router.main.os.replace") as mock_replace:
        mock_replace.side_effect = OSError("Mocked replace error")

        with pytest.raises(OSError, match="Mocked replace error"):
            await _atomic_write_json_async(str(target_file), new_data)

    # Verify the original file was NOT deleted or modified
    with open(target_file, "r", encoding="utf-8") as f:
        loaded_data = json.load(f)
    assert loaded_data == old_data

@pytest.mark.anyio
async def test_atomic_write_json_async_overwrite_dump_failure_keeps_original(tmp_path):
    """Test that if dump fails during async overwrite, the existing target file remains intact."""
    target_file = tmp_path / "data.json"
    old_data = {"old": "data"}

    # Write initial data
    await _atomic_write_json_async(str(target_file), old_data)
    assert target_file.exists()

    # Object that cannot be serialized to JSON
    class Unserializable:
        pass

    with pytest.raises(TypeError):
        await _atomic_write_json_async(str(target_file), {"new": Unserializable()})

    # Verify the original file was NOT deleted or modified
    with open(target_file, "r", encoding="utf-8") as f:
        loaded_data = json.load(f)
    assert loaded_data == old_data
<<<<<<< HEAD
<<<<<<< HEAD
<<<<<<< HEAD
<<<<<<< HEAD
=======


>>>>>>> origin/master
=======


>>>>>>> origin/master
=======


>>>>>>> origin/master
=======


>>>>>>> origin/master
