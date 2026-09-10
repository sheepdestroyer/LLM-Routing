import asyncio
import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.main import (
    _register_ollama_models_in_db,
    _register_openrouter_models_in_db,
    sync_adaptive_router_roster,
)


@pytest.fixture
def mock_env():
    with patch.dict(
        os.environ,
        {"DATABASE_URL": "postgresql://test:test@localhost:5432/test", "ROUTER_API_KEY": "test_api_key"},
        clear=False,
    ):
        yield


@pytest.mark.asyncio
async def test_register_openrouter_models_concurrency_and_semaphore(mock_env):
    """Verify that OpenRouter model registration runs concurrently and respects the semaphore limit of 10."""
    num_models = 25
    mock_config = {
        "model_list": [
            {
                "model_name": f"openrouter-model-{i}",
                "litellm_params": {"model": f"openrouter/provider/model-{i}"},
            }
            for i in range(num_models)
        ]
    }

    mock_client = AsyncMock()
    current_active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def mock_post(url, **kwargs):
        nonlocal current_active, max_active
        async with lock:
            current_active += 1
            if current_active > max_active:
                max_active = current_active
        await asyncio.sleep(0.01)
        async with lock:
            current_active -= 1
        resp = MagicMock()
        resp.status_code = 200
        return resp

    mock_client.post.side_effect = mock_post

    with (
        patch("router.main.get_http_client", return_value=mock_client),
        patch("router.main.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread,
    ):
        mock_to_thread.return_value = mock_config
        await _register_openrouter_models_in_db("test_master_key")

    assert mock_client.post.call_count == num_models
    assert 1 < max_active <= 10


@pytest.mark.asyncio
async def test_register_ollama_models_concurrency_and_semaphore(mock_env):
    """Verify that Ollama model registration runs concurrently and respects the semaphore limit of 10."""
    num_models = 25
    mock_config = {
        "model_list": [
            {
                "model_name": f"ollama-model-{i}",
                "litellm_params": {"model": f"ollama_chat/model-{i}"},
            }
            for i in range(num_models)
        ]
    }

    mock_client = AsyncMock()
    current_active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def mock_post(url, **kwargs):
        nonlocal current_active, max_active
        async with lock:
            current_active += 1
            if current_active > max_active:
                max_active = current_active
        await asyncio.sleep(0.01)
        async with lock:
            current_active -= 1
        resp = MagicMock()
        resp.status_code = 200
        return resp

    mock_client.post.side_effect = mock_post

    with (
        patch("router.main.get_http_client", return_value=mock_client),
        patch("router.main.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread,
    ):
        mock_to_thread.return_value = mock_config
        await _register_ollama_models_in_db("test_master_key")

    assert mock_client.post.call_count == num_models
    assert 1 < max_active <= 10


@pytest.mark.asyncio
async def test_sync_adaptive_router_roster_concurrency_and_semaphore():
    """Verify that sync_adaptive_router_roster registers tier models concurrently respecting semaphore."""
    num_models = 15
    mock_openrouter_response = MagicMock()
    mock_openrouter_response.status_code = 200
    mock_openrouter_response.json.return_value = {
        "data": [
            {
                "id": f"model-{i}",
                "supported_parameters": ["tools"],
                "pricing": {"prompt": "0", "completion": "0"},
                "context_length": 4096,
            }
            for i in range(num_models)
        ]
    }

    current_active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def mock_post(url, **kwargs):
        nonlocal current_active, max_active
        async with lock:
            current_active += 1
            if current_active > max_active:
                max_active = current_active
        await asyncio.sleep(0.01)
        async with lock:
            current_active -= 1
        resp = MagicMock()
        resp.status_code = 200
        return resp

    mock_client_instance = AsyncMock()
    mock_client_instance.get.return_value = mock_openrouter_response
    mock_client_instance.post.side_effect = mock_post

    scores = [85.0 - i for i in range(num_models)]
    with (
        patch("router.main.get_http_client", return_value=mock_client_instance),
        patch("router.main.compute_free_model_score", side_effect=scores),
        patch("router.main._load_aa_scores"),
        patch("router.main._purge_stale_deployments", new_callable=AsyncMock),
        patch.dict(os.environ, {"DATABASE_URL": "postgresql://test:test@localhost:5432/testdb"}),
        patch("router.main._registered_free_models", {}),
    ):
        await sync_adaptive_router_roster("test_key")

        import router.main as r_main

        assert mock_client_instance.post.call_count > 0
        assert 1 < max_active <= 10
        # Verify atomic swap populated the roster with registered models across tiers
        assert any(len(models) > 0 for models in r_main._registered_free_models.values())


@pytest.mark.asyncio
async def test_register_models_individual_failures_handled_gracefully(mock_env, caplog):
    """Verify that individual model failures/exceptions do not abort the batch or raise exceptions."""
    caplog.set_level(logging.INFO)
    mock_config = {
        "model_list": [
            {"model_name": "openrouter-ok", "litellm_params": {"model": "openrouter/ok"}},
            {"model_name": "openrouter-fail-http", "litellm_params": {"model": "openrouter/fail"}},
            {"model_name": "openrouter-fail-exc", "litellm_params": {"model": "openrouter/exc"}},
        ]
    }

    resp_200 = MagicMock(status_code=200)
    resp_500 = MagicMock(status_code=500, text="Internal Server Error")

    async def mock_post(url, **kwargs):
        payload = kwargs.get("json", {})
        name = payload.get("model_name")
        if name == "openrouter-ok":
            return resp_200
        elif name == "openrouter-fail-http":
            return resp_500
        else:
            raise RuntimeError("Simulated network timeout")

    mock_client = AsyncMock()
    mock_client.post.side_effect = mock_post

    with (
        patch("router.main.get_http_client", return_value=mock_client),
        patch("router.main.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread,
    ):
        mock_to_thread.return_value = mock_config
        # Should complete without error
        await _register_openrouter_models_in_db("test_master_key")

    assert mock_client.post.call_count == 3
    assert "OpenRouter DB registration: 1 registered, 2 failed" in caplog.text
    assert "HTTP 500" in caplog.text
    assert "Simulated network timeout" in caplog.text


@pytest.mark.asyncio
async def test_register_ollama_models_individual_failures_handled_gracefully(mock_env, caplog):
    """Verify that ollama registration handles HTTP errors, network exceptions, and missing model_name."""
    caplog.set_level(logging.INFO)
    mock_config = {
        "model_list": [
            {"model_name": "ollama-ok", "litellm_params": {"model": "ollama_chat/ok"}},
            {"model_name": "ollama-fail-http", "litellm_params": {"model": "ollama_chat/fail"}},
            {"model_name": "ollama-fail-exc", "litellm_params": {"model": "ollama_chat/exc"}},
            {"litellm_params": {"model": "ollama_chat/no-name"}},  # Missing model_name key
        ]
    }

    resp_200 = MagicMock(status_code=200)
    resp_500 = MagicMock(status_code=500, text="Internal Server Error")

    async def mock_post(url, **kwargs):
        payload = kwargs.get("json", {})
        name = payload.get("model_name")
        if name == "ollama-ok":
            return resp_200
        elif name == "ollama-fail-http":
            return resp_500
        elif name == "ollama-fail-exc":
            raise RuntimeError("Simulated connection reset")
        else:
            return resp_500

    mock_client = AsyncMock()
    mock_client.post.side_effect = mock_post

    with (
        patch("router.main.get_http_client", return_value=mock_client),
        patch("router.main.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread,
    ):
        mock_to_thread.return_value = mock_config
        # Should complete without error or KeyError
        await _register_ollama_models_in_db("test_master_key")

    assert mock_client.post.call_count == 4
    assert "Ollama DB registration: 1 registered, 3 failed" in caplog.text
    assert "HTTP 500" in caplog.text
    assert "Simulated connection reset" in caplog.text
