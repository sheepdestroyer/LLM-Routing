import asyncio
import copy
import json
import os
import sys
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from httpx import ASGITransport

from router.main import (
    CONFIG_PATH,
    MAX_TRIAGE_CACHE_SIZE,
    AnnotationPayload,
    ToolUsageRecord,
    _atomic_save_json,
    _atomic_write_json_async,
    _atomic_write_json_sync,
    _authenticate_client_request,
    _check_llama_health,
    _close_prop_ctx,
    _get_router_output_dir,
    _make_prop_ctx,
    _parse_oauth_token_info,
    _periodic_model_sync,
    _periodic_triage_cache_cleanup,
    _read_annotations_async,
    _register_ollama_models_in_db,
    _resolve_llama_endpoints,
    _save_best_model_to_disk,
    _save_free_models_roster,
    _validate_litellm_virtual_key,
    app,
    chat_completions,
    check_http_endpoint,
    resolve_external_urls,
    classify_request,
    detect_active_tool,
    extract_or_synthesize_session_id,
    get_best_free_model,
    get_classifier_client,
    get_dashboard_data,
    get_gemini_oauth_status,
    get_llama_client,
    get_llamacpp_metrics,
    get_pie_chart_gradient,
    lifespan,
    map_tool_to_category,
    maybe_trigger_roster_sync,
    proxy_audio,
    proxy_memory,
    proxy_models,
    record_tool_usage,
    _register_openrouter_models_in_db,
    resolve_external_urls,
    responses_api,
    save_annotations,
    save_persisted_stats,
    sync_adaptive_router_roster,
    sync_stats_from_valkey,
    triage_cache,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# 1. Singleton HTTP clients
# ---------------------------------------------------------------------------
def test_client_singletons():
    import router.main as rm

    c1 = get_classifier_client()
    c2 = get_classifier_client()
    assert c1 is c2

    l1 = get_llama_client()
    l2 = get_llama_client()
    assert l1 is l2


# ---------------------------------------------------------------------------
# 2. sync_stats_from_valkey branches
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sync_stats_from_valkey_branches():
    import router.main as rm

    mock_redis = AsyncMock()
    # 1. raw_stats is None, raw_timeline is None
    mock_redis.get.side_effect = [None, None]
    with patch("router.main.get_redis", return_value=mock_redis):
        await sync_stats_from_valkey()

    # 2. raw_stats is non-dict JSON, raw_timeline is not a list
    mock_redis.get.side_effect = [b'"not-a-dict"', b'"not-a-list"']
    with patch("router.main.get_redis", return_value=mock_redis):
        await sync_stats_from_valkey()

    # 3. raw_stats with missing keys, total_requests == 0, last_triage_decision == "None"
    rm.stats["total_requests"] = 0
    rm.stats.pop("tool_tokens", None)
    mock_redis.get.side_effect = [
        json.dumps(
            {
                "total_requests": 0,
                "last_triage_decision": "None",
                "tool_tokens": {"tree": 10},
            }
        ).encode("utf-8"),
        json.dumps([]).encode("utf-8"),
    ]
    with patch("router.main.get_redis", return_value=mock_redis):
        await sync_stats_from_valkey()
        assert "tool_tokens" in rm.stats
        assert rm.stats["tool_tokens"]["tree"] >= 10

    # 4. Exception handling
    mock_redis.get.side_effect = RuntimeError("Valkey connection down")
    with patch("router.main.get_redis", return_value=mock_redis):
        await sync_stats_from_valkey()


# ---------------------------------------------------------------------------
# 3. Langfuse context manager helpers
# ---------------------------------------------------------------------------
def test_langfuse_prop_ctx_helpers():
    import router.main as rm

    # _close_prop_ctx(None)
    assert _close_prop_ctx(None) is None

    # _close_prop_ctx with faulty context manager
    bad_ctx = MagicMock()
    bad_ctx.__exit__.side_effect = RuntimeError("exit failed")
    assert _close_prop_ctx(bad_ctx) is None

    # _make_prop_ctx(None, None)
    assert _make_prop_ctx(None, None) is None

    # _make_prop_ctx when propagate_attributes is None
    with patch("router.main.propagate_attributes", None):
        assert _make_prop_ctx("sess-1", "user-1") is None


# ---------------------------------------------------------------------------
# 4. extract_or_synthesize_session_id branches
# ---------------------------------------------------------------------------
def test_extract_or_synthesize_session_id():
    req = MagicMock()
    req.headers = {}
    req.state = MagicMock()
    req.state.auth_key_alias = "testalias"

    # Explicit session header or body
    assert extract_or_synthesize_session_id({"session_id": "explicit-1"}, req) == "explicit-1"
    assert extract_or_synthesize_session_id({"session": "explicit-2"}, req) == "explicit-2"
    req.headers["x-session-id"] = "explicit-3"
    assert extract_or_synthesize_session_id({}, req) == "explicit-3"
    del req.headers["x-session-id"]

    # Non-dict msg and role in ("system", "user") with list content
    body = {
        "messages": [
            "not-a-dict",
            {"role": "system", "content": [{"type": "text", "text": "sys prompt"}]},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "user prompt"},
        ]
    }
    sess_id = extract_or_synthesize_session_id(body, req)
    assert sess_id.startswith("sess-testalias-")

    # Empty messages list -> fallback to uuid
    req.state.auth_key_alias = ""
    sess_id2 = extract_or_synthesize_session_id({"messages": []}, req)
    assert sess_id2.startswith("sess-")


# ---------------------------------------------------------------------------
# 5. _resolve_llama_endpoints branches
# ---------------------------------------------------------------------------
def test_resolve_llama_endpoints_branches():
    import router.main as rm

    # 1. os.environ/ in raw_server and raw_classifier
    test_config = {
        "llama_server_url": "os.environ/MY_LLAMA_SERVER",
        "router": {"router_model": {"api_base": "os.environ/MY_LLAMA_CLASSIFIER"}},
    }
    with (
        patch.dict(
            os.environ,
            {
                "MY_LLAMA_SERVER": "https://remote.llama.lan:8080",
                "MY_LLAMA_CLASSIFIER": "https://remote.classifier.lan:8080/v1",
                "PUBLIC_BASE_URL": "http://localhost:8080",
            },
            clear=False,
        ),
        patch("router.main.config", test_config),
        patch("router.main.router_model_conf", test_config["router"]["router_model"]),
    ):
        s, c = _resolve_llama_endpoints()
        assert s == "https://remote.llama.lan:8080"
        assert c == "https://remote.classifier.lan:8080/v1"

    # 2. Custom HTTP server/classifier and pytest not in sys.modules fallback
    test_config2 = {
        "llama_server_url": "http://plain.lan:8080",
        "router": {"router_model": {"api_base": "http://plain.lan:8080/v1"}},
    }
    with (
        patch.dict(os.environ, {"PUBLIC_BASE_URL": ""}, clear=False),
        patch("router.main.config", test_config2),
        patch("router.main.router_model_conf", test_config2["router"]["router_model"]),
    ):
        s, c = _resolve_llama_endpoints()
        assert s == "http://plain.lan:8080"
        assert c == "http://plain.lan:8080/v1"

    # 3. Empty raw URLs falling back when pytest not in sys.modules
    test_config3 = {
        "llama_server_url": "",
        "router": {"router_model": {"api_base": ""}},
    }
    with (
        patch.dict(os.environ, {"LLAMA_SERVER_URL": "", "LLAMA_CLASSIFIER_URL": ""}, clear=False),
        patch("router.main.config", test_config3),
        patch("router.main.router_model_conf", test_config3["router"]["router_model"]),
        patch.dict(sys.modules, {"pytest": None}),
    ):
        s, c = _resolve_llama_endpoints()
        assert s == "http://127.0.0.1:8080"
        assert c == "http://127.0.0.1:8080/v1"


# ---------------------------------------------------------------------------
# 6. _atomic_write_json_sync & _atomic_write_json_async
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_atomic_write_json_branches():
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "test.json")

        # 1. String serialization
        _atomic_write_json_sync(test_file, '{"hello": "world"}')
        with open(test_file) as f:
            assert json.load(f) == {"hello": "world"}

        # 2. Exception in os.fdopen
        with patch("os.fdopen", side_effect=RuntimeError("fdopen failed")):
            with pytest.raises(RuntimeError):
                _atomic_write_json_sync(test_file, {"a": 1})

        # 3. Failure during write and os.unlink raises OSError
        with (
            patch("json.dump", side_effect=ValueError("json fail")),
            patch("os.unlink", side_effect=OSError("unlink fail")),
        ):
            with pytest.raises(ValueError):
                _atomic_write_json_sync(test_file, {"b": 2})

        # 4. _atomic_write_json_async with scalar data
        await _atomic_write_json_async(test_file, 12345)
        with open(test_file) as f:
            assert json.load(f) == 12345


# ---------------------------------------------------------------------------
# 7. _periodic_triage_cache_cleanup
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_periodic_triage_cache_cleanup_branches():
    call_count = 0

    def mock_cleanup():
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("cleanup error")

    async def fake_sleep(sec):
        if call_count >= 2:
            raise asyncio.CancelledError()

    with (
        patch("router.main.cleanup_triage_cache", side_effect=mock_cleanup),
        patch("asyncio.sleep", side_effect=fake_sleep),
    ):
        with pytest.raises(asyncio.CancelledError):
            await _periodic_triage_cache_cleanup()


# ---------------------------------------------------------------------------
# 8. OpenRouter / Ollama model DB registration
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_model_db_registration_branches():
    # 1. _register_openrouter_models_in_db with non-dict and non-matching models
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".yaml") as f:
        f.write("model_list:\n  - not_a_dict\n  - model_name: other-model\n")
        tmp_cfg = f.name

    try:
        mock_client = AsyncMock()
        with (
            patch.dict(os.environ, {"LITELLM_CONFIG_PATH": tmp_cfg}),
            patch("router.main.get_http_client", return_value=mock_client),
        ):
            await _register_openrouter_models_in_db("master-key")

        # Test POST failures: HTTP 400 and exception
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".yaml") as f2:
            f2.write("model_list:\n  - model_name: openrouter-test\n    litellm_params: {model: openrouter/test}\n")
            tmp_cfg2 = f2.name

        resp_400 = MagicMock()
        resp_400.status_code = 400
        resp_400.text = "Bad Request"
        mock_client.post.side_effect = [resp_400, RuntimeError("network failure")]

        with (
            patch.dict(os.environ, {"LITELLM_CONFIG_PATH": tmp_cfg2}),
            patch("router.main.get_http_client", return_value=mock_client),
        ):
            await _register_openrouter_models_in_db("master-key")
            await _register_openrouter_models_in_db("master-key")
    finally:
        if os.path.exists(tmp_cfg):
            os.unlink(tmp_cfg)
        if "tmp_cfg2" in locals() and os.path.exists(tmp_cfg2):
            os.unlink(tmp_cfg2)

    # 2. _register_ollama_models_in_db with non-dict and non-matching models
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".yaml") as f3:
        f3.write("model_list:\n  - not_a_dict\n  - model_name: other-model\n")
        tmp_cfg3 = f3.name

    try:
        mock_client = AsyncMock()
        with (
            patch.dict(os.environ, {"LITELLM_CONFIG_PATH": tmp_cfg3}),
            patch("router.main.get_http_client", return_value=mock_client),
        ):
            await _register_ollama_models_in_db("master-key")
    finally:
        if os.path.exists(tmp_cfg3):
            os.unlink(tmp_cfg3)


@pytest.mark.asyncio
async def test_sync_adaptive_router_roster_cascade_branch():
    mock_client = AsyncMock()
    mock_client.post.return_value = MagicMock(status_code=200)

    fake_models_data = [
        {
            "id": "model-adv",
            "score": 90.0,
            "has_tools": True,
            "context_length": 128000,
            "supported_parameters": ["tools"],
        },
        {
            "id": "model-adv",
            "score": 90.0,
            "has_tools": True,
            "context_length": 128000,
            "supported_parameters": ["tools"],
        },
        {
            "id": "model-reasoning",
            "score": 76.0,
            "has_tools": True,
            "context_length": 128000,
            "supported_parameters": ["tools"],
        },
        {
            "id": "model-reasoning",
            "score": 76.0,
            "has_tools": True,
            "context_length": 128000,
            "supported_parameters": ["tools"],
        },
        {
            "id": "model-complex",
            "score": 69.0,
            "has_tools": True,
            "context_length": 128000,
            "supported_parameters": ["tools"],
        },
        {
            "id": "model-complex",
            "score": 69.0,
            "has_tools": True,
            "context_length": 128000,
            "supported_parameters": ["tools"],
        },
    ]
    with (
        patch("router.main.get_http_client", return_value=mock_client),
        patch("router.main._fetch_openrouter_free_models", return_value=fake_models_data),
    ):
        await sync_adaptive_router_roster("test-key")


# ---------------------------------------------------------------------------
# 9. Lifespan startup and shutdown branches
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_lifespan_error_branches():
    import router.main as rm

    # 1. _periodic_model_sync exception
    sleep_count = 0

    async def fake_sleep(sec):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count > 1:
            raise asyncio.CancelledError()

    with (
        patch.dict("os.environ", {"LITELLM_MASTER_KEY": "test-key"}),
        patch("asyncio.sleep", side_effect=fake_sleep),
        patch("router.main.ModelRegistrySync.sync_all_models", side_effect=RuntimeError("sync fail")),
    ):
        await _periodic_model_sync()

    # 2. lifespan readiness timeout ValueError, readiness, subtask errors, shutdown errors
    mock_client = AsyncMock()
    readiness_ok = MagicMock(status_code=200)
    mock_client.get.return_value = readiness_ok
    mock_client.aclose.side_effect = RuntimeError("aclose error")

    mock_classifier = AsyncMock()
    mock_classifier.aclose.side_effect = RuntimeError("classifier aclose error")

    mock_redis = AsyncMock()
    mock_redis.aclose.side_effect = RuntimeError("redis aclose error")

    with (
        patch.dict(
            os.environ,
            {"LITELLM_READINESS_TIMEOUT": "invalid_num", "LITELLM_MASTER_KEY": "test-key"},
            clear=False,
        ),
        patch("router.main.get_http_client", return_value=mock_client),
        patch("router.main.sync_stats_from_valkey", new=AsyncMock()),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        patch("router.main.push_aggregate_scores", new=AsyncMock()),
        patch("router.main._periodic_triage_cache_cleanup", new=AsyncMock()),
        patch("router.main._periodic_model_sync", new=AsyncMock()),
        patch("router.main.ModelRegistrySync.sync_all_models", side_effect=RuntimeError("sync fail")),
        patch("router.main.sync_adaptive_router_roster", side_effect=RuntimeError("roster fail")),
        patch("router.main._register_langfuse_models_in_db", side_effect=RuntimeError("langfuse fail")),
        patch("router.main._atomic_write_json_async", side_effect=RuntimeError("timeline fail")),
        patch("router.main._classifier_client", mock_classifier),
        patch("router.main._redis_client", mock_redis),
    ):
        async with lifespan(app):
            pass


# ---------------------------------------------------------------------------
# 10. Health check functions
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_health_check_branches():
    mock_client = AsyncMock()
    # 500 status code
    mock_client.get.return_value = MagicMock(status_code=500)
    with patch("router.main.get_http_client", return_value=mock_client):
        assert await check_http_endpoint("http://example.com") is False

    # Exception
    mock_client.get.side_effect = RuntimeError("network error")
    with patch("router.main.get_http_client", return_value=mock_client):
        assert await check_http_endpoint("http://example.com") is False

    # _check_llama_health exception
    mock_llama = AsyncMock()
    mock_llama.get.side_effect = RuntimeError("llama error")
    with patch("router.main.get_llama_client", return_value=mock_llama):
        assert await _check_llama_health() is False


# ---------------------------------------------------------------------------
# 11. classify_request branches
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_classify_request_branches():
    import router.main as rm

    # 1. Outer cache hit
    rm.triage_cache["hit-outer"] = ("agent-simple-core", time.time())
    dec, lat, hit, _ = await classify_request("hit-outer", bypass_cache=False)
    assert dec == "agent-simple-core"
    assert hit is True

    # 2. Inner cache hit (simulate concurrent populate while waiting for lock)
    rm.triage_cache.pop("hit-inner", None)
    orig_acquire = rm.classification_lock.acquire

    async def fake_acquire():
        res = await orig_acquire()
        rm.triage_cache["hit-inner"] = ("agent-medium-core", time.time())
        return res

    with patch.object(rm.classification_lock, "acquire", side_effect=fake_acquire):
        dec, lat, hit, _ = await classify_request("hit-inner", bypass_cache=False)
        assert dec == "agent-medium-core"
        assert hit is True

    # 3. Langfuse trace ID provided but get_langfuse() is None, and content not in valid_tiers
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"choices": [{"message": {"content": "unknown-tier"}}]}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp

    with (
        patch("router.main.get_classifier_client", return_value=mock_client),
        patch("router.main.get_langfuse", return_value=None),
    ):
        dec, lat, hit, _ = await classify_request("test invalid tier", bypass_cache=True, langfuse_trace_id="tr-123")
        assert dec == "agent-advanced-core"

    # 4. Cache cap eviction
    for i in range(MAX_TRIAGE_CACHE_SIZE + 5):
        rm.triage_cache[f"prompt-{i}"] = ("agent-simple-core", time.time())
    with patch("router.main.get_classifier_client", return_value=mock_client):
        await classify_request("new prompt to trigger evict", bypass_cache=True)


# ---------------------------------------------------------------------------
# 12. OAuth token info and status
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_oauth_token_info_branches():
    # Non-dict
    assert _parse_oauth_token_info("not-a-dict") == (None, 0)

    # Expiry with "Z"
    tok, exp = _parse_oauth_token_info({"access_token": "abc", "expiry_date": "2030-01-01T00:00:00Z"})
    assert tok == "abc"
    assert exp > 0

    # Invalid string expiry
    tok2, exp2 = _parse_oauth_token_info({"access_token": "abc", "expiry_date": "invalid-date"})
    assert exp2 == 0

    # get_gemini_oauth_status with expiry_ms == 0
    with (
        patch("os.path.exists", return_value=True),
        patch("router.main._read_json_file_async", return_value={"access_token": "abc", "expiry_date": 0}),
    ):
        status = await get_gemini_oauth_status()
        assert status["status"] == "valid"
        assert status["detail"] == "OAuth token active"


# ---------------------------------------------------------------------------
# 13. Tool detection branches
# ---------------------------------------------------------------------------
def test_tool_detection_branches():
    # Names with __
    assert map_tool_to_category("prefix__tree_action") == "tree"
    assert map_tool_to_category("prefix__shell_exec") == "shell"

    # detect_active_tool: role tool, no name, no tool_call_id
    body1 = {"messages": [{"role": "tool", "content": "done"}]}
    assert detect_active_tool(body1) == "other"

    # detect_active_tool: role tool, tool_call_id present, previous msg is user
    body2 = {
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "call_123", "content": "res"},
        ]
    }
    assert detect_active_tool(body2) == "other"

    # detect_active_tool: role assistant, tool_calls is empty list
    body3 = {
        "messages": [
            {"role": "assistant", "tool_calls": []},
        ]
    }
    assert detect_active_tool(body3) == "none"


# ---------------------------------------------------------------------------
# 14. record_tool_usage branches
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_record_tool_usage_branches():
    import router.main as rm

    rm.stats.pop("routing_paths", None)
    u = ToolUsageRecord(
        tool_name="shell",
        prompt_tokens=10,
        completion_tokens=20,
        model="test-model",
        latency_ms=100.0,
        route="google_oauth_direct",
    )

    # 1. Running loop: done_callback exception handling
    fut = asyncio.get_running_loop().create_future()
    fut.set_exception(RuntimeError("disk thread error"))
    with patch("asyncio.get_running_loop") as mock_loop_fn:
        mock_loop = MagicMock()
        mock_loop.create_task.return_value = MagicMock()
        mock_loop.run_in_executor.return_value = fut
        mock_loop_fn.return_value = mock_loop
        record_tool_usage._last_save = 0.0
        record_tool_usage(u)

    # 2. No running event loop (RuntimeError), and _atomic_write_json_sync raises
    rm._last_stats_save = 0.0
    record_tool_usage._last_save = 0.0
    with (
        patch("asyncio.get_running_loop", side_effect=RuntimeError("no loop")),
        patch("router.main._atomic_write_json_sync", side_effect=RuntimeError("sync write error")),
    ):
        record_tool_usage(u)

    # 3. loop.run_in_executor raises Exception
    with patch("asyncio.get_running_loop") as mock_loop_fn:
        mock_loop = MagicMock()
        mock_loop.create_task.return_value = MagicMock()
        mock_loop.run_in_executor.side_effect = Exception("executor failed")
        mock_loop_fn.return_value = mock_loop
        record_tool_usage._last_save = 0.0
        record_tool_usage(u)


# ---------------------------------------------------------------------------
# 15. get_llamacpp_metrics branches
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_llamacpp_metrics_branches():
    import router.main as rm

    rm.llamacpp_metrics_cache["last_fetched"] = 0.0
    mock_client = AsyncMock()

    # Models 500 error
    mock_client.get.side_effect = [
        MagicMock(status_code=500),
        MagicMock(status_code=200, json=lambda: {"build_info": "b1"}),
    ]
    with patch("router.main.get_llama_client", return_value=mock_client):
        res = await get_llamacpp_metrics()
        assert res["build"] == "b1"

    # Slots with next_token not dict and not list, or list with non-dict
    rm.llamacpp_metrics_cache["last_fetched"] = 0.0
    mock_client.get.side_effect = [
        MagicMock(status_code=200, json=lambda: {"data": [{"id": "m1", "status": {"value": "loaded"}}]}),
        MagicMock(status_code=200, json=lambda: {"build_info": "b2"}),
        MagicMock(
            status_code=200,
            json=lambda: [
                {"id": 1, "next_token": 123},
                {"id": 2, "next_token": ["non-dict-token"]},
            ],
        ),
    ]
    with patch("router.main.get_llama_client", return_value=mock_client):
        res2 = await get_llamacpp_metrics()
        assert len(res2["slots"]) == 2
        assert res2["slots"][0]["n_decoded"] == 0
        assert res2["slots"][1]["n_decoded"] == 0


# ---------------------------------------------------------------------------
# 16. get_best_free_model branches & pie chart
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_best_free_model_branches():
    import router.main as rm

    # _get_router_output_dir when CONFIG_PATH is empty
    with patch("router.main.CONFIG_PATH", ""):
        assert _get_router_output_dir() == "/config/router_dir"

    # _atomic_save_json call
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        tmp_name = f.name
    try:
        _atomic_save_json(tmp_name, {"test": 1})
        with open(tmp_name) as f:
            assert json.load(f) == {"test": 1}
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

    # get_best_free_model cache hit
    rm.free_model_cache["data"] = {"id": "cached-model", "score": 90.0}
    rm.free_model_cache["last_fetched"] = time.time()
    with patch("router.main._save_best_model_to_disk"):
        best = await get_best_free_model()
        assert best["id"] == "cached-model"

    # get_best_free_model fresh fetch success
    rm.free_model_cache["data"] = None
    mock_models = [
        {"id": "fresh-1", "name": "Fresh 1", "score": 85.0, "context_length": 100000, "has_tools": True},
    ]
    with (
        patch("router.main._fetch_openrouter_free_models", return_value=mock_models),
        patch("router.main._save_free_models_roster"),
        patch("router.main._save_best_model_to_disk"),
    ):
        best_fresh = await get_best_free_model()
        assert best_fresh["id"] == "fresh-1"

    # get_best_free_model exception -> fallback
    rm.free_model_cache["data"] = None
    with (
        patch("router.main._fetch_openrouter_free_models", side_effect=RuntimeError("openrouter down")),
        patch("router.main._save_best_model_to_disk"),
    ):
        best_fallback = await get_best_free_model()
        assert best_fallback["is_fallback"] is True

    # get_pie_chart_gradient when gradient_parts is empty
    rm.stats["tool_tokens"] = {"tree": -5}
    assert get_pie_chart_gradient() == "background: rgba(255, 255, 255, 0.05);"


# ---------------------------------------------------------------------------
# 17. Proxy memory, audio, and models branches
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_memory_audio_models_proxy_branches():
    import router.main as rm

    # Memory proxy netloc mismatch
    req_bad_netloc = MagicMock()
    req_bad_netloc.headers = {}
    req_bad_netloc.body = AsyncMock(return_value=b"")
    with patch("router.main.urlparse", return_value=MagicMock(netloc="attacker.com")):
        with pytest.raises(HTTPException) as exc:
            await proxy_memory(req_bad_netloc, "/evil")
        assert exc.value.status_code == 400

    # Memory proxy client exception
    req_mem = MagicMock()
    req_mem.query_params = {}
    req_mem.method = "GET"
    req_mem.headers = {"Authorization": "Bearer test-key"}
    req_mem.body = AsyncMock(return_value=b"")
    mock_client = AsyncMock()
    mock_client.request.side_effect = RuntimeError("memory down")
    with patch("router.main.get_http_client", return_value=mock_client):
        with pytest.raises(HTTPException) as exc:
            await proxy_memory(req_mem, "/status")
        assert exc.value.status_code == 502

    # Audio proxy netloc mismatch
    with patch("router.main.urlparse", return_value=MagicMock(netloc="attacker.com")):
        with pytest.raises(HTTPException) as exc:
            await proxy_audio(req_bad_netloc, "/evil")
        assert exc.value.status_code == 400

    # Audio proxy client exception with valid Bearer header
    req_audio = MagicMock()
    req_audio.query_params = {}
    req_audio.method = "POST"
    req_audio.headers = {"Authorization": "Bearer valid-token"}
    req_audio.body = AsyncMock(return_value=b"")
    with patch("router.main.get_http_client", return_value=mock_client):
        with pytest.raises(HTTPException) as exc:
            await proxy_audio(req_audio, "/transcriptions")
        assert exc.value.status_code == 502

    # proxy_models status 200 but invalid JSON
    mock_models_resp = MagicMock()
    mock_models_resp.status_code = 200
    mock_models_resp.json.side_effect = ValueError("invalid json")
    mock_models_resp.content = b"not json"
    mock_models_resp.headers = {}
    mock_client_models = AsyncMock()
    mock_client_models.get.return_value = mock_models_resp
    with patch("router.main.get_http_client", return_value=mock_client_models):
        resp = await proxy_models()
        assert resp.status_code == 200
        assert resp.body == b"not json"

    # proxy_models client.get raises Exception
    mock_client_models.get.side_effect = RuntimeError("models failed")
    with patch("router.main.get_http_client", return_value=mock_client_models):
        with pytest.raises(HTTPException) as exc:
            await proxy_models()
        assert exc.value.status_code == 502

    # _validate_litellm_virtual_key branches
    assert await _validate_litellm_virtual_key("") is None
    assert await _validate_litellm_virtual_key("not-sk") is None

    # Cached virtual key expired
    rm._VIRTUAL_KEY_CACHE["sk-old"] = (time.time() - 400.0, {"key": "old"})
    with patch.dict(os.environ, {"LITELLM_MASTER_KEY": ""}):
        assert await _validate_litellm_virtual_key("sk-old") is None

    # Virtual key info blocked
    mock_key_resp = MagicMock()
    mock_key_resp.status_code = 200
    mock_key_resp.json.return_value = {"info": {"blocked": True}}
    mock_client_vk = AsyncMock()
    mock_client_vk.get.return_value = mock_key_resp
    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "master"}),
        patch("router.main.get_http_client", return_value=mock_client_vk),
    ):
        assert await _validate_litellm_virtual_key("sk-blocked") is None

    # Virtual key lookup raises Exception
    mock_client_vk.get.side_effect = RuntimeError("vk error")
    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "master"}),
        patch("router.main.get_http_client", return_value=mock_client_vk),
    ):
        assert await _validate_litellm_virtual_key("sk-err") is None

    # _authenticate_client_request with empty token
    req_empty_tok = MagicMock()
    req_empty_tok.headers = {"Authorization": "Bearer   "}
    with pytest.raises(HTTPException) as exc:
        await _authenticate_client_request(req_empty_tok)
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# 18. Responses API branches
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_responses_api_full_branches():
    with patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-master-key"}):
        await _run_responses_api_full_branches()


async def _run_responses_api_full_branches():
    test_inputs = [
        ["   ", "valid text"],
        [{"role": "user", "content": ["str1", {"type": "input_text", "text": "str2"}]}],
        [{"type": "input_text", "text": "block text"}],
    ]

    mock_client = AsyncMock()
    mock_resp = MagicMock(status_code=200, content=b'{"id":"resp1"}', headers={})
    mock_client.post.return_value = mock_resp

    for inp in test_inputs:
        req = MagicMock()
        req.headers = {"Authorization": "Bearer test-key"}
        req.json = AsyncMock(return_value={"model": "gpt-4o-mini", "input": inp})
        with (
            patch("router.main._authenticate_client_request", new=AsyncMock()),
            patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
            patch("router.main.get_http_client", return_value=mock_client),
        ):
            res = await responses_api(req)
            assert res.status_code == 200

    # Instructions fallback
    req_inst = MagicMock()
    req_inst.headers = {"Authorization": "Bearer test-key"}
    req_inst.json = AsyncMock(return_value={"model": "gpt-4o-mini", "instructions": "test instructions"})
    with (
        patch("router.main._authenticate_client_request", new=AsyncMock()),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        patch("router.main.get_http_client", return_value=mock_client),
    ):
        res = await responses_api(req_inst)
        assert res.status_code == 200

    # Streaming SSE parser: delta, done, unparseable SSE line, trailing buffer
    sse_chunks = [
        b'data: {"type": "response.function_call_arguments.delta", "item_id": "item1"}\n\n',
        b'data: {"type": "response.function_call_arguments.done", "item_id": "item1"}\n\n',
        b"data: invalid-json-line\n\n",
        b"data: trailing-buffer-without-newline",
    ]

    class FakeAsyncIter:
        def __init__(self, items):
            self.items = items

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.items:
                raise StopAsyncIteration
            return self.items.pop(0)

    mock_stream_resp = MagicMock()
    mock_stream_resp.status_code = 200
    mock_stream_resp.aiter_bytes = lambda: FakeAsyncIter(list(sse_chunks))
    mock_stream_resp.aclose = AsyncMock()
    mock_client.send.return_value = mock_stream_resp

    req_stream = MagicMock()
    req_stream.headers = {"Authorization": "Bearer test-key"}
    req_stream.json = AsyncMock(return_value={"model": "gpt-4o-mini", "input": "hi", "stream": True})

    with (
        patch("router.main._authenticate_client_request", new=AsyncMock()),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        patch("router.main.get_http_client", return_value=mock_client),
    ):
        res = await responses_api(req_stream)
        assert isinstance(res, StreamingResponse)
        async for _ in res.body_iterator:
            pass

    # Non-streaming post raises Exception
    mock_client.post.side_effect = RuntimeError("post error")
    with (
        patch("router.main._authenticate_client_request", new=AsyncMock()),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        patch("router.main.get_http_client", return_value=mock_client),
    ):
        with pytest.raises(HTTPException) as exc:
            await responses_api(req_inst)
        assert exc.value.status_code == 502


# ---------------------------------------------------------------------------
# 19. maybe_trigger_roster_sync branch
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_maybe_trigger_roster_sync_branches():
    import router.main as rm

    rm._last_roster_sync = time.monotonic() - 400.0

    async def fake_sync(key):
        pass

    with (
        patch("router.main.sync_adaptive_router_roster", side_effect=fake_sync),
        patch("time.monotonic", side_effect=[0.0, 1000.0, 50.0]),
    ):
        await maybe_trigger_roster_sync(force=False)


# ---------------------------------------------------------------------------
# 20. chat_completions full coverage
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chat_completions_error_and_routing_branches():
    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-master-key"}),
        patch("router.main.save_persisted_stats", new=AsyncMock()),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
    ):
        await _run_chat_completions_error_and_routing_branches()


async def _run_chat_completions_error_and_routing_branches():
    import router.main as rm

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Invalid JSON
        r = await client.post(
            "/v1/chat/completions",
            content=b"not json",
            headers={"Authorization": "Bearer test-key", "Content-Type": "application/json"},
        )
        assert r.status_code == 400

        # 2. Empty messages list
        r = await client.post(
            "/v1/chat/completions",
            json={"messages": []},
            headers={"Authorization": "Bearer test-key"},
        )
        assert r.status_code == 400

        # 3. Message parsing: non-dict message, content as list of text blocks, user_id as int
        mock_lite_resp = MagicMock()
        mock_lite_resp.status_code = 200
        mock_lite_resp.json.return_value = {
            "choices": [{"message": {"content": "completed text"}}],
            "usage": {},
        }
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_lite_resp
        with (
            patch("router.main.get_http_client", return_value=mock_http),
            patch("router.main.get_langfuse", return_value=None),
        ):
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "user": 12345,
                    "messages": [
                        "not-a-dict",
                        {"role": "assistant", "content": "hi"},
                        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                    ],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 200

        # 4. Context window exceeded
        with (
            patch("router.main.estimate_prompt_tokens", return_value=300000),
            patch("router.main.get_http_client", return_value=mock_http),
        ):
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "agent-simple-core",
                    "messages": [{"role": "user", "content": "test context window"}],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 400
            assert "Context window exceeded" in r.text

        # 5. Clamping max_tokens and pre-screening exception
        with (
            patch("router.main.estimate_prompt_tokens", side_effect=[100, 100]),
            patch("router.main.get_http_client", return_value=mock_http),
        ):
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "agent-simple-core",
                    "max_tokens": 100000,
                    "messages": [{"role": "user", "content": "clamp tokens"}],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 200

        with (
            patch("router.main.estimate_prompt_tokens", side_effect=[RuntimeError("prescreen error"), 10]),
            patch("router.main.get_http_client", return_value=mock_http),
        ):
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "agent-simple-core",
                    "messages": [{"role": "user", "content": "prescreen error"}],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 200

        # 6. Auto model routing to all 4 other tiers
        tiers = [
            "agent-medium-core",
            "agent-complex-core",
            "agent-reasoning-core",
            "agent-advanced-core",
        ]
        for t in tiers:
            with (
                patch("router.main.classify_request", return_value=(t, 10.0, False, t)),
                patch("router.main.get_http_client", return_value=mock_http),
            ):
                r = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "llm-routing-auto-free",
                        "messages": [{"role": "user", "content": f"test {t}"}],
                    },
                    headers={"Authorization": "Bearer test-key"},
                )
                assert r.status_code == 200

        # 7. Non-streaming LiteLLM 429 status (triggers roster sync) and generic exception
        resp_429 = MagicMock(status_code=429, text="Rate limit")
        mock_http_429 = AsyncMock()
        mock_http_429.post.return_value = resp_429
        with (
            patch("router.main.get_http_client", return_value=mock_http_429),
            patch("router.main.maybe_trigger_roster_sync", new=AsyncMock()) as mock_sync,
        ):
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "agent-simple-core",
                    "messages": [{"role": "user", "content": "test 429"}],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 429
            mock_sync.assert_called_once()

        mock_http_err = AsyncMock()
        mock_http_err.post.side_effect = RuntimeError("httpx fail")
        with patch("router.main.get_http_client", return_value=mock_http_err):
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "agent-simple-core",
                    "messages": [{"role": "user", "content": "test err"}],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 502

        # 8. Ollama routing branches
        ollama_tests = [
            ("llm-routing-auto-ollama", "agent-reasoning-core", "ollama-deepseek-v4-pro"),
            ("llm-routing-auto-ollama", "agent-complex-core", "ollama-deepseek-v4-flash"),
            ("llm-routing-ollama", "agent-simple-core", "ollama-deepseek-v4-flash"),
        ]
        for c_mod, tier, expected_proxy in ollama_tests:
            with (
                patch("router.main.classify_request", return_value=(tier, 5.0, False, tier)),
                patch("router.main.get_http_client", return_value=mock_http),
            ):
                r = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": c_mod,
                        "messages": [{"role": "user", "content": f"test {c_mod}"}],
                    },
                    headers={"Authorization": "Bearer test-key"},
                )
                assert r.status_code == 200

        # Ollama active cooldown
        rm._ollama_cooldown_until = time.monotonic() + 60.0
        with (
            patch("router.main.classify_request", return_value=("agent-advanced-core", 5.0, False, "adv")),
            patch("router.main.get_http_client", return_value=mock_http),
        ):
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "llm-routing-auto-ollama",
                    "messages": [{"role": "user", "content": "auto cooldown"}],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 200

        with patch("router.main.get_http_client", return_value=mock_http):
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "llm-routing-ollama",
                    "messages": [{"role": "user", "content": "direct cooldown"}],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 429
        rm._ollama_cooldown_until = 0.0

        # Ollama failure (transient)
        mock_ollama_fail = AsyncMock()
        resp_500 = MagicMock(status_code=500, text="Internal Server Error")
        mock_ollama_fail.post.side_effect = [resp_500, mock_lite_resp]
        with (
            patch("router.main.classify_request", return_value=("agent-advanced-core", 5.0, False, "adv")),
            patch("router.main.get_http_client", return_value=mock_ollama_fail),
            patch("router.main.save_cooldowns_to_valkey", new=AsyncMock()),
        ):
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "llm-routing-auto-ollama",
                    "messages": [{"role": "user", "content": "ollama fail"}],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 200

        # Direct ollama with transient failure
        mock_ollama_fail_direct = AsyncMock()
        mock_ollama_fail_direct.post.return_value = resp_500
        with (
            patch("router.main.get_http_client", return_value=mock_ollama_fail_direct),
            patch("router.main.save_cooldowns_to_valkey", new=AsyncMock()),
        ):
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "llm-routing-ollama",
                    "messages": [{"role": "user", "content": "direct fail"}],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 429

        # Ollama failure (non-transient 400)
        rm._ollama_cooldown_until = 0.0
        resp_400 = MagicMock(status_code=400, text="Bad Request")
        mock_ollama_400 = AsyncMock()
        mock_ollama_400.post.return_value = resp_400
        with (
            patch("router.main.classify_request", return_value=("agent-advanced-core", 5.0, False, "adv")),
            patch("router.main.get_http_client", return_value=mock_ollama_400),
        ):
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "llm-routing-auto-ollama",
                    "messages": [{"role": "user", "content": "ollama 400"}],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 400

        # Direct ollama 400
        rm._ollama_cooldown_until = 0.0
        with (
            patch("router.main.classify_request", return_value=("agent-advanced-core", 5.0, False, "adv")),
            patch("router.main.get_http_client", return_value=mock_ollama_400),
        ):
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "llm-routing-ollama",
                    "messages": [{"role": "user", "content": "direct 400"}],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 400

        # Ollama unexpected exception
        rm._ollama_cooldown_until = 0.0
        mock_ollama_exc = AsyncMock()
        mock_ollama_exc.post.side_effect = RuntimeError("unexpected ollama crash")
        with (
            patch("router.main.classify_request", return_value=("agent-advanced-core", 5.0, False, "adv")),
            patch("router.main.get_http_client", return_value=mock_ollama_exc),
            patch("router.main.save_cooldowns_to_valkey", new=AsyncMock()),
        ):
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "llm-routing-ollama",
                    "messages": [{"role": "user", "content": "direct crash"}],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 429

        # 9. Streaming branches:
        mock_stream_429 = MagicMock(status_code=429)
        mock_stream_429.aread = AsyncMock(return_value=b"rate limit stream")
        mock_stream_429.aclose = AsyncMock()
        mock_http_stream = AsyncMock()
        mock_http_stream.send.return_value = mock_stream_429

        with (
            patch("router.main.get_http_client", return_value=mock_http_stream),
            patch("router.main.maybe_trigger_roster_sync", new=AsyncMock()) as mock_stream_sync,
        ):
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "agent-simple-core",
                    "stream": True,
                    "messages": [{"role": "user", "content": "stream 429"}],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 429
            mock_stream_sync.assert_called_once()

        class ErrorStreamIter:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError("stream mid-abort")

        mock_stream_ok = MagicMock(status_code=200)
        mock_stream_ok.aiter_bytes = lambda: ErrorStreamIter()
        mock_stream_ok.aclose = AsyncMock()
        mock_http_stream.send.return_value = mock_stream_ok

        rm._ollama_cooldown_until = 0.0
        with (
            patch("router.main.classify_request", return_value=("agent-advanced-core", 5.0, False, "adv")),
            patch("router.main.get_http_client", return_value=mock_http_stream),
            patch("router.main.save_cooldowns_to_valkey", new=AsyncMock()),
        ):
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "llm-routing-ollama",
                    "stream": True,
                    "messages": [{"role": "user", "content": "stream err"}],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 200
            try:
                async for _ in r.aiter_bytes():
                    pass
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 21. Metrics endpoint
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_metrics_endpoint():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch("router.main.sync_stats_from_valkey", new=AsyncMock()),
            patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        ):
            r = await client.get("/metrics")
            assert r.status_code == 200
            assert "triage_requests_total" in r.text
            assert "circuit_breaker_total_trips" in r.text


# ---------------------------------------------------------------------------
# 22. Dashboard and resolve_external_urls branches
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dashboard_and_resolve_external_urls_branches():
    mock_tasks = [
        RuntimeError("valkey down"),
        RuntimeError("litellm down"),
        RuntimeError("llama down"),
        RuntimeError("langfuse down"),
        RuntimeError("oauth down"),
        RuntimeError("model down"),
        RuntimeError("goose down"),
        RuntimeError("llamacpp down"),
    ]

    with (
        patch("router.main.sync_stats_from_valkey", new=AsyncMock()),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        patch("router.main.check_tcp_port", side_effect=mock_tasks[0:1]),
        patch("router.main.check_http_endpoint", side_effect=mock_tasks[1:3]),
        patch("router.main._check_llama_health", side_effect=mock_tasks[2:3]),
        patch("router.main.get_gemini_oauth_status", side_effect=mock_tasks[4:5]),
        patch("router.main.get_best_free_model", side_effect=mock_tasks[5:6]),
        patch("router.main.get_goose_sessions", side_effect=mock_tasks[6:7]),
        patch("router.main.get_llamacpp_metrics", side_effect=mock_tasks[7:8]),
        patch("os.path.exists", return_value=False),
    ):
        data = await get_dashboard_data()
        assert data["valkey_status"] is False
        assert data["litellm_status"] is False
        assert data["oauth_status"]["status"] == "error"
        assert data["best_free_model"]["id"] == "error"

    roster_json = json.dumps(
        {
            "models": [
                {"id": "m-active", "name": "Active Model", "score": 88.0, "context_length": 128000, "has_tools": True},
                {
                    "id": "m-excluded",
                    "name": "Excluded Model",
                    "score": 50.0,
                    "context_length": 32000,
                    "has_tools": False,
                },
            ]
        }
    )
    with (
        patch("os.path.exists", return_value=True),
        patch("aiofiles.open") as mock_open,
        patch("router.main._registered_free_models", {"agent-advanced-core": ["m-active"]}),
    ):
        mock_file = AsyncMock()
        mock_file.read.return_value = roster_json
        mock_open.return_value.__aenter__.return_value = mock_file

        data2 = await get_dashboard_data()
        assert "Active (advanced)" in data2["roster_table_html"]
        assert "Excluded" in data2["roster_table_html"]

    with (
        patch("os.path.exists", return_value=True),
        patch("aiofiles.open", side_effect=RuntimeError("disk error")),
    ):
        data3 = await get_dashboard_data()
        assert "Error loading roster" in data3["roster_table_html"]

    req = MagicMock()
    req.base_url.hostname = "dashboard.vendeuvre.lan"
    req.base_url.netloc = "dashboard.vendeuvre.lan:8443"
    req.url.scheme = "https"

    with patch.dict(os.environ, {"PUBLIC_BASE_URL": "dashboard.vendeuvre.lan:8443"}):
        lf, ll, lm = resolve_external_urls(req)
        assert "vendeuvre.lan" in lf

    with patch.dict(os.environ, {"PUBLIC_BASE_URL": "invalid!@#host.lan"}):
        lf, ll, lm = resolve_external_urls(req)
        assert "vendeuvre.lan" in lf

    with (
        patch.dict(os.environ, {"PUBLIC_BASE_URL": "dashboard.vendeuvre.lan:9999999"}),
    ):
        lf, ll, lm = resolve_external_urls(req)
        assert "vendeuvre.lan" in lf

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch("router.main.get_dashboard_data", return_value=data2),
            patch("router.main.resolve_external_urls", return_value=("http://lf", "http://ll", "http://lm")),
        ):
            r = await client.get("/dashboard")
            assert r.status_code == 200
            assert "html" in r.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# 23. Annotation branches
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_annotation_validation_and_caching():
    huge_data = {str(i): {"tier": "agent-simple-core"} for i in range(1005)}
    with pytest.raises(ValueError, match="maximum of 1000 annotations"):
        AnnotationPayload.model_validate(huge_data)

    long_key = "1" * 130
    with pytest.raises(ValueError, match="key is too long"):
        AnnotationPayload.model_validate({long_key: {"tier": "agent-simple-core"}})

    with pytest.raises(ValueError, match="keys must be numeric strings"):
        AnnotationPayload.model_validate({"invalid_key!": {"tier": "agent-simple-core"}})

    with pytest.raises(ValueError, match="exceeds the maximum serialized size"):
        AnnotationPayload.model_validate({"1": {"note": "😀" * 1000, "ts": "a" * 100, "tier": "agent-advanced-core"}})

    import router.main as rm

    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write('{"1": {"tier": 1}}')
        ann_path = f.name

    try:
        rm._annotations_cache.clear()
        res1 = await _read_annotations_async(ann_path)
        assert res1 == {"1": {"tier": 1}}

        res2 = await _read_annotations_async(ann_path)
        assert res2 == {"1": {"tier": 1}}
    finally:
        if os.path.exists(ann_path):
            os.unlink(ann_path)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 24. Final gaps coverage
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_coverage_final_gaps():
    import router.main as rm

    # 1. extract_or_synthesize_session_id (lines 576->589, 589->594)
    req = MagicMock()
    req.headers = {}
    req.state = MagicMock(auth_key_alias="")
    sess_id = extract_or_synthesize_session_id({"messages": [{"role": "assistant", "content": "hi"}]}, req)
    assert sess_id.startswith("sess-")

    # 2. _resolve_llama_endpoints fallback logging (lines 734-735, 749-750)
    with patch.dict(sys.modules):
        sys.modules.pop("pytest", None)
        with patch.dict(os.environ, {"LLAMA_SERVER_URL": "", "LLAMA_CLASSIFIER_URL": ""}, clear=False):
            srv, clf = _resolve_llama_endpoints()
            assert "8080" in srv
            assert "8080" in clf

    # 3. _register_openrouter_models_in_db & _register_ollama_models_in_db with empty config path (lines 1161->1160, 1314->1313)
    with patch.dict(os.environ, {"LITELLM_CONFIG_PATH": ""}):
        mock_client = AsyncMock()
        mock_client.post.return_value = MagicMock(status_code=200)
        with patch("router.main.get_http_client", return_value=mock_client):
            await _register_openrouter_models_in_db("master-key")
            await _register_ollama_models_in_db("master-key")

    # 4. _periodic_model_sync without master key (line 1612->1608)
    sleep_cnt = 0

    async def fake_sleep_no_key(sec):
        nonlocal sleep_cnt
        sleep_cnt += 1
        if sleep_cnt > 1:
            raise asyncio.CancelledError()

    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": ""}),
        patch("asyncio.sleep", side_effect=fake_sleep_no_key),
    ):
        await _periodic_model_sync()

    # 5. lifespan: readiness non-200 then 200 (line 1656->1662), and _http_client aclose error (lines 1727-1728)
    mock_http_ls = AsyncMock()
    mock_http_ls.get.side_effect = [MagicMock(status_code=503), MagicMock(status_code=200)]
    mock_http_ls.aclose.side_effect = RuntimeError("http aclose error")
    with (
        patch.dict(os.environ, {"LITELLM_READINESS_TIMEOUT": "5", "LITELLM_MASTER_KEY": "test-key"}),
        patch("router.main.get_http_client", return_value=mock_http_ls),
        patch("router.main.sync_stats_from_valkey", new=AsyncMock()),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        patch("router.main.push_aggregate_scores", new=AsyncMock()),
        patch("router.main._periodic_triage_cache_cleanup", new=AsyncMock()),
        patch("router.main._periodic_model_sync", new=AsyncMock()),
        patch("asyncio.sleep", new=AsyncMock()),
        patch("router.main.ModelRegistrySync.sync_all_models", new=AsyncMock()),
        patch("router.main.sync_adaptive_router_roster", new=AsyncMock()),
        patch("router.main._register_langfuse_models_in_db", new=AsyncMock()),
    ):
        rm._http_client = mock_http_ls
        async with lifespan(app):
            pass

    # 6. classify_request expired cache check (lines 1825->1833, 1840->1848)
    rm.triage_cache["expired prompt"] = ("agent-simple-core", time.time() - 100000)
    mock_class_client = AsyncMock()
    mock_class_client.post.return_value = MagicMock(
        status_code=200, json=lambda: {"choices": [{"message": {"content": "agent-simple-core"}}]}
    )
    with patch("router.main.get_classifier_client", return_value=mock_class_client):
        await classify_request("expired prompt")

    # 7. detect_active_tool assistant non-dict tool_call (line 2096->2064)
    b_tc = {"messages": [{"role": "assistant", "tool_calls": ["not-dict"]}]}
    assert detect_active_tool(b_tc) == "none"

    # 8. record_tool_usage throttled stats save and success branch (lines 2178->2185, 2180, 2209)
    u_rec = ToolUsageRecord(
        tool_name="tree",
        prompt_tokens=5,
        completion_tokens=5,
        model="test-m",
        latency_ms=50.0,
        route="litellm_fallback",
    )
    with (
        patch("asyncio.get_running_loop", side_effect=RuntimeError("no loop")),
        patch("router.main._atomic_write_json_sync", return_value=None),
    ):
        rm._last_stats_save = 0.0
        record_tool_usage._last_save = 0.0
        record_tool_usage(u_rec)
        # Call immediately again to hit throttle branch (2178->2185)
        record_tool_usage(u_rec)

    # 9. _get_router_output_dir (2424->2426) & get_best_free_model empty models (2482->2511)
    with patch("router.main.CONFIG_PATH", "config.yaml"):
        assert _get_router_output_dir() == "/config/router_dir"

    rm.free_model_cache["data"] = None
    with (
        patch("router.main._fetch_openrouter_free_models", return_value=[]),
        patch("router.main._save_best_model_to_disk"),
    ):
        best_empty = await get_best_free_model()
        assert best_empty["is_fallback"] is True

    # 10. proxy_models non-200 (2712->2766) and 200 without data key (2715->2766)
    mock_m_client = AsyncMock()
    mock_m_client.get.return_value = MagicMock(status_code=500, content=b"error", headers={})
    with patch("router.main.get_http_client", return_value=mock_m_client):
        res_500 = await proxy_models()
        assert res_500.status_code == 500

    mock_m_client.get.return_value = MagicMock(status_code=200, json=lambda: {"no_data": 1}, content=b"{}", headers={})
    with patch("router.main.get_http_client", return_value=mock_m_client):
        res_nodata = await proxy_models()
        assert res_nodata.status_code == 200

    # 11. responses_api input parsing branches & SSE streaming branches
    mock_sse_client = AsyncMock()
    sse_test_chunks = [
        b'data: {"type": "response.function_call_arguments.delta"}\n\n',
        b'data: {"type": "response.function_call_arguments.done"}\n\n',
        b'data: {"type": "response.output_item.done", "item": {"type": "message"}}\n\n',
        b'data: {"type": "response.output_item.done", "item": {"type": "function_call", "id": "fc1", "arguments": "{\\"a\\":1}"}}\n\n',
        b'data: {"type": "response.output_item.done", "item": {"type": "function_call", "id": "fc1", "arguments": "{\\"a\\":1}"}}\n\n',
    ]

    class MockSSEStream:
        def __init__(self, chunks, raise_error=False):
            self.chunks = list(chunks)
            self.raise_error = raise_error

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.raise_error:
                raise RuntimeError("mid-stream failure")
            if not self.chunks:
                raise StopAsyncIteration
            return self.chunks.pop(0)

    stream_resp = MagicMock(status_code=200, headers={"content-type": "text/event-stream"})
    stream_resp.aiter_bytes = lambda: MockSSEStream(sse_test_chunks)
    stream_resp.aclose = AsyncMock()
    mock_sse_client.send.return_value = stream_resp

    inputs_to_test = [
        [],  # 2914->2943
        ["   ", 123, {"role": "assistant"}, {"type": "text", "text": ""}, {"role": "user", "content": 123}],
        [{"role": "user", "content": "simple string content"}],  # 2925
        [
            {"role": "user", "content": ["str1", {"type": "other"}, {"type": "text", "text": ""}]}
        ],  # 2931->2928, 2933->2928
        [{"role": "user", "content": ["   "]}],  # 2936->2914
        [{"type": "text", "text": "   "}],  # 2940->2914
    ]
    for inp in inputs_to_test:
        req_inp = MagicMock()
        req_inp.headers = {"Authorization": "Bearer test-key"}
        req_inp.json = AsyncMock(
            return_value={"model": "gpt-4o-mini", "input": inp, "stream": True, "instructions": 123}
        )
        with (
            patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-master-key"}),
            patch("router.main._authenticate_client_request", new=AsyncMock()),
            patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
            patch("router.main.get_http_client", return_value=mock_sse_client),
        ):
            res = await responses_api(req_inp)
            assert res.status_code == 200
            async for _ in res.body_iterator:
                pass

    # SSE mid-stream exception (3040-3042)
    err_resp = MagicMock(status_code=200, headers={"content-type": "text/event-stream"})
    err_resp.aiter_bytes = lambda: MockSSEStream([], raise_error=True)
    err_resp.aclose = AsyncMock()
    mock_sse_client.send.return_value = err_resp
    req_err = MagicMock()
    req_err.headers = {"Authorization": "Bearer test-key"}
    req_err.json = AsyncMock(return_value={"model": "gpt-4o-mini", "input": "hi", "stream": True})
    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-master-key"}),
        patch("router.main._authenticate_client_request", new=AsyncMock()),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        patch("router.main.get_http_client", return_value=mock_sse_client),
    ):
        res = await responses_api(req_err)
        with pytest.raises(RuntimeError, match="mid-stream failure"):
            async for _ in res.body_iterator:
                pass

    # 12. maybe_trigger_roster_sync monotonic check inside lock (line 3090)
    rm._last_roster_sync = 0.0
    with (
        patch("router.main._roster_sync_lock.locked", return_value=False),
        patch("time.monotonic", side_effect=[1000.0, 50.0]),
    ):
        await maybe_trigger_roster_sync(force=False)

    # 13. chat_completions additional branches
    mock_lite_success = MagicMock(status_code=200)
    mock_lite_success.json.return_value = {
        "choices": [{"message": {"content": "reply"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    # Stream full consumption with edge SSE chunks
    sse_stream_chunks = [
        bytes([255, 255]),
        b'data: {"choices": []}\n\n',
        b'data: {"choices": [{"delta": null}]}\n\n',
        b"data: not-json\n\n",
        b'data: {"choices": [{"delta": {"content": "chunk1"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    mock_stream_200 = MagicMock(status_code=200)
    mock_stream_200.aiter_bytes = lambda: MockSSEStream(sse_stream_chunks)
    mock_stream_200.aclose = AsyncMock()

    mock_http_chat = AsyncMock()
    mock_http_chat.post.return_value = mock_lite_success
    mock_http_chat.send.return_value = mock_stream_200

    # Mock Langfuse with errors
    mock_lf = MagicMock()
    mock_obs = MagicMock()
    mock_obs.id = "obs-1"
    mock_obs.update.side_effect = RuntimeError("obs update error")
    mock_lf.start_observation.side_effect = [mock_obs, RuntimeError("litellm span error")]

    # Set custom backend api_key
    rm.backends["custom_model"] = {"api_base": "http://127.0.0.1:4000/v1", "api_key": "custom-secret-key"}

    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-master-key"}),
        patch("router.main.get_http_client", return_value=mock_http_chat),
        patch("router.main.save_persisted_stats", new=AsyncMock()),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        patch("router.main.get_langfuse", return_value=mock_lf),
    ):
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # chat with no user message, custom_model, user id
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "custom_model",
                    "user": "user-123",
                    "messages": [123, {"role": "assistant", "content": "hi"}],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 200

            # chat with empty session_id, metadata dict, auth key alias (covers 3178->3180, 3184->3186, 3190, 3255->3257, 3384->3396, 3399->3401, 3474, 3476->3478, 3611->3613)
            async def fake_auth_with_alias(request):
                request.state.auth_key_alias = "test-alias"

            mock_fresh_lf = MagicMock()
            mock_fresh_obs = MagicMock()
            mock_fresh_lf.start_observation.return_value = mock_fresh_obs

            with (
                patch("router.main.extract_or_synthesize_session_id", return_value=""),
                patch("router.main._authenticate_client_request", side_effect=fake_auth_with_alias),
                patch(
                    "router.main.get_langfuse",
                    side_effect=[mock_fresh_lf, mock_fresh_lf, None, mock_fresh_lf, mock_fresh_lf, None],
                ),
            ):
                mock_http_chat.post.side_effect = None
                mock_http_chat.post.return_value = mock_lite_success
                r = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "llm-routing-auto-free",
                        "messages": [{"role": "user", "content": "metadata test"}],
                        "metadata": {"existing": True},
                    },
                    headers={"Authorization": "Bearer test-key"},
                )
                assert r.status_code == 200

                # Streaming with empty session_id -> covers 3611->3613
                mock_http_chat.send.return_value = mock_stream_200
                r = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "llm-routing-auto-free",
                        "stream": True,
                        "messages": [{"role": "user", "content": "stream no sess"}],
                    },
                    headers={"Authorization": "Bearer test-key"},
                )
                assert r.status_code == 200
                await r.aread()

            # Non-streaming missing choices or message (3650->3654, 3652->3654)
            mock_no_choices = MagicMock(status_code=200)
            mock_no_choices.json.return_value = {"choices": []}
            mock_http_chat.post.return_value = mock_no_choices
            r = await client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "no choices"}]},
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 200

            mock_no_msg = MagicMock(status_code=200)
            mock_no_msg.json.return_value = {"choices": [{"message": None}]}
            mock_http_chat.post.return_value = mock_no_msg
            r = await client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "no msg"}]},
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 200

            # chat with stream=True full read (covers 3510-3566, 3523->3513, 3525->3513, 3528-3531)
            mock_http_chat.send.return_value = mock_stream_200
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "agent-simple-core",
                    "stream": True,
                    "messages": [{"role": "user", "content": "stream all"}],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 200
            content = await r.aread()
            assert b"chunk1" in content

            # chat with stream=True early close (GeneratorExit -> 3598-3607)
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "agent-simple-core",
                    "stream": True,
                    "messages": [{"role": "user", "content": "stream close"}],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            async for _ in r.aiter_bytes():
                break
            await r.aclose()

            # chat with stream 429 on non-agent model (3628->3630)
            mock_stream_429_custom = MagicMock(status_code=429)
            mock_stream_429_custom.aread = AsyncMock(return_value=b"rate limit stream custom")
            mock_stream_429_custom.aclose = AsyncMock()
            mock_http_chat.send.return_value = mock_stream_429_custom
            r = await client.post(
                "/v1/chat/completions",
                json={"model": "custom_model", "stream": True, "messages": [{"role": "user", "content": "stream 429"}]},
                headers={"Authorization": "Bearer test-key"},
            )
            assert r.status_code == 429

            # chat with stream 429 on agent- model (3569-3570)
            class Stream429Iter:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    err = RuntimeError("429 rate limit")
                    err.status_code = 429
                    raise err

            stream_429 = MagicMock(status_code=200)
            stream_429.aiter_bytes = lambda: Stream429Iter()
            stream_429.aclose = AsyncMock()
            mock_http_chat.send.return_value = stream_429
            with patch("router.main.maybe_trigger_roster_sync", new=AsyncMock()) as mock_s_sync:
                r = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "agent-simple-core",
                        "stream": True,
                        "messages": [{"role": "user", "content": "stream 429"}],
                    },
                    headers={"Authorization": "Bearer test-key"},
                )
                await r.aread()
                mock_s_sync.assert_called_once()

            # chat with stream 429 on ollama model (3569->3572)
            rm._ollama_cooldown_until = 0.0
            with patch("router.main.classify_request", return_value=("agent-advanced-core", 5.0, False, "adv")):
                r = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "llm-routing-ollama",
                        "stream": True,
                        "messages": [{"role": "user", "content": "stream 429 ollama"}],
                    },
                    headers={"Authorization": "Bearer test-key"},
                )
                await r.aread()

            # chat with stream ollama error and valkey save error (3594-3595)
            rm._ollama_cooldown_until = 0.0

            class StreamOllamaErrIter:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    raise RuntimeError("ollama stream abort")

            stream_ollama_err = MagicMock(status_code=200)
            stream_ollama_err.aiter_bytes = lambda: StreamOllamaErrIter()
            stream_ollama_err.aclose = AsyncMock()
            mock_http_chat.send.return_value = stream_ollama_err
            with (
                patch("router.main.classify_request", return_value=("agent-advanced-core", 5.0, False, "adv")),
                patch("router.main.save_cooldowns_to_valkey", side_effect=RuntimeError("valkey down")),
            ):
                r = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "llm-routing-ollama",
                        "stream": True,
                        "messages": [{"role": "user", "content": "stream valkey err"}],
                    },
                    headers={"Authorization": "Bearer test-key"},
                )
                await r.aread()

            # Ollama non-streaming cooldown fallback failure (3734-3740)
            rm._ollama_cooldown_until = time.monotonic() + 100
            mock_http_chat.post.side_effect = None
            mock_http_chat.post.return_value = MagicMock(status_code=500, text="fallback failed")
            with patch("router.main.classify_request", return_value=("agent-advanced-core", 5.0, False, "adv")):
                r = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "llm-routing-auto-ollama",
                        "messages": [{"role": "user", "content": "cd fallback fail"}],
                    },
                    headers={"Authorization": "Bearer test-key"},
                )
                assert r.status_code == 500

            # Ollama non-streaming transient failure where fallback also fails (3772-3778)
            rm._ollama_cooldown_until = 0.0
            mock_http_chat.post.side_effect = [
                MagicMock(status_code=503, text="ollama 503"),
                MagicMock(status_code=500, text="fallback 500"),
            ]
            with (
                patch("router.main.classify_request", return_value=("agent-advanced-core", 5.0, False, "adv")),
                patch("router.main.save_cooldowns_to_valkey", new=AsyncMock()),
            ):
                r = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "llm-routing-auto-ollama",
                        "messages": [{"role": "user", "content": "transient fallback fail"}],
                    },
                    headers={"Authorization": "Bearer test-key"},
                )
                assert r.status_code == 500

            # Non-streaming outer cancelled cleanup (3838-3839)
            with (
                patch("router.main.classify_request", side_effect=KeyboardInterrupt("cancelled request")),
            ):
                with pytest.raises(KeyboardInterrupt):
                    await client.post(
                        "/v1/chat/completions",
                        json={
                            "model": "llm-routing-auto-free",
                            "messages": [{"role": "user", "content": "interrupt"}],
                        },
                        headers={"Authorization": "Bearer test-key"},
                    )

            # Langfuse start_observation exception at top (3198-3203)
            mock_lf_init_err = MagicMock()
            mock_lf_init_err.create_trace_id.return_value = "trace-1"
            mock_lf_init_err.start_observation.side_effect = RuntimeError("start_obs error")
            with patch("router.main.get_langfuse", return_value=mock_lf_init_err):
                mock_http_chat.post.side_effect = None
                mock_http_chat.post.return_value = mock_lite_success
                r = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "gpt-4o-mini",
                        "messages": [{"role": "user", "content": "lf init err"}],
                    },
                    headers={"Authorization": "Bearer test-key"},
                )
                assert r.status_code == 200

    # Direct generator cancellation for 3598-3607
    mock_cancel_stream = MagicMock(status_code=200)
    mock_cancel_stream.aiter_bytes = lambda: MockSSEStream([b"data: chunk1\n\n", b"data: chunk2\n\n"])
    mock_cancel_stream.aclose = AsyncMock()
    mock_http_chat.send.return_value = mock_cancel_stream

    req_stream_direct = MagicMock()
    req_stream_direct.headers = {"Authorization": "Bearer test-key"}
    req_stream_direct.state = MagicMock(auth_key_alias="test-alias")
    req_stream_direct.json = AsyncMock(
        return_value={
            "model": "agent-simple-core",
            "stream": True,
            "messages": [{"role": "user", "content": "direct stream cancel"}],
        }
    )
    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-master-key"}),
        patch("router.main._authenticate_client_request", new=AsyncMock()),
        patch("router.main.get_http_client", return_value=mock_http_chat),
        patch("router.main.save_persisted_stats", new=AsyncMock()),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        patch("router.main.get_langfuse", return_value=mock_lf),
    ):
        s_res = await chat_completions(req_stream_direct)
        s_gen = s_res.body_iterator
        first_chunk = await s_gen.asend(None)
        assert b"chunk1" in first_chunk
        await s_gen.aclose()

    # 14. get_dashboard_data total_routed == 0 (line 4052->4073)
    rm.stats["routing_paths"] = {"google_oauth_direct": 0, "litellm_fallback": 0}
    with patch("router.main.get_best_free_model", return_value={"id": "m1"}):
        d_data = await get_dashboard_data()
        assert d_data["routing_pie_gradient"] == "background: rgba(255, 255, 255, 0.05);"

    # 15. resolve_external_urls branches (lines 4216-4221, 4229-4234, 4236-4241)
    req_bad_host = MagicMock()
    req_bad_host.base_url = MagicMock(hostname="invalid host with spaces!", netloc="localhost", scheme="http")
    h, n, b = resolve_external_urls(req_bad_host)
    assert "invalid host with spaces!" in h

    req_none_netloc = MagicMock()
    req_none_netloc.base_url = MagicMock(hostname="localhost", netloc=123, scheme="http")
    h, n, b = resolve_external_urls(req_none_netloc)
    assert "localhost" in n

    req_colon_netloc = MagicMock()
    req_colon_netloc.base_url = MagicMock(hostname="localhost", netloc=":80", scheme="http")
    h, n, b = resolve_external_urls(req_colon_netloc)
    assert "localhost" in n
