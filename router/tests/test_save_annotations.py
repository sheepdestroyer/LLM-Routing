import pytest
from unittest.mock import patch, AsyncMock
from fastapi.responses import JSONResponse
from fastapi import HTTPException
import json
from pathlib import Path

from router import main
from router.main import AnnotationPayload, AnnotationItem

@pytest.fixture(autouse=True)
def reset_globals():
    """Reset global variables related to annotations."""
    original_cache = dict(main._annotations_cache)
    main._annotations_cache.clear()

    yield

    main._annotations_cache.clear()
    main._annotations_cache.update(original_cache)

@pytest.mark.asyncio
@patch("router.main.DATA_DIR", new_callable=lambda: Path("/tmp"))
@patch("router.main._read_annotations_async", new_callable=AsyncMock)
@patch("router.main._atomic_write_json_async", new_callable=AsyncMock)
@patch("pathlib.Path.exists")
async def test_save_annotations_success(mock_exists, mock_write, mock_read, mock_data_dir):
    # Setup mocks
    mock_exists.return_value = True

    existing_data = {
        "123": {"tier": 1, "note": "old note", "ts": "123"}
    }
    mock_read.return_value = existing_data

    # Create payload
    item_data = {"tier": 2, "note": "new note", "ts": "456"}
    payload = AnnotationPayload(root={"123": AnnotationItem(**item_data), "h456": AnnotationItem(tier=3)})

    # Run function
    response = await main.save_annotations(payload)

    # Check assertions
    assert isinstance(response, JSONResponse)

    # Need to decode JSON content to verify it
    body = json.loads(response.body.decode('utf-8'))
    assert body["status"] == "ok"
    assert body["saved"] == 2

    mock_read.assert_awaited_once()
    mock_write.assert_awaited_once()

    # Check the merged data that was written
    written_data = mock_write.call_args[0][1]
    assert "123" in written_data
    assert "h456" in written_data
    assert written_data["123"]["tier"] == 2
    assert written_data["123"]["note"] == "new note"
    assert written_data["123"]["ts"] == "456"
    assert written_data["h456"]["tier"] == 3
    # For a new item, it dumps all fields so unset ones are None
    assert written_data["h456"]["note"] is None
    assert written_data["h456"]["ts"] is None

@pytest.mark.asyncio
@patch("router.main.DATA_DIR", new_callable=lambda: Path("/tmp"))
@patch("router.main._read_annotations_async", new_callable=AsyncMock)
@patch("router.main._atomic_write_json_async", new_callable=AsyncMock)
@patch("pathlib.Path.exists")
async def test_save_annotations_partial_update(mock_exists, mock_write, mock_read, mock_data_dir):
    # Setup mocks
    mock_exists.return_value = True

    existing_data = {
        "123": {"tier": 1, "note": "old note", "ts": "123"}
    }
    mock_read.return_value = existing_data

    # Create payload for a partial update - only tier is changed
    payload = AnnotationPayload(root={"123": AnnotationItem(tier=2)})

    # Run function
    response = await main.save_annotations(payload)

    # Check assertions
    assert isinstance(response, JSONResponse)
    mock_read.assert_awaited_once()
    mock_write.assert_awaited_once()

    written_data = mock_write.call_args[0][1]
    assert "123" in written_data
    assert written_data["123"]["tier"] == 2
    assert written_data["123"]["note"] == "old note" # Should be preserved
    assert written_data["123"]["ts"] == "123" # Should be preserved

@pytest.mark.asyncio
@patch("router.main.DATA_DIR", new_callable=lambda: Path("/tmp"))
@patch("router.main._read_annotations_async", new_callable=AsyncMock)
@patch("router.main._atomic_write_json_async", new_callable=AsyncMock)
@patch("pathlib.Path.exists")
async def test_save_annotations_no_existing(mock_exists, mock_write, mock_read, mock_data_dir):
    # Setup mocks - file doesn't exist yet
    mock_exists.return_value = False

    # Create payload
    payload = AnnotationPayload(root={"123": AnnotationItem(tier=1)})

    # Run function
    response = await main.save_annotations(payload)

    # Check assertions
    assert isinstance(response, JSONResponse)

    # Should not try to read if file doesn't exist
    mock_read.assert_not_called()
    mock_write.assert_awaited_once()

    # Check written data
    written_data = mock_write.call_args[0][1]
    assert "123" in written_data
    assert written_data["123"]["tier"] == 1

@pytest.mark.asyncio
@patch("router.main.DATA_DIR", new_callable=lambda: Path("/tmp"))
@patch("router.main._read_annotations_async", new_callable=AsyncMock)
@patch("router.main._atomic_write_json_async", new_callable=AsyncMock)
@patch("pathlib.Path.exists")
async def test_save_annotations_read_error(mock_exists, mock_write, mock_read, mock_data_dir):
    # Setup mocks
    mock_exists.return_value = True

    # Read throws an error - should log warning and overwrite
    mock_read.side_effect = Exception("Corrupted JSON")

    # Create payload
    payload = AnnotationPayload(root={"123": AnnotationItem(tier=1)})

    # Run function
    response = await main.save_annotations(payload)

    # Check assertions
    assert isinstance(response, JSONResponse)
    mock_read.assert_awaited_once()
    mock_write.assert_awaited_once()

    # Check written data (should just be the new data)
    written_data = mock_write.call_args[0][1]
    assert "123" in written_data
    assert len(written_data) == 1

@pytest.mark.asyncio
@patch("router.main.DATA_DIR", new_callable=lambda: Path("/tmp"))
@patch("pathlib.Path.exists")
async def test_save_annotations_exception(mock_exists, mock_data_dir):
    # Setup to throw an exception
    mock_exists.side_effect = Exception("Unexpected error")

    payload = AnnotationPayload(root={"123": AnnotationItem(tier=1)})

    # Run function and verify exception
    with pytest.raises(HTTPException) as exc_info:
        await main.save_annotations(payload)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Failed to save annotations"
