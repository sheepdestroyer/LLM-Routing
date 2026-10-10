"""Synthetic OpenRouter fixtures; no live provider requests or billing claims."""

import asyncio
import copy
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from router.model_metadata import CATALOG_URL, PROVENANCE_KEY, OpenRouterMetadataSync, normalize_openrouter_model


@pytest.fixture
def catalog_model():
    return {
        "id": "vendor/nested/model:free",
        "context_length": 100000,
        "top_provider": {"max_completion_tokens": 12000},
        "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
        "supported_parameters": ["tools", "reasoning", "parallel_tool_calls"],
        "pricing": {"prompt": "0.000001", "completion": "0.000002", "input_cache_read": "0"},
    }


def deployment(target="vendor/nested/model:free", ident="one", **info):
    return {
        "model_name": "stable-client-alias",
        "litellm_params": {"model": f"openrouter/{target}", "api_base": "https://provider.test/v1", "temperature": 0.2},
        "model_info": {"id": ident, "db_model": True, "access_groups": ["team"], **info},
    }


class API:
    def __init__(self, models, catalog):
        self.models = copy.deepcopy(models)
        self.catalog = copy.deepcopy(catalog)
        self.requests = []
        self.patches = []
        self.catalog_status = 200
        self.patch_status = 200
        self.read_status = 200
        self.read_mutation = None
        self.drop_write = False

    async def handle(self, request):
        self.requests.append(request)
        if str(request.url) == CATALOG_URL:
            assert "authorization" not in request.headers
            return httpx.Response(self.catalog_status, json={"data": self.catalog})
        assert request.headers["authorization"] == "Bearer unit-test-secret"
        if request.method == "GET":
            selected = self.models
            ident = request.url.params.get("litellm_model_id")
            if ident:
                if self.read_mutation:
                    self.read_mutation(self)
                selected = [
                    m for m in self.models if isinstance(m.get("model_info"), dict) and m["model_info"]["id"] == ident
                ]
            return httpx.Response(self.read_status, json={"data": copy.deepcopy(selected)})
        body = json.loads(request.content)
        self.patches.append(body)
        if self.patch_status == 200 and not self.drop_write:
            item = next(
                m
                for m in self.models
                if isinstance(m.get("model_info"), dict) and m["model_info"]["id"] == body["model_info"]["id"]
            )
            for scope in ("model_info", "litellm_params"):
                item[scope].update(body.get(scope, {}))
        return httpx.Response(self.patch_status, json={"ok": True})

    def sync(self):
        return OpenRouterMetadataSync(
            "https://proxy.test",
            "unit-test-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle), timeout=3600),
        )


def test_normalize_limits_capabilities_and_prices(catalog_model):
    normalized = normalize_openrouter_model(catalog_model)
    info = normalized["model_info"]
    assert info["max_input_tokens"] == 100000  # Combined context ceiling, not context minus output.
    assert info["max_output_tokens"] == info["max_tokens"] == 12000
    assert info["mode"] == "chat"
    assert info["supports_vision"] is True
    assert info["supports_function_calling"] is True
    assert info["supports_reasoning"] is True
    assert normalized["litellm_params"]["cache_read_input_token_cost"] == 0
    assert normalized["unresolved"] == []


@pytest.mark.asyncio
async def test_adopt_then_noop_alias_stable_and_safe(catalog_model):
    api = API([deployment(metadata={"unrelated": {"keep": True}})], [catalog_model])
    sync = api.sync()
    first = await sync.reconcile()
    assert first["counts"]["updated"] == 1
    assert len(api.patches) == 1
    assert set(api.patches[0]) == {"model_info", "litellm_params"}
    assert "model" not in api.patches[0]["litellm_params"]
    assert "api_base" not in api.patches[0]["litellm_params"]
    assert api.models[0]["model_name"] == "stable-client-alias"
    assert api.models[0]["model_info"]["access_groups"] == ["team"]
    assert api.models[0]["model_info"]["metadata"]["unrelated"] == {"keep": True}
    second = await sync.reconcile()
    assert second["counts"]["unchanged"] == 1
    assert len(api.patches) == 1
    assert sum(str(r.url) == CATALOG_URL for r in api.requests) == 1
    await sync.aclose()
    await sync._client.aclose()


@pytest.mark.asyncio
async def test_override_zero_false_and_dry_run(catalog_model):
    api = API([deployment(max_input_tokens=0, supports_vision=False)], [catalog_model])
    sync = api.sync()
    before = copy.deepcopy(api.models)
    result = await sync.reconcile(dry_run=True)
    assert result["counts"]["would_update"] == 1
    assert not api.patches
    assert api.models == before
    assert "unit-test-secret" not in json.dumps(result)
    await sync.reconcile()
    assert api.models[0]["model_info"]["max_input_tokens"] == 0
    assert api.models[0]["model_info"]["supports_vision"] is False
    await sync._client.aclose()


@pytest.mark.parametrize("value", [True, None, {}, [], "broken", "NaN", "Infinity", "-1", "1e999", "1e-9999"])
def test_price_validation(value, catalog_model):
    catalog_model["pricing"]["prompt"] = value
    result = normalize_openrouter_model(catalog_model)
    assert "input_cost_per_token" not in result["litellm_params"]
    assert "prompt" in result["unresolved"]


@pytest.mark.parametrize("value", [None, 0, "1.5", str(2**54)])
def test_invalid_limits(value, catalog_model):
    catalog_model["context_length"] = value
    catalog_model["top_provider"] = {"max_completion_tokens": value}
    result = normalize_openrouter_model(catalog_model)
    assert "context_length" in result["unresolved"]
    assert "max_completion_tokens" in result["unresolved"]


def test_missing_or_invalid_evidence_and_audio(catalog_model):
    empty = normalize_openrouter_model({})
    assert empty["model_info"] == {}
    assert set(empty["unresolved"]) == {
        "context_length",
        "max_completion_tokens",
        "input_modalities",
        "output_modalities",
        "supported_parameters",
        "mode",
        "prompt",
        "completion",
    }
    catalog_model["architecture"] = {"input_modalities": ["text", "audio"], "output_modalities": ["audio"]}
    catalog_model["supported_parameters"] = ["reasoning_effort", "tool_choice", "tool_choice"]
    result = normalize_openrouter_model(catalog_model)["model_info"]
    assert result["supports_audio_input"] is True
    assert result["supports_audio_output"] is True
    assert result["supports_vision"] is False
    assert result["supports_reasoning"] is True
    catalog_model["architecture"] = {"input_modalities": [7], "output_modalities": None}
    catalog_model["supported_parameters"] = "tools"
    assert "input_modalities" in normalize_openrouter_model(catalog_model)["unresolved"]


def test_exact_tiers_later_wins_inheritance_and_output_anchor(catalog_model):
    catalog_model["pricing"]["overrides"] = [
        {"min_prompt_tokens": 272001, "prompt": "0.000005", "input_cache_read": "0.0000005"},
        {"min_prompt_tokens": 100000, "completion": "0.000003"},
        {"min_prompt_tokens": 272001, "completion": "0.000004"},
    ]
    normalized = normalize_openrouter_model(catalog_model)
    assert normalized["litellm_params"]["tiered_pricing"] == [
        {
            "range": [0, 100000],
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
            "cache_read_input_token_cost": 0,
        },
        {
            "range": [100000, 272001],
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 3e-6,
            "cache_read_input_token_cost": 0,
        },
        {
            "range": [272001, 2**53],
            "input_cost_per_token": 5e-6,
            "output_cost_per_token": 4e-6,
            "cache_read_input_token_cost": 5e-7,
        },
    ]
    assert not any("_above_" in field for scope in ("model_info", "litellm_params") for field in normalized[scope])


@pytest.mark.parametrize(
    "overrides",
    [
        None,
        {},
        [None],
        [{}],
        [{"min_prompt_tokens": "1.5"}],
        [{"min_prompt_tokens": 200000, "utc_start": 0, "prompt": "0.1"}],
        [{"min_prompt_tokens": 200000, "prompt": "-1"}],
        [{"min_prompt_tokens": 200000}],
        [{"min_prompt_tokens": 2**53, "prompt": "0.1"}],
    ],
)
def test_unsupported_or_malformed_overrides(overrides, catalog_model):
    catalog_model["pricing"]["overrides"] = overrides
    result = normalize_openrouter_model(catalog_model)
    assert "pricing_overrides" in result["unresolved"]
    assert "tiered_pricing" not in result["litellm_params"]


def test_tier_requires_valid_prompt_anchor(catalog_model):
    catalog_model["pricing"].pop("prompt")
    catalog_model["pricing"]["overrides"] = [{"min_prompt_tokens": 200000, "completion": "0.1"}]
    assert "pricing_overrides" in normalize_openrouter_model(catalog_model)["unresolved"]


@pytest.mark.asyncio
async def test_retarget_owned_fields_stale_limits_replace_paid_table_and_restart(catalog_model):
    catalog_model["pricing"]["overrides"] = [
        {"min_prompt_tokens": 272000, "prompt": "0.000005", "completion": "0.000007", "input_cache_read": "0.000003"}
    ]
    api = API([deployment()], [catalog_model])
    sync = api.sync()
    await sync.reconcile()
    # Simulated Admin UI changes preserve deployment and alias; reconstruct synchronizer as a restart.
    api.models[0]["litellm_params"]["model"] = "openrouter/other/model"
    replacement = copy.deepcopy(catalog_model)
    replacement.update(id="other/model", context_length=20000, top_provider={})
    replacement["architecture"]["input_modalities"] = ["text"]
    replacement["pricing"] = {"prompt": "0.000002", "completion": "0.000004", "input_cache_read": "0.000001"}
    api.catalog = [replacement]
    restarted = api.sync()
    result = await restarted.reconcile()
    assert result["counts"]["updated"] == 1
    info = api.models[0]["model_info"]
    params = api.models[0]["litellm_params"]
    assert info["max_input_tokens"] == 20000
    assert info["supports_vision"] is False
    assert params["tiered_pricing"] == []
    assert params["input_cost_per_token"] == 0.000002
    assert params["output_cost_per_token"] == 0.000004
    assert params["cache_read_input_token_cost"] == 0.000001
    state = info["metadata"][PROVENANCE_KEY]
    assert state["status"] == "stale"
    assert state["field_sources"]["model_info.max_output_tokens"] == catalog_model["id"]
    assert state["field_sources"]["model_info.max_input_tokens"] == "other/model"
    assert info["max_output_tokens"] == 12000  # Stock cannot delete it; never claim it was cleared.
    await sync._client.aclose()
    await restarted._client.aclose()


@pytest.mark.asyncio
async def test_changed_owned_values_are_operator_overrides(catalog_model):
    api = API([deployment()], [catalog_model])
    sync = api.sync()
    await sync.reconcile()
    api.models[0]["model_info"].update(supports_vision=False, max_input_tokens=0)
    api.models[0]["litellm_params"]["input_cost_per_token"] = 0
    catalog_model["pricing"]["prompt"] = "0.0009"
    api.catalog = [catalog_model]
    result = await sync.reconcile(force_catalog_refresh=True)
    assert result["counts"]["updated"] == 1  # Relinquish ownership, not overwrite operator values.
    state = api.models[0]["model_info"]["metadata"][PROVENANCE_KEY]
    assert set(state["overrides"]) == {
        "model_info.supports_vision",
        "model_info.max_input_tokens",
        "litellm_params.input_cost_per_token",
    }
    assert "supports_vision" not in state["last_written"]["model_info"]
    assert api.models[0]["litellm_params"]["input_cost_per_token"] == 0
    await sync.reconcile()
    assert len(api.patches) == 2
    await sync._client.aclose()


@pytest.mark.asyncio
async def test_unknown_changed_target_marks_old_values_with_old_sources(catalog_model):
    api = API([deployment()], [catalog_model])
    sync = api.sync()
    await sync.reconcile()
    api.models[0]["litellm_params"]["model"] = "openrouter/vendor/unlisted:free"
    result = await sync.reconcile()
    assert result["counts"]["unresolved"] == 1
    payload = api.patches[-1]
    expected = dict.fromkeys(
        key for key in normalize_openrouter_model(catalog_model)["litellm_params"] if key != "tiered_pricing"
    )
    assert payload["litellm_params"] == expected
    assert set(payload["model_info"]) == {"id", "metadata"}
    state = payload["model_info"]["metadata"][PROVENANCE_KEY]
    assert state["status"] == "unresolved"
    assert "missing_catalog_model" in state["unresolved"]
    assert set(state["field_sources"].values()) == {catalog_model["id"]}
    assert api.models[0]["model_info"]["max_input_tokens"] == 100000
    await sync._client.aclose()


@pytest.mark.asyncio
async def test_catalog_failure_preserves_and_reports_stale_no_cached_fallback(catalog_model, caplog):
    api = API([deployment()], [catalog_model])
    sync = api.sync()
    await sync.reconcile()
    api.catalog_status = 503
    result = await sync.reconcile(force_catalog_refresh=True)
    assert result["counts"]["unresolved"] == 1
    assert result["errors"] == ["catalog:HTTPStatusError"]
    assert "catalog_unavailable" in result["deployments"][0]["provenance"]["unresolved"]
    assert "catalog unavailable" in caplog.text
    assert "unit-test-secret" not in caplog.text
    await sync._client.aclose()


@pytest.mark.asyncio
async def test_skip_yaml_other_prefix_duplicates_and_process_each_id(catalog_model):
    rows = [
        deployment(ident="one"),
        deployment(ident="two"),
        deployment(ident="one"),
        deployment(db_model=False),
        deployment(ident="three"),
        deployment(ident=None),
    ]
    rows[4]["litellm_params"]["model"] = "custom/openrouter/vendor/model"
    rows[3]["model_info"]["id"] = "yaml"
    api = API(rows, [catalog_model])
    # Remove duplicate in exact API read to reflect a valid response; initial duplicate remains scan-only.
    original = api.handle

    async def handler(request):
        response = await original(request)
        if request.method == "GET" and request.url.params.get("litellm_model_id") == "one":
            return httpx.Response(200, json={"data": [copy.deepcopy(api.models[0])]})
        return response

    sync = OpenRouterMetadataSync(
        "https://proxy.test", "unit-test-secret", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    result = await sync.reconcile()
    assert result["counts"] == {
        "scanned": 6,
        "skipped": 4,
        "updated": 2,
        "would_update": 0,
        "unchanged": 0,
        "unresolved": 0,
        "failed": 0,
    }
    assert [p["model_info"]["id"] for p in api.patches] == ["one", "two"]
    await sync._client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", ["patch", "verify", "before_target", "after_target", "gone", "read_error", "malformed_metadata"]
)
async def test_failures_and_races_are_reported(catalog_model, phase):
    api = API([deployment()], [catalog_model])
    sync = api.sync()
    if phase == "patch":
        api.patch_status = 500
    if phase == "verify":
        api.drop_write = True
    if phase == "read_error":

        async def broken(_):
            raise httpx.ReadError("Do not log unit-test-secret")

        sync._exact = broken
    if phase == "malformed_metadata":
        api.models[0]["model_info"]["metadata"] = ["unknown"]
    if phase in ("before_target", "after_target", "gone"):

        def mutate(api):
            if phase == "gone":
                api.models = []
            elif phase == "before_target" or api.patches:
                api.models[0]["litellm_params"]["model"] = "openrouter/changed"

        api.read_mutation = mutate
    result = await sync.reconcile()
    assert result["counts"]["failed"] == 1
    assert result["counts"]["updated"] == 0
    assert "unit-test-secret" not in json.dumps(result)
    await sync._client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{"data": {}}, {"data": [3]}, {}])
async def test_bad_deployment_scan(data):
    async def handler(request):
        return httpx.Response(200, json=data)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sync = OpenRouterMetadataSync("https://proxy.test", "secret", client)
    result = await sync.reconcile()
    assert result["errors"]
    assert result["deployments"] == []
    assert sync.get_status() == result
    mutable = sync.status
    mutable["errors"].clear()
    assert sync.status["errors"]
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "catalog", [{}, [], [None], [{}], [{"id": 3}], [{"id": ""}], [{"id": "duplicate"}, {"id": "duplicate"}]]
)
async def test_catalog_shape_and_identity(catalog):
    api = API([deployment()], catalog)
    sync = api.sync()
    result = await sync.reconcile()
    assert result["counts"]["unresolved"] == 1
    assert result["errors"] == ["catalog:ValueError"]
    assert sync._catalog is None
    await sync._client.aclose()


@pytest.mark.parametrize(
    "old",
    [
        [],
        {},
        None,
        {"version": 2},
        {"version": 1, "deployment_id": "wrong"},
        {"version": 1, "deployment_id": "one", "last_written": []},
        {"version": 1, "deployment_id": "one", "last_written": {"model_info": []}},
        {"version": 1, "deployment_id": "one", "field_sources": []},
        {"version": 1, "deployment_id": "one", "last_written": {"litellm_params": {"api_key": "never-echo"}}},
        {"version": 1, "deployment_id": "one", "last_written": {"model_info": {"metadata": {"private": "never-echo"}}}},
    ],
)
def test_untrusted_provenance_is_not_adopted(old, catalog_model):
    row = deployment(metadata={PROVENANCE_KEY: old})
    with pytest.raises(ValueError):
        OpenRouterMetadataSync._plan(row, catalog_model["id"], normalize_openrouter_model(catalog_model), "missing")


@pytest.mark.asyncio
async def test_owned_clients_close_external_clients_retained_and_lock_serializes(catalog_model, monkeypatch):
    owned = OpenRouterMetadataSync("https://proxy.test", "secret")
    await owned.aclose()
    client = await owned._http()
    assert client is await owned._http()
    await owned.aclose()
    assert client.is_closed
    assert owned._client is None
    api = API([deployment()], [catalog_model])
    sync = api.sync()
    results = await asyncio.gather(*(sync.reconcile() for _ in range(5)))
    assert sum(r["counts"]["updated"] for r in results) == 1
    assert sum(r["counts"]["unchanged"] for r in results) == 4
    assert len(api.patches) == 1
    await sync.aclose()
    assert not sync._client.is_closed
    sync._catalog_at -= 3601
    await sync.reconcile()
    assert sum(str(r.url) == CATALOG_URL for r in api.requests) == 2
    await sync._client.aclose()


@pytest.mark.asyncio
async def test_malformed_deployment_nested_shapes_are_skipped_and_empty_target_unresolved(catalog_model):
    api = API([{"model_info": "invalid", "litellm_params": []}, deployment(target="")], [catalog_model])
    sync = api.sync()
    result = await sync.reconcile()
    assert result["counts"]["skipped"] == 1
    assert result["counts"]["unresolved"] == 1
    assert result["counts"]["updated"] == 1
    await sync._client.aclose()


@pytest.mark.asyncio
async def test_catalog_never_leaks_injected_auth(catalog_model):
    api = API([deployment()], [catalog_model])
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(api.handle), headers={"Authorization": "Bearer global-secret"}
    )
    sync = OpenRouterMetadataSync("https://proxy.test", "unit-test-secret", client)
    assert (await sync.reconcile())["counts"]["updated"] == 1
    await client.aclose()


def test_false_is_not_equal_to_operator_zero(catalog_model):
    row = deployment(supports_vision=0)
    row["model_info"]["metadata"] = {
        PROVENANCE_KEY: {
            "version": 1,
            "deployment_id": "one",
            "last_written": {"model_info": {"supports_vision": False}},
        }
    }
    plan = OpenRouterMetadataSync._plan(row, catalog_model["id"], normalize_openrouter_model(catalog_model), "missing")
    assert "model_info.supports_vision" in plan["provenance"]["overrides"]


@pytest.mark.asyncio
async def test_exact_active_tier_patch_is_in_stock_consumed_params_table(catalog_model):
    catalog_model["pricing"]["overrides"] = [{"min_prompt_tokens": 272001, "prompt": "0.000005"}]
    api = API([deployment()], [catalog_model])
    sync = api.sync()
    await sync.reconcile()
    payload = api.patches[0]
    table = payload["litellm_params"]["tiered_pricing"]
    assert table[1] == {
        "range": [272001, 2**53],
        "input_cost_per_token": 5e-6,
        "output_cost_per_token": 2e-6,
        "cache_read_input_token_cost": 0,
    }
    assert not any("_above_" in key for scope in ("model_info", "litellm_params") for key in payload[scope])
    assert api.models[0]["litellm_params"]["tiered_pricing"] == table
    await sync._client.aclose()


@pytest.mark.parametrize("outputs", [None, [], ["embedding"], ["image"], ["audio"]])
def test_unknown_mode_not_invented(outputs, catalog_model):
    catalog_model["architecture"]["output_modalities"] = outputs
    normalized = normalize_openrouter_model(catalog_model)
    assert "mode" not in normalized["model_info"]
    assert "mode" in normalized["unresolved"]


@pytest.mark.asyncio
async def test_provider_exit_reports_stale_owned_evidence_without_takeover(catalog_model):
    api = API([deployment(), deployment(ident="unowned")], [catalog_model])
    sync = api.sync()
    await sync.reconcile()
    api.models[0]["litellm_params"]["model"] = "openai/unrelated"
    api.models[1] = deployment(ident="unowned")
    api.models[1]["litellm_params"]["model"] = "openai/unrelated"
    api.models[0]["model_info"]["supports_vision"] = False
    before = copy.deepcopy(api.models[0]["litellm_params"])
    result = await sync.reconcile()
    assert result["counts"]["skipped"] == 1
    state = api.models[0]["model_info"]["metadata"][PROVENANCE_KEY]
    assert state["status"] == "stale"
    assert "provider_changed" in state["unresolved"]
    assert set(state["field_sources"].values()) == {catalog_model["id"]}
    assert api.models[0]["model_info"]["supports_vision"] is False
    assert api.models[0]["litellm_params"]["model"] == before["model"]
    expected = dict.fromkeys(
        key for key in normalize_openrouter_model(catalog_model)["litellm_params"] if key != "tiered_pricing"
    )
    assert api.patches[-1]["litellm_params"] == expected
    assert "model" not in api.patches[-1]["litellm_params"]
    await sync._client.aclose()


@pytest.mark.asyncio
async def test_no_client_defaults_leak_to_public_catalog(catalog_model):
    api = API([deployment()], [catalog_model])
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(api.handle),
        headers={"X-API-Key": "key-secret", "Cookie": "cookie-secret", "X-Private": "private-secret"},
        cookies={"session": "session-secret"},
        params={"api_key": "query-secret"},
        auth=httpx.BasicAuth("basic-secret", "password-secret"),
    )
    sync = OpenRouterMetadataSync("https://proxy.test", "unit-test-secret", client)
    assert await sync._get_catalog(True) == {catalog_model["id"]: catalog_model}
    public = next(r for r in api.requests if r.url.host == "openrouter.ai")
    assert str(public.url) == CATALOG_URL
    assert set(public.headers) <= {"host", "accept"}
    assert "secret" not in str(public.headers)
    await client.aclose()


@pytest.mark.asyncio
async def test_catalog_retry_retains_cache_marks_stale_and_recovers(catalog_model, monkeypatch):
    import router.model_metadata as module

    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    api = API([deployment()], [catalog_model])
    sync = api.sync()
    await sync.reconcile()
    api.catalog_status = 429
    first = await sync.reconcile(force_catalog_refresh=True)
    second = await sync.reconcile(force_catalog_refresh=True)
    assert len([r for r in api.requests if str(r.url) == CATALOG_URL]) == 2
    assert sync._catalog[catalog_model["id"]] == catalog_model
    assert first["deployments"][0]["provenance"]["status"] == "stale"
    assert second["deployments"][0]["provenance"]["status"] == "stale"
    api.models.append(deployment(ident="new"))
    third = await sync.reconcile()
    assert third["counts"]["updated"] == 1
    assert api.models[1]["model_info"]["max_input_tokens"] == 100000
    assert api.models[1]["model_info"]["metadata"][PROVENANCE_KEY]["status"] == "stale"
    clock[0] += 4000
    api.catalog_status = 200
    recovered = await sync.reconcile()
    assert recovered["counts"]["unresolved"] == 0
    assert len([r for r in api.requests if str(r.url) == CATALOG_URL]) == 3
    await sync._client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not-json", b'{"data":[]}', b'{"data":[null]}', b'{"data":[{"id":"good"},3]}'])
async def test_bad_catalog_does_not_replace_good_snapshot(body, catalog_model):
    api = API([deployment()], [catalog_model])
    sync = api.sync()
    await sync.reconcile()
    original = api.handle

    async def handler(request):
        if str(request.url) == CATALOG_URL:
            return httpx.Response(200, content=body)
        return await original(request)

    sync._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    report = await sync.reconcile(force_catalog_refresh=True)
    assert report["errors"]
    assert report["deployments"][0]["provenance"]["status"] == "stale"
    assert sync._catalog == {catalog_model["id"]: catalog_model}
    await sync._client.aclose()


@pytest.mark.asyncio
async def test_catalog_size_is_bounded_while_streaming(monkeypatch):
    import router.model_metadata as module

    monkeypatch.setattr(module, "CATALOG_MAX_BYTES", 32)
    chunks = []
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(10):
                chunks.append(1)
                yield b" " * 16

        async def aclose(self):
            closed.append(True)

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Stream())))
    sync = OpenRouterMetadataSync("https://proxy.test", "secret", client)
    with pytest.raises(ValueError):
        await sync._get_catalog(True)
    assert len(chunks) == 3
    assert closed == [True]
    assert sync._catalog is None
    await client.aclose()


@pytest.mark.parametrize(
    "value, expected",
    [(None, 0), ("120", 120), ("999999", 3600), ("-1", 0), ("invalid", 0), ("Wed, 21 Oct 2015 07:28:00 GMT", 300)],
)
def test_retry_after_seconds_date_invalid_and_bound(value, expected, monkeypatch):
    import router.model_metadata as module

    monkeypatch.setattr(module.time, "time", lambda: 1445412180)
    assert OpenRouterMetadataSync._retry_after(value) == expected


@pytest.mark.asyncio
async def test_429_retry_after_deadline_and_exponential_limit(monkeypatch):
    import router.model_metadata as module

    clock = [100.0]
    requests = []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    async def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"}, content=b"secret raw body")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sync = OpenRouterMetadataSync("https://proxy.test", "secret", client)
    for attempt in range(10):
        with pytest.raises(httpx.HTTPStatusError):
            await sync._get_catalog(True)
        assert 120 <= sync._retry_at - clock[0] <= module.RETRY_MAX
        with pytest.raises(ValueError, match="deferred"):
            await sync._get_catalog(True)
        assert len(requests) == attempt + 1
        clock[0] = sync._retry_at
    await client.aclose()


@pytest.mark.asyncio
async def test_catalog_cancel_propagates_and_closes_stream():
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise asyncio.CancelledError
            yield b""  # Async generator contract; never executed.

        async def aclose(self):
            closed.append(True)

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Stream())))
    sync = OpenRouterMetadataSync("https://proxy.test", "secret", client)
    with pytest.raises(asyncio.CancelledError):
        await sync._get_catalog(True)
    assert closed == [True]
    assert sync._catalog_failures == 0
    await client.aclose()


def test_missing_owned_base_prices_clear_both_scopes_only(catalog_model):
    row = deployment(input_cost_per_token=0.1)
    row["litellm_params"]["input_cost_per_token"] = 0.1
    row["model_info"]["metadata"] = {
        PROVENANCE_KEY: {
            "version": 1,
            "deployment_id": "one",
            "source_id": "old/model",
            "last_written": {scope: {"input_cost_per_token": 0.1} for scope in ("model_info", "litellm_params")},
        }
    }
    plan = OpenRouterMetadataSync._plan(row, "new/model", None, "missing_catalog_model")
    assert plan["changes"] == {scope: {"input_cost_per_token": None} for scope in ("model_info", "litellm_params")}
    for scope in ("model_info", "litellm_params"):
        row[scope].update(plan["payload"][scope])
    again = OpenRouterMetadataSync._plan(row, "new/model", None, "missing_catalog_model")
    assert again["dirty"] is False
    catalog_model["pricing"].pop("prompt")
    missing_price = OpenRouterMetadataSync._plan(row, "new/model", normalize_openrouter_model(catalog_model), "missing")
    assert "input_cost_per_token" not in missing_price["changes"]["litellm_params"]
    assert "input_cost_per_token" not in missing_price["changes"]["model_info"]


def apply_plan(row, plan):
    for scope in ("model_info", "litellm_params"):
        row[scope].update(plan["payload"].get(scope, {}))


def test_zero_threshold_no_empty_range_and_optional_unknown_cache(catalog_model):
    catalog_model["pricing"].pop("input_cache_read")
    catalog_model["pricing"]["overrides"] = [{"min_prompt_tokens": 0, "prompt": "0.000003"}]
    normalized = normalize_openrouter_model(catalog_model)
    assert normalized["unresolved"] == []
    assert normalized["litellm_params"]["tiered_pricing"] == [
        {"range": [0, 2**53], "input_cost_per_token": 3e-6, "output_cost_per_token": 2e-6}
    ]


@pytest.mark.parametrize("source", ["prompt", "completion"])
def test_unknown_required_rate_refuses_partial_table(source, catalog_model):
    catalog_model["pricing"].pop(source)
    catalog_model["pricing"]["overrides"] = [{"min_prompt_tokens": 272001, "completion": "0.000003"}]
    normalized = normalize_openrouter_model(catalog_model)
    assert "pricing_overrides" in normalized["unresolved"]
    assert "tiered_pricing" not in normalized["litellm_params"]


@pytest.mark.parametrize("operator", [[], [False], [0], [{"range": [0, 100], "input_cost_per_token": 0}]])
def test_operator_table_lists_preserved_at_adoption_and_after_ownership(operator, catalog_model):
    catalog_model["pricing"]["overrides"] = [{"min_prompt_tokens": 272001, "prompt": "0.000005"}]
    normalized = normalize_openrouter_model(catalog_model)
    row = deployment()
    row["litellm_params"]["tiered_pricing"] = copy.deepcopy(operator)
    initial = OpenRouterMetadataSync._plan(row, catalog_model["id"], normalized, "missing")
    assert "tiered_pricing" not in initial["changes"]["litellm_params"]
    assert "litellm_params.tiered_pricing" in initial["provenance"]["overrides"]
    row = deployment()
    apply_plan(row, OpenRouterMetadataSync._plan(row, catalog_model["id"], normalized, "missing"))
    row["litellm_params"]["tiered_pricing"] = copy.deepcopy(operator)
    changed = OpenRouterMetadataSync._plan(row, "new/model", normalized, "missing")
    assert "tiered_pricing" not in changed["changes"]["litellm_params"]
    assert "tiered_pricing" not in changed["provenance"]["last_written"]["litellm_params"]
    assert row["litellm_params"]["tiered_pricing"] == operator


@pytest.mark.parametrize("scope", ["litellm_params"])
@pytest.mark.parametrize("field", ["input_cost_per_token", "output_cost_per_token_above_272001_tokens"])
def test_manual_flat_pricing_not_superseded_by_table(scope, field, catalog_model):
    catalog_model["pricing"]["overrides"] = [{"min_prompt_tokens": 272001, "prompt": "0.000005"}]
    row = deployment()
    row[scope][field] = 0
    plan = OpenRouterMetadataSync._plan(row, catalog_model["id"], normalize_openrouter_model(catalog_model), "missing")
    assert "tiered_pricing" not in plan["changes"]["litellm_params"]
    assert f"{scope}.{field}" in plan["provenance"]["overrides"]
    assert "pricing_overrides" in plan["provenance"]["unresolved"]


@pytest.mark.asyncio
async def test_server_derived_info_prices_not_mistaken_for_manual_params(catalog_model):
    catalog_model["pricing"]["overrides"] = [{"min_prompt_tokens": 272001, "prompt": "0.000005"}]
    api = API([deployment()], [catalog_model])
    sync = api.sync()
    assert (await sync.reconcile())["counts"]["updated"] == 1
    # Stock exposes derived pricing in model_info; it is not an independent override.
    api.models[0]["model_info"].update(
        {key: value for key, value in api.models[0]["litellm_params"].items() if "cost" in key}
    )
    assert (await sync.reconcile())["counts"]["unchanged"] == 1
    catalog_model["pricing"]["overrides"][0]["prompt"] = "0.000006"
    api.catalog = [catalog_model]
    report = await sync.reconcile(force_catalog_refresh=True)
    assert report["counts"]["updated"] == 1
    assert report["deployments"][0]["provenance"]["overrides"] == []
    assert api.models[0]["litellm_params"]["tiered_pricing"][1]["input_cost_per_token"] == 6e-6
    await sync._client.aclose()


def test_manual_base_change_disables_owned_table_without_overwriting_operator(catalog_model):
    catalog_model["pricing"]["overrides"] = [{"min_prompt_tokens": 272001, "prompt": "0.000005"}]
    normalized = normalize_openrouter_model(catalog_model)
    row = deployment()
    apply_plan(row, OpenRouterMetadataSync._plan(row, catalog_model["id"], normalized, "missing"))
    row["litellm_params"]["input_cost_per_token"] = 0
    plan = OpenRouterMetadataSync._plan(row, catalog_model["id"], normalized, "missing")
    assert plan["changes"]["litellm_params"] == {"tiered_pricing": []}
    assert "litellm_params.input_cost_per_token" in plan["provenance"]["overrides"]


@pytest.mark.parametrize("target,reason", [(None, "provider_changed"), ("unlisted/model", "missing_catalog_model")])
def test_owned_nonempty_table_cleared_on_unknown_target_or_provider_exit(target, reason, catalog_model):
    catalog_model["pricing"]["overrides"] = [{"min_prompt_tokens": 272001, "prompt": "0.000005"}]
    row = deployment()
    apply_plan(
        row,
        OpenRouterMetadataSync._plan(row, catalog_model["id"], normalize_openrouter_model(catalog_model), "missing"),
    )
    plan = OpenRouterMetadataSync._plan(row, target, None, reason)
    assert plan["changes"]["litellm_params"]["tiered_pricing"] == []
    assert plan["changes"]["litellm_params"]["input_cost_per_token"] is None
    assert "litellm_params.tiered_pricing" in plan["provenance"]["unresolved"]


def test_same_target_outage_keeps_owned_table_and_incomplete_retarget_clears(catalog_model):
    catalog_model["pricing"]["overrides"] = [{"min_prompt_tokens": 272001, "prompt": "0.000005"}]
    row = deployment()
    apply_plan(
        row,
        OpenRouterMetadataSync._plan(row, catalog_model["id"], normalize_openrouter_model(catalog_model), "missing"),
    )
    plan = OpenRouterMetadataSync._plan(row, catalog_model["id"], None, "catalog_unavailable")
    assert not plan["changes"]["litellm_params"]
    assert plan["provenance"]["status"] == "stale"
    catalog_model["pricing"].pop("completion")
    plan = OpenRouterMetadataSync._plan(row, "new/model", normalize_openrouter_model(catalog_model), "missing")
    assert plan["changes"]["litellm_params"]["tiered_pricing"] == []
    assert "pricing_overrides" in plan["provenance"]["unresolved"]


def test_unsupported_literal_tier_ownership_not_speculatively_migrated(catalog_model):
    row = deployment(
        metadata={
            PROVENANCE_KEY: {
                "version": 1,
                "deployment_id": "one",
                "last_written": {"model_info": {"input_cost_per_token_above_272001_tokens": 5e-6}},
            }
        }
    )
    with pytest.raises(ValueError, match="Unsupported literal-tier ownership"):
        OpenRouterMetadataSync._plan(row, catalog_model["id"], normalize_openrouter_model(catalog_model), "missing")


@pytest.mark.asyncio
async def test_shared_long_timeout_explicitly_bounded_for_scan_exact_patch_and_verify(catalog_model):
    api = API([deployment()], [catalog_model])
    sync = api.sync()
    assert sync._client.timeout.read == 3600
    assert (await sync.reconcile())["counts"]["updated"] == 1
    assert len([r for r in api.requests if r.url.host == "proxy.test"]) == 4
    for request in api.requests:
        assert request.extensions["timeout"] == {"connect": 15.0, "read": 15.0, "write": 15.0, "pool": 15.0}
    await sync._client.aclose()


def test_nested_lists_bool_zero_and_dictionary_values_compare_exactly(catalog_model):
    from router.model_metadata import _equal

    assert not _equal([{"rate": False}], [{"rate": 0}])
    assert not _equal([], None)
    assert not _equal({}, [])
    assert not _equal([1], [1, 2])
    assert _equal({"range": [0, 272001]}, {"range": [0, 272001]})
