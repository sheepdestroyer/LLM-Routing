import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import orjson
import pytest

from router import agy_proxy
from router.agy_proxy import (
    SESSION_TTL_SECONDS,
    AgyProxyRequest,
    CooldownPersistence,
    _is_quota_exhausted,
    _run_agy_print,
    _session_store,
    cleanup_session_store,
    get_session_store,
    set_session_store,
    try_agy_proxy,
)
from router.circuit_breaker import get_google_breaker, get_vendor_breaker


@pytest.fixture(autouse=True)
def reset_state():
    _session_store.clear()
    get_google_breaker().record_success()
    get_vendor_breaker().record_success()
    yield
    _session_store.clear()
    get_google_breaker().record_success()
    get_vendor_breaker().record_success()


# ============================================================================
# 1. Session Store Coverage Tests
# ============================================================================


def test_cleanup_session_store_expired_and_non_dict():
    """Verify expired sessions and non-dict entries are handled during cleanup."""
    now = time.time()
    _session_store["expired"] = {
        "conversation_id": "c1",
        "current_tier_index": 0,
        "last_accessed": now - SESSION_TTL_SECONDS - 100,
    }
    _session_store["non_dict"] = 99999
    _session_store["valid"] = {
        "conversation_id": "c2",
        "current_tier_index": 0,
        "last_accessed": now,
    }

    # max_size=10 is >= current size (3), so excess eviction loop does not run
    cleanup_session_store(max_size=10)

    assert "expired" not in _session_store
    assert "non_dict" in _session_store
    assert "valid" in _session_store


def test_cleanup_session_store_excess_lru_with_non_dict():
    """Verify LRU eviction correctly handles non-dict values when size exceeds max_size."""
    now = time.time()
    _session_store["s1"] = {"last_accessed": now + 1}
    _session_store["s2"] = "not_dict"
    _session_store["s3"] = {"last_accessed": now + 5}

    # Force excess eviction by setting max_size=1
    cleanup_session_store(max_size=1)

    assert len(_session_store) == 1
    assert "s3" in _session_store


def test_get_session_store_branches():
    """Verify get_session_store covers missing keys, invalid types, and expired records."""
    # Key not found
    assert get_session_store("nonexistent") is None

    # Key exists but is not a dict
    _session_store["corrupt"] = "corrupt_data"
    assert get_session_store("corrupt") is None
    assert "corrupt" not in _session_store

    # Key exists but expired
    now = time.time()
    _session_store["expired"] = {
        "conversation_id": "c_old",
        "current_tier_index": 0,
        "last_accessed": now - SESSION_TTL_SECONDS - 5,
    }
    assert get_session_store("expired") is None
    assert "expired" not in _session_store

    # Valid key: returns dict and updates last_accessed
    _session_store["ok"] = {
        "conversation_id": "c_ok",
        "current_tier_index": 1,
        "last_accessed": now - 10,
    }
    res = get_session_store("ok")
    assert res is not None
    assert res["conversation_id"] == "c_ok"
    assert res["last_accessed"] >= now


def test_set_session_store_capacity_eviction():
    """Verify set_session_store triggers cleanup when capacity is reached and covers update branch."""
    with patch("router.agy_proxy.MAX_SESSION_STORE_SIZE", 3):
        _session_store["s1"] = {"conversation_id": "c1", "current_tier_index": 0, "last_accessed": 10.0}
        _session_store["s2"] = {"conversation_id": "c2", "current_tier_index": 0, "last_accessed": 20.0}
        _session_store["s3"] = {"conversation_id": "c3", "current_tier_index": 0, "last_accessed": 30.0}

        # Adding a new session when len >= MAX_SESSION_STORE_SIZE triggers cleanup
        set_session_store("s4", "c4", 0)
        assert "s4" in _session_store
        assert len(_session_store) <= 3

        # Updating an existing session when len >= MAX_SESSION_STORE_SIZE does not trigger cleanup
        set_session_store("s4", "c4_updated", 1)
        assert _session_store["s4"]["conversation_id"] == "c4_updated"
        assert _session_store["s4"]["current_tier_index"] == 1


# ============================================================================
# 2. _run_agy_print Tests
# ============================================================================


@pytest.mark.asyncio
async def test_run_agy_print_success_with_model_override():
    """Verify _run_agy_print forwards model_override and conversation_id."""
    client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "returncode": 0,
        "stdout": "Hello world",
        "stderr": "",
        "conversation_id": "conv-xyz",
    }
    client.post = AsyncMock(return_value=mock_resp)

    rc, stdout, stderr, conv_id = await _run_agy_print(
        client, "my prompt", model_override="claude-opus-4-6@default", conversation_id="conv-12345"
    )
    assert rc == 0
    assert stdout == "Hello world"
    assert stderr == ""
    assert conv_id == "conv-xyz"


@pytest.mark.asyncio
async def test_run_agy_print_default_model_and_none_fields():
    """Verify _run_agy_print handles None values in daemon JSON response."""
    client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "returncode": None,
        "stdout": None,
        "stderr": None,
        "conversation_id": None,
    }
    client.post = AsyncMock(return_value=mock_resp)

    rc, stdout, stderr, conv_id = await _run_agy_print(client, "prompt", model_override="", conversation_id=None)
    assert rc == 0
    assert stdout == ""
    assert stderr == ""
    assert conv_id is None


@pytest.mark.asyncio
async def test_run_agy_print_http_error():
    """Verify _run_agy_print returns -1 on non-200 HTTP status."""
    client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 502
    client.post = AsyncMock(return_value=mock_resp)

    rc, stdout, stderr, conv_id = await _run_agy_print(client, "prompt")
    assert rc == -1
    assert "Daemon returned HTTP status 502" in stderr
    assert conv_id is None


@pytest.mark.asyncio
async def test_run_agy_print_connection_exception():
    """Verify _run_agy_print handles network and connection exceptions."""
    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("Daemon unreachable"))

    rc, stdout, stderr, conv_id = await _run_agy_print(client, "prompt")
    assert rc == -1
    assert "Daemon connection error" in stderr
    assert conv_id is None


# ============================================================================
# 3. _is_quota_exhausted Additional Edge Cases
# ============================================================================


@pytest.mark.asyncio
async def test_is_quota_exhausted_code_429_in_log():
    """Verify _is_quota_exhausted detects 'code 429' in cli.log lines."""
    mock_file = AsyncMock()
    mock_file.seek = AsyncMock()
    mock_file.tell = AsyncMock(return_value=2000)
    mock_file.read = AsyncMock(return_value=b"Error: code 429 quota reached\n")

    with patch("aiofiles.open") as mock_open:
        mock_open.return_value.__aenter__.return_value = mock_file
        with patch("router.agy_proxy.time.time", return_value=1000.0):
            with patch("router.agy_proxy._last_log_check", 0.0):
                res = await _is_quota_exhausted(0, "", "")
                assert res is True


@pytest.mark.asyncio
async def test_is_quota_exhausted_file_size_small():
    """Verify _is_quota_exhausted with file size < 1024 bytes seeks to 0."""
    mock_file = AsyncMock()
    mock_file.seek = AsyncMock()
    mock_file.tell = AsyncMock(return_value=200)
    mock_file.read = AsyncMock(return_value=b"normal log line\n")

    with patch("aiofiles.open") as mock_open:
        mock_open.return_value.__aenter__.return_value = mock_file
        with patch("router.agy_proxy.time.time", return_value=1000.0):
            with patch("router.agy_proxy._last_log_check", 0.0):
                res = await _is_quota_exhausted(0, "", "")
                assert res is False
                mock_file.seek.assert_any_call(0)


@pytest.mark.asyncio
async def test_is_quota_exhausted_nonzero_rc_no_output():
    """Verify non-zero returncode with empty stdout/stderr is NOT quota exhausted."""
    assert await _is_quota_exhausted(1, "", "") is False


# ============================================================================
# 4. try_agy_proxy Target Tier & Circuit Breakers
# ============================================================================


@pytest.mark.asyncio
async def test_try_agy_proxy_reasoning_core_target_tier():
    """Verify agent-reasoning-core uses single gemini tier."""
    client = AsyncMock()
    with patch("router.agy_proxy._run_agy_print", return_value=(0, "Reasoning result", "", "conv-r")) as mock_run:
        req = AgyProxyRequest(
            prompt="Reason about this",
            target_tier="agent-reasoning-core",
            client=client,
        )
        resp = await try_agy_proxy(req)
        assert resp is not None
        assert resp["model"] == "gemini-3.8-flash (via agy)"
        assert resp["choices"][0]["message"]["content"] == "Reasoning result"
        mock_run.assert_called_once()
        assert mock_run.call_args[1]["model_override"] == ""


@pytest.mark.asyncio
async def test_try_agy_proxy_both_breakers_open():
    """Verify try_agy_proxy immediately returns None when both breakers are open."""
    google_breaker = get_google_breaker()
    vendor_breaker = get_vendor_breaker()
    with (
        patch.object(google_breaker, "is_currently_allowed", return_value=False),
        patch.object(vendor_breaker, "is_currently_allowed", return_value=False),
    ):
        req = AgyProxyRequest(prompt="test", client=AsyncMock())
        resp = await try_agy_proxy(req)
        assert resp is None


@pytest.mark.asyncio
async def test_try_agy_proxy_google_breaker_skips_tier_0():
    """Verify try_agy_proxy skips tier 0 when google breaker is open but vendor breaker is allowed."""
    google_breaker = get_google_breaker()
    vendor_breaker = get_vendor_breaker()
    with (
        patch.object(google_breaker, "is_currently_allowed", return_value=True),
        patch.object(vendor_breaker, "is_currently_allowed", return_value=True),
        patch.object(google_breaker, "is_allowed", return_value=False),
        patch.object(vendor_breaker, "is_allowed", return_value=True),
    ):
        with patch("router.agy_proxy._run_agy_print", return_value=(0, "Claude result", "", "conv-claude")) as mock_run:
            req = AgyProxyRequest(prompt="test prompt", client=AsyncMock())
            resp = await try_agy_proxy(req)
            assert resp is not None
            assert resp["model"] == "claude-opus-4.6 (via agy)"
            mock_run.assert_called_once()
            assert mock_run.call_args[1]["model_override"] == "claude-opus-4-6@default"


# ============================================================================
# 5. Client Lifecycle & Cooldown Persistence
# ============================================================================


@pytest.mark.asyncio
async def test_try_agy_proxy_creates_and_closes_client():
    """Verify try_agy_proxy creates httpx client when None and closes it in finally."""
    google_breaker = get_google_breaker()
    vendor_breaker = get_vendor_breaker()
    with (
        patch.object(google_breaker, "is_currently_allowed", return_value=False),
        patch.object(vendor_breaker, "is_currently_allowed", return_value=False),
    ):
        mock_c = AsyncMock()
        with patch("router.agy_proxy.httpx.AsyncClient", return_value=mock_c):
            req = AgyProxyRequest(prompt="test", client=None)
            resp = await try_agy_proxy(req)
            assert resp is None
            mock_c.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_agy_proxy_external_client_not_closed_on_non_stream():
    """Verify try_agy_proxy does not close caller-provided client."""
    client = AsyncMock()
    with patch("router.agy_proxy._run_agy_print", return_value=(0, "done", "", None)):
        req = AgyProxyRequest(prompt="test", client=client)
        resp = await try_agy_proxy(req)
        assert resp is not None
        client.aclose.assert_not_called()


@pytest.mark.asyncio
async def test_try_agy_proxy_cooldown_sync_and_save_success():
    """Verify try_agy_proxy invokes sync and save on CooldownPersistence."""
    cooldown = AsyncMock(spec=CooldownPersistence)
    client = AsyncMock()
    with patch("router.agy_proxy._run_agy_print", return_value=(0, "success response", "", None)):
        req = AgyProxyRequest(prompt="hello", client=client, cooldown_persistence=cooldown)
        resp = await try_agy_proxy(req)
        assert resp is not None
        cooldown.sync.assert_awaited_once()
        cooldown.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_agy_proxy_cooldown_sync_exception():
    """Verify try_agy_proxy survives exception during cooldown sync."""
    cooldown = AsyncMock(spec=CooldownPersistence)
    cooldown.sync.side_effect = RuntimeError("Valkey error")
    client = AsyncMock()
    with patch("router.agy_proxy._run_agy_print", return_value=(0, "success response", "", None)):
        req = AgyProxyRequest(prompt="hello", client=client, cooldown_persistence=cooldown)
        resp = await try_agy_proxy(req)
        assert resp is not None
        cooldown.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_agy_proxy_cooldown_save_exception_on_success():
    """Verify try_agy_proxy catches exception when saving cooldown on success."""
    cooldown = AsyncMock(spec=CooldownPersistence)
    cooldown.save.side_effect = RuntimeError("Save error")
    client = AsyncMock()
    with patch("router.agy_proxy._run_agy_print", return_value=(0, "success response", "", None)):
        req = AgyProxyRequest(prompt="hello", client=client, cooldown_persistence=cooldown)
        resp = await try_agy_proxy(req)
        assert resp is not None


@pytest.mark.asyncio
async def test_try_agy_proxy_cooldown_save_exception_on_failure():
    """Verify try_agy_proxy catches exception when saving cooldown on quota failure."""
    cooldown = AsyncMock(spec=CooldownPersistence)
    cooldown.save.side_effect = RuntimeError("Save error")
    client = AsyncMock()
    # Tier 0 quota exhausted, Tier 1 returns other error
    with patch(
        "router.agy_proxy._run_agy_print",
        side_effect=[
            (0, "", "RESOURCE_EXHAUSTED", None),
            (1, "", "other error", None),
        ],
    ):
        req = AgyProxyRequest(prompt="hello", client=client, cooldown_persistence=cooldown)
        resp = await try_agy_proxy(req)
        assert resp is None


@pytest.mark.asyncio
async def test_try_agy_proxy_non_stream_quota_exhausted_no_persistence():
    """Verify try_agy_proxy quota failure without cooldown_persistence."""
    client = AsyncMock()
    with patch(
        "router.agy_proxy._run_agy_print",
        side_effect=[
            (0, "", "RESOURCE_EXHAUSTED", None),
            (0, "Recovered tier 2", "", "conv-2"),
        ],
    ):
        req = AgyProxyRequest(prompt="hello", client=client, cooldown_persistence=None)
        resp = await try_agy_proxy(req)
        assert resp is not None
        assert resp["choices"][0]["message"]["content"] == "Recovered tier 2"


# ============================================================================
# 6. Message History Formatting
# ============================================================================


@pytest.mark.asyncio
async def test_try_agy_proxy_messages_context_formatting():
    """Verify message formatting with complex list blocks, missing roles, and truncation."""
    messages = [
        "not-a-dict-within-window",
        {"role": "system", "content": "You are a bot"},
        {"role": "user", "content": None},
        {"role": "assistant", "content": "First reply"},
        {
            "role": "user",
            "content": [
                "not-a-dict-block",
                {"type": "image", "url": "http://img"},
                {"type": "text", "text": None},
                {"type": "text", "text": "Structured user text"},
            ],
        },
        {"role": "assistant", "content": "Second reply"},
        {"role": "user", "content": "Final user query"},
    ]
    client = AsyncMock()
    with patch("router.agy_proxy._run_agy_print", return_value=(0, "OK", "", None)) as mock_run:
        req = AgyProxyRequest(prompt="fallback prompt", messages=messages, client=client)
        resp = await try_agy_proxy(req)
        assert resp is not None
        called_prompt = mock_run.call_args[0][1]
        assert "Assistant: First reply" in called_prompt
        assert "Assistant: Second reply" in called_prompt
        assert "User: Structured user text" in called_prompt
        assert "User: Final user query" in called_prompt
        assert "You are a bot" not in called_prompt


# ============================================================================
# 7. Session Continuity & Resumption
# ============================================================================


@pytest.mark.asyncio
async def test_try_agy_proxy_session_resumption_with_conv_id():
    """Verify session continuation starts at saved tier index and passes conversation ID."""
    set_session_store("test-session", "conv-start-12345", 1)
    client = AsyncMock()
    with patch(
        "router.agy_proxy._run_agy_print",
        return_value=(0, "tier 2 answer", "", "conv-start-12345"),
    ) as mock_run:
        req = AgyProxyRequest(prompt="hello", session_id="test-session", client=client)
        resp = await try_agy_proxy(req)
        assert resp is not None
        assert resp["model"] == "claude-opus-4.6 (via agy)"
        assert mock_run.call_args[1]["conversation_id"] == "conv-start-12345"
        assert mock_run.call_args[1]["model_override"] == "claude-opus-4-6@default"


@pytest.mark.asyncio
async def test_try_agy_proxy_session_resumption_without_conv_id():
    """Verify session continuation when existing conversation_id is None."""
    _session_store["test-session-none"] = {
        "conversation_id": None,
        "current_tier_index": 0,
        "last_accessed": time.time(),
    }
    client = AsyncMock()
    with patch(
        "router.agy_proxy._run_agy_print",
        return_value=(0, "tier 1 answer", "", "new-conv-id"),
    ):
        req = AgyProxyRequest(prompt="hello", session_id="test-session-none", client=client)
        resp = await try_agy_proxy(req)
        assert resp is not None
        saved = get_session_store("test-session-none")
        assert saved is not None
        assert saved["conversation_id"] == "new-conv-id"


@pytest.mark.asyncio
async def test_try_agy_proxy_session_unknown_id():
    """Verify session_id not yet in store is created on success."""
    client = AsyncMock()
    with patch("router.agy_proxy._run_agy_print", return_value=(0, "ans", "", "conv-new")):
        req = AgyProxyRequest(prompt="hello", session_id="unknown-sess", client=client)
        resp = await try_agy_proxy(req)
        assert resp is not None
        saved = get_session_store("unknown-sess")
        assert saved is not None
        assert saved["conversation_id"] == "conv-new"


# ============================================================================
# 8. Loop Timeout, Empty Output & Failure
# ============================================================================


@pytest.mark.asyncio
async def test_try_agy_proxy_loop_total_timeout():
    """Verify timeout check breaks the loop when remaining <= 0."""
    client = AsyncMock()
    call_count = 0

    def mock_time():
        nonlocal call_count
        call_count += 1
        if call_count >= 4:
            return 2000.0
        return 1000.0

    with patch("router.agy_proxy.time.time", side_effect=mock_time):
        req = AgyProxyRequest(prompt="hello", total_timeout=100.0, client=client)
        resp = await try_agy_proxy(req)
        assert resp is None


@pytest.mark.asyncio
async def test_try_agy_proxy_all_tiers_exhausted_cleans_session():
    """Verify all tiers exhausted removes session from session_store."""
    set_session_store("sess-fail", "conv-fail", 0)
    client = AsyncMock()
    with patch("router.agy_proxy._run_agy_print", return_value=(1, "", "error", None)):
        req = AgyProxyRequest(prompt="hello", session_id="sess-fail", client=client)
        resp = await try_agy_proxy(req)
        assert resp is None
        assert "sess-fail" not in _session_store


@pytest.mark.asyncio
async def test_try_agy_proxy_tier_returns_empty_stdout_tries_next():
    """Verify non-streaming empty stdout skips to next tier."""
    client = AsyncMock()
    with patch(
        "router.agy_proxy._run_agy_print",
        side_effect=[
            (0, "", "", None),  # Tier 0 empty response
            (0, "Tier 2 success", "", "conv-2"),  # Tier 1 success
        ],
    ):
        with patch("router.agy_proxy._is_quota_exhausted", return_value=False):
            req = AgyProxyRequest(prompt="hello", client=client)
            resp = await try_agy_proxy(req)
            assert resp is not None
            assert resp["choices"][0]["message"]["content"] == "Tier 2 success"


# ============================================================================
# 9. Streaming Path Comprehensive Coverage
# ============================================================================


@pytest.mark.asyncio
async def test_try_agy_proxy_stream_send_exception_fallback():
    """Verify streaming handles client.send exception and falls back to next tier."""
    client = AsyncMock()
    client.build_request = MagicMock()

    mock_resp_success = AsyncMock()
    mock_resp_success.aclose = AsyncMock()

    async def success_lines():
        yield orjson.dumps({"type": "token", "content": "Fallback ok"}).decode("utf-8")

    mock_resp_success.aiter_lines = success_lines

    client.send = AsyncMock(
        side_effect=[
            httpx.ConnectError("Connection failed"),
            mock_resp_success,
        ]
    )

    req = AgyProxyRequest(prompt="stream test", stream=True, client=client)
    res = await try_agy_proxy(req)
    assert res is not None
    assert res["model"] == "claude-opus-4.6"
    chunks = [c async for c in res["stream"]]
    assert chunks == ["Fallback ok"]


@pytest.mark.asyncio
async def test_try_agy_proxy_stream_empty_first_line_stop_iteration():
    """Verify streaming handles empty stream on first tier and continues to next."""
    client = AsyncMock()
    client.build_request = MagicMock()

    mock_r1 = AsyncMock()
    mock_r1.aclose = AsyncMock()

    async def empty_lines():
        if False:
            yield ""

    mock_r1.aiter_lines = empty_lines

    mock_r2 = AsyncMock()
    mock_r2.aclose = AsyncMock()

    async def ok_lines():
        yield orjson.dumps({"type": "token", "content": "T2 token"}).decode("utf-8")

    mock_r2.aiter_lines = ok_lines

    client.send = AsyncMock(side_effect=[mock_r1, mock_r2])
    req = AgyProxyRequest(prompt="stream test", stream=True, client=client)
    res = await try_agy_proxy(req)
    assert res is not None
    assert res["model"] == "claude-opus-4.6"
    mock_r1.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_agy_proxy_stream_read_first_line_exception():
    """Verify streaming handles error reading initial stream line."""
    client = AsyncMock()
    client.build_request = MagicMock()

    mock_r1 = AsyncMock()
    mock_r1.aclose = AsyncMock()

    async def error_lines():
        raise RuntimeError("Read error")
        yield ""

    mock_r1.aiter_lines = error_lines

    mock_r2 = AsyncMock()
    mock_r2.aclose = AsyncMock()

    async def ok_lines():
        yield orjson.dumps({"type": "token", "content": "T2 token"}).decode("utf-8")

    mock_r2.aiter_lines = ok_lines

    client.send = AsyncMock(side_effect=[mock_r1, mock_r2])
    req = AgyProxyRequest(prompt="stream test", stream=True, client=client)
    res = await try_agy_proxy(req)
    assert res is not None
    assert res["model"] == "claude-opus-4.6"
    mock_r1.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_agy_proxy_stream_invalid_json_first_line():
    """Verify streaming handles non-JSON initial line and closes stream."""
    client = AsyncMock()
    client.build_request = MagicMock()

    mock_r1 = AsyncMock()
    mock_r1.aclose = AsyncMock()

    async def bad_json_lines():
        yield "this is not valid json {["

    mock_r1.aiter_lines = bad_json_lines

    mock_r2 = AsyncMock()
    mock_r2.aclose = AsyncMock()

    async def ok_lines():
        yield orjson.dumps({"type": "token", "content": "T2 token"}).decode("utf-8")

    mock_r2.aiter_lines = ok_lines

    client.send = AsyncMock(side_effect=[mock_r1, mock_r2])
    req = AgyProxyRequest(prompt="stream test", stream=True, client=client)
    res = await try_agy_proxy(req)
    assert res is not None
    assert res["model"] == "claude-opus-4.6"
    mock_r1.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_agy_proxy_stream_status_quota_exhausted():
    """Verify stream initial status message indicating quota exhaustion triggers breaker and fallback."""
    client = AsyncMock()
    client.build_request = MagicMock()
    cooldown = AsyncMock(spec=CooldownPersistence)
    cooldown.save.side_effect = [RuntimeError("Save failed"), None]

    mock_r1 = AsyncMock()
    mock_r1.aclose = AsyncMock()

    async def status_exhausted_lines():
        yield orjson.dumps({"type": "status", "returncode": None, "stderr": "RESOURCE_EXHAUSTED"}).decode("utf-8")

    mock_r1.aiter_lines = status_exhausted_lines

    mock_r2 = AsyncMock()
    mock_r2.aclose = AsyncMock()

    async def ok_lines():
        yield orjson.dumps({"type": "token", "content": "T2 token"}).decode("utf-8")

    mock_r2.aiter_lines = ok_lines

    client.send = AsyncMock(side_effect=[mock_r1, mock_r2])
    req = AgyProxyRequest(prompt="stream test", stream=True, client=client, cooldown_persistence=cooldown)
    res = await try_agy_proxy(req)
    assert res is not None
    assert res["model"] == "claude-opus-4.6"
    mock_r1.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_agy_proxy_stream_status_quota_exhausted_no_persistence():
    """Verify stream quota exhaustion when cooldown_persistence is None."""
    client = AsyncMock()
    client.build_request = MagicMock()

    mock_r1 = AsyncMock()
    mock_r1.aclose = AsyncMock()

    async def status_exhausted_lines():
        yield orjson.dumps({"type": "status", "returncode": 0, "stderr": "quota reached"}).decode("utf-8")

    mock_r1.aiter_lines = status_exhausted_lines

    mock_r2 = AsyncMock()
    mock_r2.aclose = AsyncMock()

    async def ok_lines():
        yield orjson.dumps({"type": "token", "content": "T2 token"}).decode("utf-8")

    mock_r2.aiter_lines = ok_lines

    client.send = AsyncMock(side_effect=[mock_r1, mock_r2])
    req = AgyProxyRequest(prompt="stream test", stream=True, client=client, cooldown_persistence=None)
    res = await try_agy_proxy(req)
    assert res is not None
    assert res["model"] == "claude-opus-4.6"
    mock_r1.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_agy_proxy_stream_status_other_error():
    """Verify stream initial status message with rc != 0 but non-quota error skips to next tier."""
    client = AsyncMock()
    client.build_request = MagicMock()

    mock_r1 = AsyncMock()
    mock_r1.aclose = AsyncMock()

    async def status_err_lines():
        yield orjson.dumps({"type": "status", "returncode": 42, "stderr": "Command failed"}).decode("utf-8")

    mock_r1.aiter_lines = status_err_lines

    mock_r2 = AsyncMock()
    mock_r2.aclose = AsyncMock()

    async def ok_lines():
        yield orjson.dumps({"type": "token", "content": "T2 token"}).decode("utf-8")

    mock_r2.aiter_lines = ok_lines

    client.send = AsyncMock(side_effect=[mock_r1, mock_r2])
    req = AgyProxyRequest(prompt="stream test", stream=True, client=client)
    res = await try_agy_proxy(req)
    assert res is not None
    assert res["model"] == "claude-opus-4.6"
    mock_r1.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_agy_proxy_stream_status_zero_rc_not_exhausted_falls_through():
    """Verify stream initial status message with rc=0 and not exhausted falls through to stream start."""
    client = AsyncMock()
    client.build_request = MagicMock()

    mock_r = AsyncMock()
    mock_r.aclose = AsyncMock()

    async def status_ok_lines():
        yield orjson.dumps({"type": "status", "returncode": 0, "stderr": ""}).decode("utf-8")
        yield orjson.dumps({"type": "token", "content": "stream after status"}).decode("utf-8")

    mock_r.aiter_lines = status_ok_lines
    client.send = AsyncMock(return_value=mock_r)

    with patch("router.agy_proxy._is_quota_exhausted", return_value=False):
        req = AgyProxyRequest(prompt="stream test", stream=True, client=client)
        res = await try_agy_proxy(req)
        assert res is not None
        assert res["model"] == "gemini-3.8-flash"
        chunks = [c async for c in res["stream"]]
        assert chunks == ["stream after status"]


@pytest.mark.asyncio
async def test_try_agy_proxy_stream_cooldown_save_exception_on_success():
    """Verify streaming survives exception when saving cooldown upon stream start."""
    client = AsyncMock()
    client.build_request = MagicMock()
    cooldown = AsyncMock(spec=CooldownPersistence)
    cooldown.save.side_effect = RuntimeError("Save cooldown failed")

    mock_r = AsyncMock()
    mock_r.aclose = AsyncMock()

    async def ok_lines():
        yield orjson.dumps({"type": "token", "content": "tok"}).decode("utf-8")

    mock_r.aiter_lines = ok_lines

    client.send = AsyncMock(return_value=mock_r)
    req = AgyProxyRequest(prompt="stream test", stream=True, client=client, cooldown_persistence=cooldown)
    res = await try_agy_proxy(req)
    assert res is not None


@pytest.mark.asyncio
async def test_try_agy_proxy_stream_token_generator_full_branches():
    """Verify token_generator initial conversation_id, whitespace lines, token yields, and parse errors."""
    client = AsyncMock()
    client.build_request = MagicMock()

    mock_r = AsyncMock()
    mock_r.aclose = AsyncMock()

    initial_line = orjson.dumps({"type": "conversation_id", "id": "conv-stream-1"}).decode("utf-8")

    lines = [
        initial_line,
        "   ",  # whitespace line: skipped
        orjson.dumps({"type": "token", "content": "Chunk1"}).decode("utf-8"),
        orjson.dumps({"type": "token", "content": ""}).decode("utf-8"),  # empty content
        orjson.dumps({"type": "conversation_id", "id": "conv-stream-2"}).decode("utf-8"),
        orjson.dumps({"type": "unknown_event", "foo": "bar"}).decode("utf-8"),
        "invalid-stream-json-line-<<<",  # parse error handled
        orjson.dumps({"type": "token", "content": "Chunk2"}).decode("utf-8"),
    ]

    async def line_stream():
        for line in lines:
            yield line

    mock_r.aiter_lines = line_stream

    client.send = AsyncMock(return_value=mock_r)
    req = AgyProxyRequest(prompt="stream prompt", session_id="stream-sess", stream=True, client=client)
    res = await try_agy_proxy(req)
    assert res is not None

    generator = res["stream"]
    collected = [chunk async for chunk in generator]
    assert collected == ["Chunk1", "Chunk2"]

    # Verify session store was updated with the latest conversation id
    saved = get_session_store("stream-sess")
    assert saved is not None
    assert saved["conversation_id"] == "conv-stream-2"
    mock_r.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_agy_proxy_stream_token_generator_conversation_id_no_session_id():
    """Verify token_generator handles conversation_id events when session_id is None."""
    client = AsyncMock()
    client.build_request = MagicMock()

    mock_r = AsyncMock()
    mock_r.aclose = AsyncMock()

    initial_line = orjson.dumps({"type": "conversation_id", "id": "conv-stream-init"}).decode("utf-8")
    lines = [
        initial_line,
        orjson.dumps({"type": "conversation_id", "id": "conv-stream-loop"}).decode("utf-8"),
        orjson.dumps({"type": "token", "content": "Done"}).decode("utf-8"),
    ]

    async def line_stream():
        for line in lines:
            yield line

    mock_r.aiter_lines = line_stream
    client.send = AsyncMock(return_value=mock_r)

    req = AgyProxyRequest(prompt="stream prompt", session_id=None, stream=True, client=client)
    res = await try_agy_proxy(req)
    assert res is not None

    chunks = [c async for c in res["stream"]]
    assert chunks == ["Done"]
    mock_r.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_agy_proxy_stream_token_generator_initial_json_decode_error():
    """Verify token_generator handles JSONDecodeError on the initial line inside generator."""
    client = AsyncMock()
    client.build_request = MagicMock()

    mock_r = AsyncMock()
    mock_r.aclose = AsyncMock()

    original_loads = orjson.loads
    loads_calls = [0]

    def mock_loads(line):
        loads_calls[0] += 1
        if loads_calls[0] == 2:  # inside token_generator for initial_line
            raise json.JSONDecodeError("corrupt initial", "{}", 0)
        return original_loads(line)

    async def line_stream():
        yield orjson.dumps({"type": "token", "content": "init-token"}).decode("utf-8")
        yield orjson.dumps({"type": "token", "content": "from iterator"}).decode("utf-8")

    mock_r.aiter_lines = line_stream
    client.send = AsyncMock(return_value=mock_r)

    with patch("router.agy_proxy.orjson.loads", side_effect=mock_loads):
        req = AgyProxyRequest(prompt="test", stream=True, client=client)
        res = await try_agy_proxy(req)
        assert res is not None
        chunks = [c async for c in res["stream"]]
        assert chunks == ["from iterator"]


@pytest.mark.asyncio
async def test_try_agy_proxy_stream_closes_auto_created_client():
    """Verify token_generator closes auto-created httpx client when close_client is True."""
    mock_r = AsyncMock()
    mock_r.aclose = AsyncMock()

    async def line_stream():
        yield orjson.dumps({"type": "token", "content": "Done"}).decode("utf-8")

    mock_r.aiter_lines = line_stream

    mock_client = AsyncMock()
    mock_client.build_request = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_r)

    with patch("router.agy_proxy.httpx.AsyncClient", return_value=mock_client):
        req = AgyProxyRequest(prompt="auto client stream", stream=True, client=None)
        res = await try_agy_proxy(req)
        assert res is not None
        chunks = [c async for c in res["stream"]]
        assert chunks == ["Done"]
        mock_client.aclose.assert_awaited_once()
