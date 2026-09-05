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
async def test_negative_cache_on_401_unauthorized():
    """Verify that a 401 response from /key/info is negative-cached."""
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.json.return_value = {"error": "Invalid token"}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp

    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "sk-master-secret"}),
        patch("router.main.get_http_client", return_value=mock_client),
    ):
        res1 = await rm._validate_litellm_virtual_key("sk-invalid-401")
        assert res1 is None
        assert mock_client.get.call_count == 1
        assert "sk-invalid-401" in rm._INVALID_VIRTUAL_KEY_CACHE

        res2 = await rm._validate_litellm_virtual_key("sk-invalid-401")
        assert res2 is None
        assert mock_client.get.call_count == 1


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
