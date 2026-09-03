from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from router.model_sync import ModelInfoFetchError, ModelRegistrySync


@pytest.fixture
def mock_client():
    return AsyncMock()


@pytest.fixture
def sync_engine(mock_client):
    return ModelRegistrySync(
        litellm_url="http://test-litellm:4000",
        master_key="sk-test-key",
        agy_daemon_url="http://test-agy:5005",
        llama_server_url="http://test-llama:8083",
        whisper_server_url="http://test-whisper:8084",
        classifier_url="http://test-classifier:8086",
        client=mock_client,
    )


@pytest.mark.anyio
async def test_aenter_with_external_client(mock_client):
    sync = ModelRegistrySync(
        litellm_url="http://test",
        master_key="sk",
        agy_daemon_url="http://test",
        llama_server_url="http://test",
        whisper_server_url="http://test",
        classifier_url="http://test",
        client=mock_client,
    )
    async with sync as s:
        assert s._owned_client is None


@pytest.mark.anyio
async def test_aenter_without_external_client():
    sync = ModelRegistrySync(
        litellm_url="http://test",
        master_key="sk",
        agy_daemon_url="http://test",
        llama_server_url="http://test",
        whisper_server_url="http://test",
        classifier_url="http://test",
    )
    async with sync as s:
        assert s._owned_client is not None
    assert sync._owned_client is None


@pytest.mark.anyio
async def test_aclose_no_owned_client(sync_engine):
    assert sync_engine._owned_client is None
    await sync_engine.aclose()


@pytest.mark.anyio
async def test_aclose_exception():
    sync = ModelRegistrySync(
        litellm_url="http://test",
        master_key="sk",
        agy_daemon_url="http://test",
        llama_server_url="http://test",
        whisper_server_url="http://test",
        classifier_url="http://test",
    )
    mock_owned = AsyncMock()
    mock_owned.aclose.side_effect = RuntimeError("Failed to close")
    sync._owned_client = mock_owned
    await sync.aclose()
    assert sync._owned_client is None


@pytest.mark.anyio
async def test_get_client_creates_and_reuses_owned_client():
    sync = ModelRegistrySync(
        litellm_url="http://test",
        master_key="sk",
        agy_daemon_url="http://test",
        llama_server_url="http://test",
        whisper_server_url="http://test",
        classifier_url="http://test",
    )
    c1 = await sync._get_client()
    assert c1 is not None
    assert sync._owned_client is c1
    c2 = await sync._get_client()
    assert c2 is c1
    await sync.aclose()


@pytest.mark.anyio
async def test_get_existing_models_skips_empty_name(sync_engine, mock_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {"model_name": None, "id": "none_name"},
            {"model_name": "", "id": "empty_name"},
            {"model_name": "valid_model", "id": "valid_id"},
        ]
    }
    mock_client.get.return_value = mock_resp
    result = await sync_engine.get_existing_models()
    assert "valid_model" in result
    assert None not in result
    assert "" not in result
    assert len(result) == 1


@pytest.mark.anyio
async def test_prune_duplicates_skips_missing_id_or_non_db(sync_engine, mock_client):
    grouped = {
        "test-model": [
            {"model_info": {"db_model": True}},
            {"model_info": {"id": "dep-2", "db_model": False}},
            {"model_info": {"id": "dep-keeper", "db_model": True}},
        ]
    }
    pruned = await sync_engine.prune_duplicates(grouped)
    assert pruned == 0
    mock_client.post.assert_not_called()


@pytest.mark.anyio
async def test_remove_stale_models_custom_names(sync_engine, mock_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client.post.return_value = mock_resp

    grouped = {
        "custom-stale": [
            {"model_info": {"id": "stale-1", "db_model": True}},
        ]
    }
    removed = await sync_engine.remove_stale_models(grouped, stale_names=["custom-stale"])
    assert removed == 1
    mock_client.post.assert_called_once()


@pytest.mark.anyio
async def test_remove_stale_models_skips_missing_id_or_non_db(sync_engine, mock_client):
    grouped = {
        "stale-no-id": [
            {"model_info": {"db_model": True}},
            {"model_info": {"id": "stale-non-db", "db_model": False}},
        ]
    }
    removed = await sync_engine.remove_stale_models(grouped, stale_names=["stale-no-id"])
    assert removed == 0
    mock_client.post.assert_not_called()


@pytest.mark.anyio
async def test_remove_stale_models_delete_fails(sync_engine, mock_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_client.post.return_value = mock_resp

    grouped = {
        "stale-model": [
            {"model_info": {"id": "stale-id", "db_model": True}},
        ]
    }
    removed = await sync_engine.remove_stale_models(grouped, stale_names=["stale-model"])
    assert removed == 0


@pytest.mark.anyio
async def test_discover_agy_latest_flash_no_matching_versions(sync_engine, mock_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"models": [{"id": "gemini-pro"}, {"id": "claude-3-opus"}]}
    mock_client.get.return_value = mock_resp

    res = await sync_engine.discover_agy_latest_flash()
    assert res == "gemini-3.8-flash"


@pytest.mark.anyio
async def test_upsert_model_existing_non_db_or_no_id(sync_engine):
    target = {"model_name": "existing-model", "litellm_params": {}, "model_info": {}}

    # Case 1: no model_id
    existing_no_id = {"existing-model": [{"model_info": {"db_model": True}}]}
    action, success = await sync_engine.upsert_model(target, existing_no_id)
    assert action == "unchanged"
    assert success is False

    # Case 2: db_model is False
    existing_non_db = {"existing-model": [{"model_info": {"id": "model-1", "db_model": False}}]}
    action, success = await sync_engine.upsert_model(target, existing_non_db)
    assert action == "unchanged"
    assert success is False


@pytest.mark.anyio
async def test_sync_all_models_refresh_prune_fetch_error(sync_engine):
    initial = {"m": [{"model_info": {"id": "1", "db_model": True}}]}
    calls = 0

    async def fake_get_existing():
        nonlocal calls
        calls += 1
        if calls == 1:
            return initial
        if calls == 2:
            raise ModelInfoFetchError("prune refresh fail")
        return initial

    with (
        patch.object(sync_engine, "get_existing_models", side_effect=fake_get_existing),
        patch.object(sync_engine, "discover_agy_latest_flash", return_value="gemini-3.8-flash"),
        patch.object(sync_engine, "prune_duplicates", return_value=0),
        patch.object(sync_engine, "remove_stale_models", return_value=0),
        patch.object(sync_engine, "upsert_model", return_value=("unchanged", False)),
    ):
        res = await sync_engine.sync_all_models()
        assert res["failed"] == 0


@pytest.mark.anyio
async def test_sync_all_models_refresh_stale_fetch_error(sync_engine):
    initial = {"m": [{"model_info": {"id": "1", "db_model": True}}]}
    calls = 0

    async def fake_get_existing():
        nonlocal calls
        calls += 1
        if calls == 1:
            return initial
        if calls == 2:
            return initial
        if calls == 3:
            raise ModelInfoFetchError("stale refresh fail")
        return initial

    with (
        patch.object(sync_engine, "get_existing_models", side_effect=fake_get_existing),
        patch.object(sync_engine, "discover_agy_latest_flash", return_value="gemini-3.8-flash"),
        patch.object(sync_engine, "prune_duplicates", return_value=0),
        patch.object(sync_engine, "remove_stale_models", return_value=0),
        patch.object(sync_engine, "upsert_model", return_value=("unchanged", False)),
    ):
        res = await sync_engine.sync_all_models()
        assert res["failed"] == 0


@pytest.mark.anyio
async def test_sync_all_models_unknown_actions(sync_engine):
    initial = {"m": [{"model_info": {"id": "1", "db_model": True}}]}

    # Test action not in results with success=False -> failed incremented
    with (
        patch.object(sync_engine, "get_existing_models", return_value=initial),
        patch.object(sync_engine, "discover_agy_latest_flash", return_value="gemini-3.8-flash"),
        patch.object(sync_engine, "prune_duplicates", return_value=0),
        patch.object(sync_engine, "remove_stale_models", return_value=0),
        patch.object(sync_engine, "upsert_model", return_value=("unknown_action", False)),
    ):
        res = await sync_engine.sync_all_models()
        assert res["failed"] > 0

    # Test action not in results with success=True -> nothing incremented
    with (
        patch.object(sync_engine, "get_existing_models", return_value=initial),
        patch.object(sync_engine, "discover_agy_latest_flash", return_value="gemini-3.8-flash"),
        patch.object(sync_engine, "prune_duplicates", return_value=0),
        patch.object(sync_engine, "remove_stale_models", return_value=0),
        patch.object(sync_engine, "upsert_model", return_value=("unknown_action", True)),
    ):
        res = await sync_engine.sync_all_models()
        assert res["failed"] == 0
