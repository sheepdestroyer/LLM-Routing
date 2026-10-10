"""Metadata integration uses disposable in-memory HTTP transports, never live services."""

import asyncio
import copy
import os
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.testclient import TestClient

import router.main as rm
from router.model_metadata import CATALOG_URL, OpenRouterMetadataSync
from router.model_sync import ModelRegistrySync


@pytest.fixture(autouse=True)
def isolate_metadata_instance(monkeypatch):
    monkeypatch.setattr(rm, "_metadata_sync", None)
    monkeypatch.setattr(rm, "_metadata_sync_config", None)
    monkeypatch.setenv("LITELLM_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("ROUTER_API_KEY", "admin-key")


def test_singleton_identity_and_configuration_change(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(rm, "get_http_client", lambda: client)
    first = rm._get_metadata_sync("test-master-key")
    assert isinstance(first, OpenRouterMetadataSync)
    assert first is rm._get_metadata_sync("test-master-key")
    assert first._client is client
    with pytest.raises(RuntimeError, match="restart required"):
        rm._get_metadata_sync("rotated-secret")
    monkeypatch.setattr(rm, "LITELLM_URL", "http://other-proxy")
    with pytest.raises(RuntimeError, match="restart required"):
        rm._get_metadata_sync("test-master-key")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_close_resets_instance_and_does_not_close_shared_client(monkeypatch, failure):
    client = AsyncMock()
    monkeypatch.setattr(rm, "get_http_client", lambda: client)
    engine = rm._get_metadata_sync("test-master-key")
    if failure:
        monkeypatch.setattr(engine, "aclose", AsyncMock(side_effect=RuntimeError("close failed")))
    await rm._close_metadata_sync()
    assert rm._metadata_sync is None
    assert rm._metadata_sync_config is None
    client.aclose.assert_not_awaited()
    await rm._close_metadata_sync()


@pytest.mark.asyncio
@pytest.mark.parametrize("key,failure", [("test-master-key", False), ("test-master-key", True), ("", False)])
async def test_periodic_scan_interval_error_recovery_and_disabled_key(monkeypatch, key, failure):
    monkeypatch.setenv("LITELLM_MASTER_KEY", key)
    engine = AsyncMock()
    if failure:
        engine.reconcile.side_effect = RuntimeError("scan failed")
    getter = MagicMock(return_value=engine)
    sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])
    monkeypatch.setattr(rm, "_get_metadata_sync", getter)
    with patch("router.main.asyncio.sleep", sleep):
        await rm._periodic_model_metadata_sync()
    assert [call.args for call in sleep.await_args_list] == [(60,), (60,), (60,)]
    assert engine.reconcile.await_count == (2 if key else 0)
    if key:
        engine.reconcile.assert_awaited_with()


@pytest.mark.parametrize("path", ["/admin/sync-models", "/admin/model-metadata/status"])
@pytest.mark.parametrize("token,keys,expected", [(None, True, 401), ("test-key", True, 403), ("test-key", False, 403)])
def test_admin_authentication_fails_closed(monkeypatch, path, token, keys, expected):
    if not keys:
        monkeypatch.setenv("LITELLM_MASTER_KEY", "")
        monkeypatch.setenv("ROUTER_API_KEY", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    client = TestClient(rm.app)
    result = client.post(path, headers=headers) if path.endswith("sync-models") else client.get(path, headers=headers)
    assert result.status_code == expected
    assert rm._metadata_sync is None


@pytest.mark.parametrize(
    "query,registry_runs,dry,force",
    [
        ("", True, False, False),
        ("?metadata_only=true", False, False, False),
        ("?dry_run=true", False, True, False),
        ("?dry_run=true&metadata_only=false&force_catalog_refresh=true", False, True, True),
        ("?metadata_only=true&force_catalog_refresh=true", False, False, True),
    ],
)
def test_admin_controls_are_additive_and_dry_run_never_syncs_registry(monkeypatch, query, registry_runs, dry, force):
    events = []

    async def registry():
        events.append("registry")
        return {"created": 2, "pruned_duplicates": 1}

    async def metadata(**kwargs):
        events.append("metadata")
        return {"dry_run": kwargs["dry_run"], "counts": {"would_update": 1}}

    engine = MagicMock(reconcile=AsyncMock(side_effect=metadata))
    getter = MagicMock(return_value=engine)
    monkeypatch.setattr(rm, "_get_metadata_sync", getter)
    with patch.object(ModelRegistrySync, "sync_all_models", AsyncMock(side_effect=registry)) as sync:
        result = TestClient(rm.app).post("/admin/sync-models" + query, headers={"Authorization": "Bearer admin-key"})
    assert result.status_code == 200
    body = result.json()
    assert body["status"] == "ok"
    assert body["metadata"]["dry_run"] is dry
    engine.reconcile.assert_awaited_once_with(dry_run=dry, force_catalog_refresh=force)
    getter.assert_called_once_with("test-master-key")
    assert sync.await_count == int(registry_runs)
    assert events == (["registry", "metadata"] if registry_runs else ["metadata"])
    if registry_runs:
        assert body["results"] == {"created": 2, "pruned_duplicates": 1}
    else:
        assert body["results"] == dict.fromkeys(
            ("pruned_duplicates", "removed_stale", "created", "updated", "unchanged", "failed"), 0
        )


def test_status_is_read_only_before_and_after_reconcile(monkeypatch):
    client = TestClient(rm.app)
    headers = {"Authorization": "Bearer admin-key"}
    assert client.get("/admin/model-metadata/status", headers=headers).json()["metadata"] == {}
    engine = OpenRouterMetadataSync("http://test", "test-master-key", client=AsyncMock())
    engine._status = {"counts": {"unresolved": 1}, "deployments": [{"source_id": "test/unknown"}]}
    monkeypatch.setattr(rm, "_metadata_sync", engine)
    original = copy.deepcopy(engine._status)
    result = client.get("/admin/model-metadata/status", headers=headers)
    assert result.json()["metadata"] == original
    result.json()["metadata"]["counts"]["unresolved"] = 99
    assert engine.get_status() == original
    engine._client.get.assert_not_awaited()
    engine._client.patch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("ready,failure", [(True, False), (True, True), (False, False)])
async def test_startup_order_nonfatal_failure_and_shutdown(monkeypatch, ready, failure):
    events = []
    client = AsyncMock()
    client.get.return_value = MagicMock(status_code=200)
    engine = OpenRouterMetadataSync("http://test", "test-master-key", client=client)

    async def registry():
        events.append("registry")
        return {}

    async def roster(*args):
        events.append("roster")

    async def langfuse():
        events.append("langfuse")

    async def metadata():
        events.append("metadata")
        if failure:
            raise RuntimeError("catalog unavailable")
        return {}

    async def wait_forever():
        await asyncio.Event().wait()

    monkeypatch.setenv("LITELLM_READINESS_TIMEOUT", "1" if ready else "0")
    monkeypatch.setattr(rm, "_metadata_sync", engine)
    monkeypatch.setattr(rm, "_metadata_sync_config", (rm.LITELLM_URL.rstrip("/"), "test-master-key"))
    monkeypatch.setattr(rm, "_http_client", client)
    monkeypatch.setattr(rm, "_classifier_client", None)
    monkeypatch.setattr(rm, "_llama_client", None)
    monkeypatch.setattr(rm, "_redis_client", None)
    with ExitStack() as stack:
        for name in [
            "sync_stats_from_valkey",
            "sync_cooldowns_from_valkey",
            "save_persisted_stats",
            "_atomic_write_json_async",
        ]:
            stack.enter_context(patch.object(rm, name, AsyncMock()))
        for name in [
            "push_aggregate_scores",
            "_periodic_triage_cache_cleanup",
            "_periodic_model_sync",
            "_periodic_best_free_model_refresh",
            "_periodic_model_metadata_sync",
        ]:
            stack.enter_context(patch.object(rm, name, AsyncMock(side_effect=wait_forever)))
        stack.enter_context(patch.object(rm, "get_http_client", return_value=client))
        stack.enter_context(patch.object(ModelRegistrySync, "sync_all_models", AsyncMock(side_effect=registry)))
        stack.enter_context(patch.object(rm, "sync_adaptive_router_roster", AsyncMock(side_effect=roster)))
        stack.enter_context(patch.object(rm, "_register_langfuse_models_in_db", AsyncMock(side_effect=langfuse)))
        stack.enter_context(patch.object(engine, "reconcile", AsyncMock(side_effect=metadata)))
        async with rm.lifespan(rm.app):
            await asyncio.sleep(0)
            assert events == (["registry", "roster", "langfuse", "metadata"] if ready else ["langfuse"])
    assert rm._metadata_sync is None
    assert rm._metadata_sync_config is None
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_timer_and_manual_share_lock_cache_and_refresh_changed_alias(monkeypatch):
    row = {
        "model_name": "user-alias",
        "litellm_params": {"model": "openrouter/test/one"},
        "model_info": {"id": "alias", "db_model": True},
    }
    requests = []
    real_sleep = asyncio.sleep
    catalog = [
        {
            "id": name,
            "context_length": size,
            "top_provider": {"max_completion_tokens": 50},
            "pricing": {"prompt": "0.1", "completion": "0.2"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
            "supported_parameters": ["tools"],
        }
        for name, size in [("test/one", 100), ("test/two", 200)]
    ]

    async def handler(request):
        requests.append((request.method, str(request.url)))
        await real_sleep(0)
        if str(request.url) == CATALOG_URL:
            return httpx.Response(200, json={"data": catalog})
        if request.method == "GET":
            return httpx.Response(200, json={"data": [copy.deepcopy(row)]})
        assert request.method == "PATCH"
        import json

        for scope, fields in json.loads(request.content).items():
            row[scope].update(fields)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(rm, "get_http_client", lambda: client)
        engine = rm._get_metadata_sync("test-master-key")
        results = await asyncio.gather(engine.reconcile(), rm._get_metadata_sync("test-master-key").reconcile())
        assert [r["counts"]["updated"] for r in results] == [1, 0]
        assert len([url for method, url in requests if url == CATALOG_URL]) == 1
        row["litellm_params"]["model"] = "openrouter/test/two"
        sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
        with patch.object(rm.asyncio, "sleep", sleep):
            await rm._periodic_model_metadata_sync()
        assert row["model_info"]["max_input_tokens"] == 200
        assert len([url for method, url in requests if url == CATALOG_URL]) == 1
        assert len([method for method, url in requests if method == "PATCH"]) == 2
        await rm._close_metadata_sync()
        assert not client.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("max_output_tokens", 123),
        ("input_cost_per_token", 0.0),
        ("cache_read_input_token_cost", 0.001),
        ("price_tiers", [{"above": 100, "rate": 0.02}]),
        ("supports_vision", False),
        ("is_public_model_group", True),
    ],
)
async def test_registry_compares_all_configured_metadata_and_uses_partial_patch(field, value):
    client = AsyncMock()
    client.patch.return_value = MagicMock(status_code=200)
    sync = ModelRegistrySync("http://test", "key", client=client)
    target = {
        "model_name": "managed",
        "litellm_params": {"model": "other/provider"},
        "model_info": {field: value, "updated_at": "ignored"},
    }
    existing = {
        "managed": [
            {
                "litellm_params": target["litellm_params"],
                "model_info": {
                    "id": "id/encoded",
                    "db_model": True,
                    "metadata": {"operator": "keep"},
                    "max_input_tokens": 99,
                },
            }
        ]
    }
    assert await sync.upsert_model(target, existing) == ("updated", True)
    client.post.assert_not_awaited()
    args, kwargs = client.patch.call_args
    assert args == ("http://test/model/id%2Fencoded/update",)
    assert kwargs["json"]["model_info"] == {"id": "id/encoded", field: value}
    existing["managed"][0]["model_info"][field] = value
    client.patch.reset_mock()
    assert await sync.upsert_model(target, existing) == ("unchanged", False)
    client.patch.assert_not_awaited()


def test_openrouter_registry_omits_derived_metadata_and_preserves_targets():
    models = ModelRegistrySync("http://test", "key").build_openrouter_models()
    assert [m["litellm_params"]["model"] for m in models] == [
        "openrouter/openrouter/auto",
        "openrouter/openai/gpt-5.6-luna",
        "openrouter/openai/gpt-5.6-luna",
    ]
    assert all(m["model_info"] == {"is_public_model_group": True} for m in models)


@pytest.mark.asyncio
async def test_legacy_explicit_openrouter_fields_not_overwritten_on_registry_drift():
    client = AsyncMock()
    client.patch.return_value = MagicMock(status_code=200)
    sync = ModelRegistrySync("http://test", "key", client=client)
    target = sync.build_openrouter_models()[1]
    legacy = {
        "id": "existing",
        "db_model": True,
        "supports_vision": False,
        "max_input_tokens": 17,
        "input_cost_per_token": 0,
        "metadata": {"operator": "keep"},
        "is_public_model_group": True,
    }
    existing = {
        target["model_name"]: [
            {"model_info": copy.deepcopy(legacy), "litellm_params": {"model": target["litellm_params"]["model"]}}
        ]
    }
    assert await sync.upsert_model(target, existing) == ("updated", True)
    assert client.patch.call_args.kwargs["json"]["model_info"] == {"id": "existing"}
    assert existing[target["model_name"]][0]["model_info"] == legacy


@pytest.mark.asyncio
async def test_admin_dry_run_real_transport_never_creates_deletes_or_patches(monkeypatch):
    methods = []
    row = {
        "model_name": "user-alias",
        "litellm_params": {"model": "openrouter/test/one", "api_key": "secret-not-in-report"},
        "model_info": {"id": "alias", "db_model": True},
    }

    def handler(request):
        methods.append(request.method)
        assert request.method == "GET"
        if str(request.url) == CATALOG_URL:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "test/one",
                            "context_length": 100,
                            "top_provider": {"max_completion_tokens": 50},
                            "pricing": {"prompt": "0.1", "completion": "0.2"},
                            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                            "supported_parameters": ["tools"],
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"data": [row]})

    original = copy.deepcopy(row)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as backend:
        monkeypatch.setattr(rm, "get_http_client", lambda: backend)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rm.app), base_url="http://router") as client:
            response = await client.post(
                "/admin/sync-models?dry_run=true&force_catalog_refresh=true",
                headers={"Authorization": "Bearer admin-key"},
            )
        assert response.status_code == 200
        assert response.json()["metadata"]["counts"]["would_update"] == 1
        assert "secret-not-in-report" not in response.text
        assert response.json()["results"]["created"] == 0
        assert methods == ["GET", "GET", "GET"]
        assert row == original
        await rm._close_metadata_sync()


@pytest.mark.asyncio
async def test_fallback_registration_omits_derived_fields(monkeypatch):
    client = AsyncMock()
    client.post.return_value = MagicMock(status_code=200)
    monkeypatch.setattr(rm, "get_http_client", lambda: client)
    with patch.object(rm.os.path, "exists", return_value=False):
        await rm._register_openrouter_models_in_db("test-master-key")
    assert client.post.await_count == 4
    payloads = [call.kwargs["json"] for call in client.post.await_args_list]
    assert all(payload["model_info"] == {"is_public_model_group": True} for payload in payloads)
    assert payloads[-1]["model_name"] == "gpt-5.6-luna"
    assert payloads[-1]["litellm_params"]["reasoning_effort"] == "max"


@pytest.mark.asyncio
async def test_config_registration_retains_explicit_operator_metadata(monkeypatch, tmp_path):
    config = tmp_path / "litellm.yaml"
    config.write_text(
        "model_list:\n  - model_name: custom-alias\n    litellm_params:\n      model: openrouter/test/operator\n      api_key: os.environ/OPENROUTER_API_KEY\n    model_info:\n      max_input_tokens: 17\n      supports_vision: false\n      input_cost_per_token: 0\n"
    )
    monkeypatch.setenv("LITELLM_CONFIG_PATH", str(config))
    client = AsyncMock()
    client.post.return_value = MagicMock(status_code=200)
    monkeypatch.setattr(rm, "get_http_client", lambda: client)
    await rm._register_openrouter_models_in_db("test-master-key")
    client.post.assert_awaited_once()
    payload = client.post.call_args.kwargs["json"]
    assert payload["model_info"] == {"max_input_tokens": 17, "supports_vision": False, "input_cost_per_token": 0}
    assert payload["litellm_params"]["api_key"] == "os.environ/OPENROUTER_API_KEY"


@pytest.mark.asyncio
async def test_free_roster_registration_has_one_metadata_owner(monkeypatch):
    client = AsyncMock()
    client.post.return_value = MagicMock(status_code=200)
    monkeypatch.setattr(rm, "get_http_client", lambda: client)
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setattr(
        rm,
        "_fetch_openrouter_free_models",
        AsyncMock(
            return_value=[
                {
                    "id": "test/free",
                    "score": 90,
                    "has_tools": True,
                    "context_length": 100,
                    "supported_parameters": ["tools", "vision"],
                }
            ]
        ),
    )
    monkeypatch.setattr(rm, "_registered_free_models", {})
    monkeypatch.setattr(rm, "_last_roster_sync", 0)
    purge = AsyncMock(side_effect=AssertionError("DB writes forbidden in this test"))
    monkeypatch.setattr(rm, "_purge_stale_deployments", purge)
    await rm.sync_adaptive_router_roster("test-master-key")
    purge.assert_not_awaited()
    assert client.post.await_count == 5
    for call in client.post.await_args_list:
        assert call.kwargs["json"]["model_info"] == {"is_public_model_group": True}
        assert call.kwargs["json"]["litellm_params"] == {"model": "openrouter/test/free", "request_timeout": 20}
