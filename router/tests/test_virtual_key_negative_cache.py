import os
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import HTTPException, Request

import router.main as rm


@pytest.fixture(autouse=True)
def clean_virtual_key_caches():
    """Ensure virtual key caches are clean before and after each test."""
    rm._VIRTUAL_KEY_CACHE.clear()
    rm._INVALID_VIRTUAL_KEY_CACHE.clear()
    yield
    rm._VIRTUAL_KEY_CACHE.clear()
    rm._INVALID_VIRTUAL_KEY_CACHE.clear()


@pytest.mark.anyio
async def test_negative_cache_on_404_not_found():
    """Verify that a 404 response from /key/info is negative-cached and prevents redundant HTTP calls."""
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.json.return_value = {"error": "Key not found in database"}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp

    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "sk-master-secret"}),
        patch("router.main.get_http_client", return_value=mock_client),
    ):
        # First call: hits /key/info and caches failure
        res1 = await rm._validate_litellm_virtual_key("sk-invalid-404")
        assert res1 is None
        assert mock_client.get.call_count == 1
        assert "sk-invalid-404" in rm._INVALID_VIRTUAL_KEY_CACHE

        # Second call: served directly from negative cache without HTTP call
        res2 = await rm._validate_litellm_virtual_key("sk-invalid-404")
        assert res2 is None
        assert mock_client.get.call_count == 1  # No additional network call


@pytest.mark.anyio
async def test_negative_cache_on_400_bad_request():
    """Verify that a 400 response from /key/info is negative-cached."""
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.json.return_value = {"error": "Malformed virtual key format"}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp

    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "sk-master-secret"}),
        patch("router.main.get_http_client", return_value=mock_client),
    ):
        res1 = await rm._validate_litellm_virtual_key("sk-invalid-400")
        assert res1 is None
        assert mock_client.get.call_count == 1
        assert "sk-invalid-400" in rm._INVALID_VIRTUAL_KEY_CACHE

        res2 = await rm._validate_litellm_virtual_key("sk-invalid-400")
        assert res2 is None
        assert mock_client.get.call_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_master_key_rejection_logs_error_and_not_cached(status_code):
    """401/403 indicate LiteLLM rejected the router's master key — must log error and NOT negative-cache."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.text = "Unauthorized master key"

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp

    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "sk-master-wrong"}),
        patch("router.main.get_http_client", return_value=mock_client),
        patch.object(rm.logger, "error") as mock_log_err,
    ):
        res = await rm._validate_litellm_virtual_key("sk-client-key-1")
        assert res is None
        assert mock_client.get.call_count == 1
        # Crucial: do NOT negative-cache since client key itself isn't necessarily invalid
        assert "sk-client-key-1" not in rm._INVALID_VIRTUAL_KEY_CACHE
        mock_log_err.assert_called_once_with("LiteLLM /key/info rejected master key with status %s", status_code)


@pytest.mark.anyio
async def test_unexpected_status_logs_warning_and_not_cached():
    """5xx / unexpected statuses from /key/info log warning and are not negative-cached."""
    mock_resp = MagicMock()
    mock_resp.status_code = 502
    mock_resp.text = "Bad Gateway"

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp

    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "sk-master-secret"}),
        patch("router.main.get_http_client", return_value=mock_client),
        patch.object(rm.logger, "warning") as mock_log_warn,
    ):
        res = await rm._validate_litellm_virtual_key("sk-502-key")
        assert res is None
        assert "sk-502-key" not in rm._INVALID_VIRTUAL_KEY_CACHE
        mock_log_warn.assert_called_once_with("LiteLLM /key/info returned unexpected status %s: %s", 502, "Bad Gateway")


@pytest.mark.anyio
async def test_negative_cache_on_blocked_key():
    """Verify that a 200 response with blocked=True is treated as invalid and negative-cached."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"info": {"key_name": "test", "blocked": True}}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp

    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "sk-master-secret"}),
        patch("router.main.get_http_client", return_value=mock_client),
    ):
        res1 = await rm._validate_litellm_virtual_key("sk-blocked-user")
        assert res1 is None
        assert mock_client.get.call_count == 1
        assert "sk-blocked-user" in rm._INVALID_VIRTUAL_KEY_CACHE

        res2 = await rm._validate_litellm_virtual_key("sk-blocked-user")
        assert res2 is None
        assert mock_client.get.call_count == 1


@pytest.mark.anyio
async def test_negative_cache_expiry():
    """Verify that expired negative cache entries are evicted and re-queried."""
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp

    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "sk-master-secret"}),
        patch("router.main.get_http_client", return_value=mock_client),
    ):
        # Pre-seed negative cache with an expired entry
        rm._INVALID_VIRTUAL_KEY_CACHE["sk-expired-entry"] = time.time() - (rm._INVALID_VIRTUAL_KEY_TTL + 10.0)

        # Should re-query since cache entry is expired
        res = await rm._validate_litellm_virtual_key("sk-expired-entry")
        assert res is None
        assert mock_client.get.call_count == 1
        # Re-populated with fresh timestamp
        assert "sk-expired-entry" in rm._INVALID_VIRTUAL_KEY_CACHE


@pytest.mark.anyio
async def test_max_cache_size_fifo_eviction():
    """Verify that _record_invalid_virtual_key evicts the oldest entry (FIFO) when exceeding max capacity."""
    with patch("router.main._MAX_INVALID_VIRTUAL_KEY_CACHE_SIZE", 3):
        t0 = time.time()
        rm._record_invalid_virtual_key("sk-first", t0)
        rm._record_invalid_virtual_key("sk-second", t0 + 1)
        rm._record_invalid_virtual_key("sk-third", t0 + 2)

        assert len(rm._INVALID_VIRTUAL_KEY_CACHE) == 3
        assert "sk-first" in rm._INVALID_VIRTUAL_KEY_CACHE

        # Adding 4th entry exceeds capacity 3 -> oldest ("sk-first") must be evicted
        rm._record_invalid_virtual_key("sk-fourth", t0 + 3)
        assert len(rm._INVALID_VIRTUAL_KEY_CACHE) == 3
        assert "sk-first" not in rm._INVALID_VIRTUAL_KEY_CACHE
        assert "sk-second" in rm._INVALID_VIRTUAL_KEY_CACHE
        assert "sk-third" in rm._INVALID_VIRTUAL_KEY_CACHE
        assert "sk-fourth" in rm._INVALID_VIRTUAL_KEY_CACHE

        # Updating existing entry should NOT evict
        rm._record_invalid_virtual_key("sk-second", t0 + 4)
        assert len(rm._INVALID_VIRTUAL_KEY_CACHE) == 3


@pytest.mark.anyio
async def test_valid_key_evicts_negative_cache_and_populates_positive_cache():
    """Verify that when a key is valid, it is removed from negative cache and added to positive cache."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    valid_info = {"key_alias": "user-app", "user_id": "u-123", "blocked": False}
    mock_resp.json.return_value = {"info": valid_info}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp

    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "sk-master-secret"}),
        patch("router.main.get_http_client", return_value=mock_client),
    ):
        res = await rm._validate_litellm_virtual_key("sk-valid-key")
        assert res == valid_info
        assert mock_client.get.call_count == 1
        assert "sk-valid-key" not in rm._INVALID_VIRTUAL_KEY_CACHE
        assert "sk-valid-key" in rm._VIRTUAL_KEY_CACHE

        # Second call: served from positive cache
        res2 = await rm._validate_litellm_virtual_key("sk-valid-key")
        assert res2 == valid_info
        assert mock_client.get.call_count == 1


@pytest.mark.anyio
async def test_network_exception_not_negative_cached():
    """Transient network errors should not permanently lock out keys in negative cache."""
    mock_client = AsyncMock()
    mock_client.get.side_effect = TimeoutError("Connection timed out to LiteLLM")

    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "sk-master-secret"}),
        patch("router.main.get_http_client", return_value=mock_client),
    ):
        res = await rm._validate_litellm_virtual_key("sk-timeout-key")
        assert res is None
        # Network errors should not be cached in negative cache
        assert "sk-timeout-key" not in rm._INVALID_VIRTUAL_KEY_CACHE


@pytest.mark.anyio
async def test_authenticate_client_request_negative_caching_integration():
    """Integration test verifying _authenticate_client_request uses negative caching for invalid tokens."""
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.json.return_value = {"error": "Key not found in database"}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp

    req = MagicMock(spec=Request)
    req.headers = {"Authorization": "Bearer sk-bad-client-token"}
    req.state = MagicMock()

    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "sk-master-secret"}),
        patch("router.main.get_http_client", return_value=mock_client),
    ):
        # 1st request -> 401, triggers 1 HTTP check
        with pytest.raises(HTTPException) as exc1:
            await rm._authenticate_client_request(req)
        assert exc1.value.status_code == 401
        assert mock_client.get.call_count == 1

        # 2nd request with same bad token -> 401, NO HTTP check
        with pytest.raises(HTTPException) as exc2:
            await rm._authenticate_client_request(req)
        assert exc2.value.status_code == 401
        assert mock_client.get.call_count == 1
