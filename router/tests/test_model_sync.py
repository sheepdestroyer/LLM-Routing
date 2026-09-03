import pytest
from unittest.mock import AsyncMock, MagicMock
from router.model_sync import ModelRegistrySync


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


@pytest.mark.asyncio
async def test_get_existing_models_success(sync_engine, mock_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {"model_name": "locallama-qwen", "model_info": {"id": "id-1"}},
            {"model_name": "locallama-qwen", "model_info": {"id": "id-2"}},
            {"model_name": "agy-gemini", "model_info": {"id": "id-3"}},
        ]
    }
    mock_client.get.return_value = mock_resp

    res = await sync_engine.get_existing_models()
    assert len(res["locallama-qwen"]) == 2
    assert len(res["agy-gemini"]) == 1


@pytest.mark.asyncio
async def test_get_existing_models_failure(sync_engine, mock_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_client.get.return_value = mock_resp

    res = await sync_engine.get_existing_models()
    assert res == {}


@pytest.mark.asyncio
async def test_prune_duplicates(sync_engine, mock_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client.post.return_value = mock_resp

    grouped = {
        "locallama-qwen": [
            {"model_name": "locallama-qwen", "model_info": {"id": "dup-1", "db_model": True}},
            {"model_name": "locallama-qwen", "model_info": {"id": "dup-2", "db_model": True}},
            {"model_name": "locallama-qwen", "model_info": {"id": "keep-3", "db_model": True}},
        ],
        "single-model": [
            {"model_name": "single-model", "model_info": {"id": "keep-1", "db_model": True}},
        ]
    }

    pruned = await sync_engine.prune_duplicates(grouped)
    assert pruned == 2
    assert mock_client.post.call_count == 2
    deleted_ids = [call.kwargs["json"]["id"] for call in mock_client.post.call_args_list]
    assert deleted_ids == ["dup-1", "dup-2"]


@pytest.mark.asyncio
async def test_remove_stale_models(sync_engine, mock_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client.post.return_value = mock_resp

    grouped = {
        "ollama/GPT-5.6 Luna (max)": [
            {"model_name": "ollama/GPT-5.6 Luna (max)", "model_info": {"id": "stale-1", "db_model": True}},
        ],
        "locallama-qwen": [
            {"model_name": "locallama-qwen", "model_info": {"id": "keep-1", "db_model": True}},
        ]
    }

    removed = await sync_engine.remove_stale_models(grouped)
    assert removed == 1
    assert mock_client.post.call_count == 1
    assert mock_client.post.call_args.kwargs["json"]["id"] == "stale-1"


@pytest.mark.asyncio
async def test_discover_agy_latest_flash(sync_engine, mock_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "ok",
        "models": [
            {"id": "gemini-3.6-flash-low"},
            {"id": "gemini-3.7-flash-medium"},
            {"id": "gemini-3.8-flash-high"},
            {"id": "claude-opus-4-6-thinking"},
        ]
    }
    mock_client.get.return_value = mock_resp

    latest = await sync_engine.discover_agy_latest_flash()
    assert latest == "gemini-3.8-flash"


@pytest.mark.asyncio
async def test_discover_agy_latest_flash_fallback(sync_engine, mock_client):
    mock_client.get.side_effect = Exception("Connection error")
    latest = await sync_engine.discover_agy_latest_flash()
    assert latest == "gemini-3.8-flash"


def test_build_model_suites(sync_engine):
    locallama = sync_engine.build_locallama_models()
    assert any(m["model_name"] == "locallama-qwen" for m in locallama)
    assert any(m["model_name"] == "locallama-whisper" for m in locallama)

    agy = sync_engine.build_agy_models(latest_flash="gemini-3.8-flash")
    assert any(m["model_name"] == "agy-gemini" for m in agy)
    assert any(m["model_name"] == "agy-gemini-sse" for m in agy)
    assert any(m["model_name"] == "agy-opus" for m in agy)
    assert any(m["model_name"] == "agy-gptoss" for m in agy)

    ollama = sync_engine.build_ollama_models()
    assert any(m["model_name"] == "ollama-deepseek-v4-pro" for m in ollama)
    assert any(m["model_name"] == "ollama-gpt-5.6-luna" for m in ollama)

    openrouter = sync_engine.build_openrouter_models()
    assert any(m["model_name"] == "openrouter-auto" for m in openrouter)
    assert any(m["model_name"] == "openrouter-tts" for m in openrouter)

    aliases = sync_engine.build_legacy_aliases(latest_flash="gemini-3.8-flash")
    assert any(m["model_name"] == "local-qwen" for m in aliases)
    assert any(m["model_name"] == "whisper-1" for m in aliases)
    assert any(m["model_name"] == "llm-routing-agy" for m in aliases)


@pytest.mark.asyncio
async def test_upsert_model_create(sync_engine, mock_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client.post.return_value = mock_resp

    target = {
        "model_name": "agy-gemini",
        "litellm_params": {"model": "openai/gemini-3.8-flash", "api_base": "http://127.0.0.1:5005/v1"},
    }
    action, ok = await sync_engine.upsert_model(target, existing_grouped={})
    assert action == "created"
    assert ok is True
    assert mock_client.post.call_count == 1
    assert "/model/new" in str(mock_client.post.call_args)


@pytest.mark.asyncio
async def test_upsert_model_update_on_drift(sync_engine, mock_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client.post.return_value = mock_resp

    existing = {
        "agy-gemini": [{
            "model_name": "agy-gemini",
            "litellm_params": {"model": "openai/gemini-3.7-flash", "api_base": "http://127.0.0.1:5005/v1"},
            "model_info": {"id": "mod-123", "db_model": True},
        }]
    }
    target = {
        "model_name": "agy-gemini",
        "litellm_params": {"model": "openai/gemini-3.8-flash", "api_base": "http://127.0.0.1:5005/v1"},
    }
    action, ok = await sync_engine.upsert_model(target, existing_grouped=existing)
    assert action == "updated"
    assert ok is True
    assert mock_client.post.call_count == 1
    assert "/model/update" in str(mock_client.post.call_args)


@pytest.mark.asyncio
async def test_upsert_model_unchanged(sync_engine, mock_client):
    existing = {
        "agy-gemini": [{
            "model_name": "agy-gemini",
            "litellm_params": {"model": "openai/gemini-3.8-flash", "api_base": "http://127.0.0.1:5005/v1"},
            "model_info": {"id": "mod-123", "db_model": True},
        }]
    }
    target = {
        "model_name": "agy-gemini",
        "litellm_params": {"model": "openai/gemini-3.8-flash", "api_base": "http://127.0.0.1:5005/v1"},
    }
    action, ok = await sync_engine.upsert_model(target, existing_grouped=existing)
    assert action == "unchanged"
    assert ok is False
    assert mock_client.post.call_count == 0


@pytest.mark.asyncio
async def test_sync_all_models(sync_engine, mock_client):
    # Setup mock responses for get_existing_models (returns duplicate),
    # discover_agy_latest_flash, and upsert calls
    info_resp_1 = MagicMock(status_code=200)
    info_resp_1.json.return_value = {
        "data": [
            {"model_name": "locallama-qwen", "model_info": {"id": "d1", "db_model": True}, "litellm_params": {"model": "openai/local-qwen"}},
            {"model_name": "locallama-qwen", "model_info": {"id": "d2", "db_model": True}, "litellm_params": {"model": "openai/local-qwen"}},
            {"model_name": "ollama/GPT-5.6 Luna (max)", "model_info": {"id": "stale-1", "db_model": True}},
        ]
    }
    info_resp_2 = MagicMock(status_code=200)
    info_resp_2.json.return_value = {"data": [
        {"model_name": "locallama-qwen", "model_info": {"id": "d2", "db_model": True}, "litellm_params": {"model": "openai/local-qwen"}},
        {"model_name": "ollama/GPT-5.6 Luna (max)", "model_info": {"id": "stale-1", "db_model": True}},
    ]}
    info_resp_3 = MagicMock(status_code=200)
    info_resp_3.json.return_value = {"data": [
        {"model_name": "locallama-qwen", "model_info": {"id": "d2", "db_model": True}, "litellm_params": {"model": "openai/local-qwen"}},
    ]}

    agy_resp = MagicMock(status_code=200)
    agy_resp.json.return_value = {"models": [{"id": "gemini-3.8-flash-high"}]}

    mock_client.get.side_effect = [info_resp_1, info_resp_2, info_resp_3, agy_resp]
    mock_client.post.return_value = MagicMock(status_code=200)

    stats = await sync_engine.sync_all_models()
    assert stats["pruned_duplicates"] == 1
    assert stats["removed_stale"] == 1
    assert stats["created"] > 0


@pytest.mark.asyncio
async def test_admin_sync_models_endpoint():
    import os
    from starlette.testclient import TestClient
    from router.main import app
    from unittest.mock import patch

    client = TestClient(app)
    mock_sync = AsyncMock(return_value={"pruned_duplicates": 2, "created": 5})

    with patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-key"}), \
         patch("router.main._authenticate_client_request", new_callable=AsyncMock), \
         patch.object(ModelRegistrySync, "sync_all_models", mock_sync):
        resp = client.post("/admin/sync-models", headers={"Authorization": "Bearer test-key"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["results"]["pruned_duplicates"] == 2


@pytest.mark.asyncio
async def test_periodic_model_sync():
    import os
    import asyncio
    from router.main import _periodic_model_sync
    from unittest.mock import patch

    mock_sync = AsyncMock(return_value={"pruned_duplicates": 0})
    with patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-key"}), \
         patch.object(ModelRegistrySync, "sync_all_models", mock_sync), \
         patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError()]):
        try:
            await _periodic_model_sync()
        except asyncio.CancelledError:
            pass
        assert mock_sync.call_count == 1
