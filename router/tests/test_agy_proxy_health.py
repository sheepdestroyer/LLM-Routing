import time
import json
import pytest
from unittest.mock import AsyncMock
from router import agy_proxy as router_agy_proxy
from router.circuit_breaker import get_google_breaker, get_vendor_breaker


@pytest.fixture(autouse=True)
def clear_session_store_and_breakers():
    router_agy_proxy._session_store.clear()
    get_google_breaker().record_success()
    get_vendor_breaker().record_success()
    yield
    router_agy_proxy._session_store.clear()
    get_google_breaker().record_success()
    get_vendor_breaker().record_success()


def test_session_store_max_constant():
    """Verify MAX_SESSION_STORE_SIZE constant is set to 10,000."""
    assert router_agy_proxy.MAX_SESSION_STORE_SIZE == 10000


def test_session_store_set_and_get():
    """Verify setting and retrieving session store data."""
    router_agy_proxy.set_session_store("session1", "conv123", 0)
    session = router_agy_proxy.get_session_store("session1")
    assert session is not None
    assert session["conversation_id"] == "conv123"
    assert session["current_tier_index"] == 0
    assert "last_accessed" in session


def test_session_store_ttl_expiration():
    """Verify expired sessions are evicted on access or cleanup."""
    now = time.time()
    router_agy_proxy._session_store["fresh_session"] = {
        "conversation_id": "conv1",
        "current_tier_index": 0,
        "last_accessed": now,
    }
    router_agy_proxy._session_store["expired_session"] = {
        "conversation_id": "conv2",
        "current_tier_index": 1,
        "last_accessed": now - router_agy_proxy.SESSION_TTL_SECONDS - 10,
    }

    assert router_agy_proxy.get_session_store("expired_session") is None
    assert "expired_session" not in router_agy_proxy._session_store
    assert router_agy_proxy.get_session_store("fresh_session") is not None


def test_session_store_lru_eviction():
    """Verify LRU eviction when session store reaches max capacity."""
    now = time.time()
    for i in range(15):
        router_agy_proxy._session_store[f"session_{i}"] = {
            "conversation_id": f"conv_{i}",
            "current_tier_index": 0,
            "last_accessed": now + i,
        }

    # Clean up with max size 10
    router_agy_proxy.cleanup_session_store(max_size=10)

    assert len(router_agy_proxy._session_store) == 10
    # Oldest 5 (session_0 .. session_4) evicted
    for i in range(5):
        assert f"session_{i}" not in router_agy_proxy._session_store
    for i in range(5, 15):
        assert f"session_{i}" in router_agy_proxy._session_store


@pytest.mark.asyncio
async def test_token_generator_aclose_on_cancellation():
    """Verify stream_resp.aclose() is always called on cancellation of token generator streaming."""
    get_google_breaker().record_success()
    get_vendor_breaker().record_success()

    mock_resp = AsyncMock()
    mock_resp.aclose = AsyncMock()

    mock_client = AsyncMock()
    mock_client.send = AsyncMock(return_value=mock_resp)

    lines = [
        json.dumps({"type": "token", "content": "Hello"}),
        json.dumps({"type": "token", "content": " World"}),
    ]

    async def mock_aiter():
        for line in lines:
            yield line

    mock_resp.aiter_lines = mock_aiter

    req = router_agy_proxy.AgyProxyRequest(
        prompt="test prompt",
        stream=True,
        client=mock_client,
    )

    res = await router_agy_proxy.try_agy_proxy(req)
    assert res is not None
    assert "stream" in res

    stream_gen = res["stream"]

    # Consume first token, then close generator prematurely (simulates cancellation)
    token1 = await anext(stream_gen)
    assert token1 == "Hello"

    await stream_gen.aclose()

    # Verify stream_resp.aclose() was called
    mock_resp.aclose.assert_awaited()
