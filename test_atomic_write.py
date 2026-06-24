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

@pytest.mark.asyncio
async def test_atomic_write_json_async_success(tmp_path):
    """Test successful asynchronous atomic JSON write."""
    target_file = tmp_path / "data.json"
    data = {"key": "value"}

    await _atomic_write_json_async(str(target_file), data)

    assert target_file.exists()

    with open(target_file, "r", encoding="utf-8") as f:
        loaded_data = json.load(f)

    assert loaded_data == data
