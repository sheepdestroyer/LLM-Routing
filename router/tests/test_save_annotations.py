import json
import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.testclient import TestClient

from router import main
from router.main import AnnotationItem, AnnotationPayload, app


def make_mock_request(auth_header: str | None = "Bearer test-key") -> Request:
    mock_req = MagicMock(spec=Request)
    headers = {}
    if auth_header is not None:
        headers["Authorization"] = auth_header
    mock_req.headers = headers
    mock_req.state = MagicMock()
    return mock_req


@pytest.fixture(autouse=True)
def reset_globals():
    """Reset global variables related to annotations."""
    original_cache = main._annotations_cache.copy()
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

    existing_data = {"123": {"tier": 1, "note": "old note", "ts": "123"}}
    mock_read.return_value = existing_data

    # Create payload
    item_data = {"tier": 2, "note": "new note", "ts": "456"}
    payload = AnnotationPayload(root={"123": AnnotationItem(**item_data), "h456": AnnotationItem(tier=3)})

    # Run function with authenticated request
    req = make_mock_request("Bearer test-key")
    response = await main.save_annotations(payload, req)

    # Check assertions
    assert isinstance(response, JSONResponse)

    # Need to decode JSON content to verify it
    body = json.loads(response.body.decode("utf-8"))
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

    existing_data = {"123": {"tier": 1, "note": "old note", "ts": "123"}}
    mock_read.return_value = existing_data

    # Create payload for a partial update - only tier is changed
    payload = AnnotationPayload(root={"123": AnnotationItem(tier=2)})

    # Run function
    req = make_mock_request("Bearer test-key")
    response = await main.save_annotations(payload, req)

    # Check assertions
    assert isinstance(response, JSONResponse)
    mock_read.assert_awaited_once()
    mock_write.assert_awaited_once()

    written_data = mock_write.call_args[0][1]
    assert "123" in written_data
    assert written_data["123"]["tier"] == 2
    assert written_data["123"]["note"] == "old note"  # Should be preserved
    assert written_data["123"]["ts"] == "123"  # Should be preserved


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
    req = make_mock_request("Bearer test-key")
    response = await main.save_annotations(payload, req)

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
    req = make_mock_request("Bearer test-key")
    response = await main.save_annotations(payload, req)

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
    req = make_mock_request("Bearer test-key")
    with pytest.raises(HTTPException) as exc_info:
        await main.save_annotations(payload, req)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Failed to save annotations"


@pytest.mark.asyncio
async def test_save_annotations_unit_missing_auth():
    payload = AnnotationPayload(root={"123": AnnotationItem(tier=1)})
    req = make_mock_request(auth_header=None)
    with pytest.raises(HTTPException) as exc_info:
        await main.save_annotations(payload, req)
    assert exc_info.value.status_code == 401
    assert "Authorization header" in exc_info.value.detail


@pytest.mark.asyncio
async def test_save_annotations_unit_invalid_bearer_token():
    payload = AnnotationPayload(root={"123": AnnotationItem(tier=1)})
    req = make_mock_request(auth_header="Bearer totally-invalid-token")
    with pytest.raises(HTTPException) as exc_info:
        await main.save_annotations(payload, req)
    assert exc_info.value.status_code == 401
    assert "Invalid Authorization token" in exc_info.value.detail


def test_save_annotations_integration_missing_auth(tmp_path):
    client = TestClient(app)
    with patch("router.main.DATA_DIR", tmp_path):
        resp = client.post("/dashboard/save-annotations", json={"123": {"tier": 1}})
        assert resp.status_code == 401
        assert "Authorization header" in resp.json()["detail"]


def test_save_annotations_integration_invalid_bearer(tmp_path):
    client = TestClient(app)
    with patch("router.main.DATA_DIR", tmp_path):
        resp = client.post(
            "/dashboard/save-annotations",
            json={"123": {"tier": 1}},
            headers={"Authorization": "Bearer non-existent-token-abc"},
        )
        assert resp.status_code == 401
        assert "Invalid Authorization token" in resp.json()["detail"]


def test_save_annotations_integration_valid_router_api_key(tmp_path):
    client = TestClient(app)
    secret_key = "test-custom-router-api-key-99"
    with (
        patch.dict(os.environ, {"ROUTER_API_KEY": secret_key}),
        patch("router.main.DATA_DIR", tmp_path),
    ):
        resp = client.post(
            "/dashboard/save-annotations",
            json={"123": {"tier": 2, "note": "verified", "ts": "2026-09-11T00:00:00Z"}},
            headers={"Authorization": f"Bearer {secret_key}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["saved"] == 1

        saved_file = tmp_path / "annotations.json"
        assert saved_file.exists()
        saved_content = json.loads(saved_file.read_text(encoding="utf-8"))
        assert "123" in saved_content
        assert saved_content["123"]["tier"] == 2
        assert saved_content["123"]["note"] == "verified"


def test_save_annotations_integration_valid_litellm_master_key(tmp_path):
    client = TestClient(app)
    master_key = "test-custom-litellm-master-key-77"
    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": master_key}),
        patch("router.main.DATA_DIR", tmp_path),
    ):
        resp = client.post(
            "/dashboard/save-annotations",
            json={"h12345abc": {"tier": 1, "note": "master key save"}},
            headers={"Authorization": f"Bearer {master_key}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["saved"] == 1

        saved_file = tmp_path / "annotations.json"
        assert saved_file.exists()
        saved_content = json.loads(saved_file.read_text(encoding="utf-8"))
        assert "h12345abc" in saved_content
        assert saved_content["h12345abc"]["tier"] == 1
