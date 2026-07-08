import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import time

from router.main import sync_cooldowns_from_valkey
import router.main

@pytest.fixture
def mock_globals():
    # Save original globals
    orig_cooldown = getattr(router.main, "_ollama_cooldown_until", 0.0)
    orig_redis_client = getattr(router.main, "_redis_client", None)
    orig_redis_last_init = getattr(router.main, "_redis_last_init_attempt", 0.0)

    # Set them to known values
    router.main._ollama_cooldown_until = 0.0
    router.main._redis_client = "dummy_client"
    router.main._redis_last_init_attempt = 0.0

    yield

    # Restore original globals
    router.main._ollama_cooldown_until = orig_cooldown
    router.main._redis_client = orig_redis_client
    router.main._redis_last_init_attempt = orig_redis_last_init

@pytest.mark.asyncio
async def test_sync_no_redis(mock_globals):
    with patch("router.main.get_redis", return_value=None):
        with patch("router.main.get_breaker") as mock_get_breaker:
            await sync_cooldowns_from_valkey()
            mock_get_breaker.assert_not_called()

@pytest.mark.asyncio
async def test_sync_redis_value_future(mock_globals):
    mock_redis = AsyncMock()
    # Mock time.time() to 100.0, so future is 110.0
    mock_redis.get.return_value = "110.0"

    with patch("router.main.get_redis", return_value=mock_redis), \
         patch("router.main.get_breaker") as mock_get_breaker, \
         patch("time.time", return_value=100.0), \
         patch("time.monotonic", return_value=50.0):

        mock_breaker = AsyncMock()
        mock_get_breaker.return_value = mock_breaker

        await sync_cooldowns_from_valkey()

        # remaining = 110.0 - 100.0 = 10.0
        # cooldown = 50.0 + 10.0 = 60.0
        assert router.main._ollama_cooldown_until == 60.0
        mock_breaker.sync_from_valkey.assert_awaited_once_with(mock_redis)

@pytest.mark.asyncio
async def test_sync_redis_value_past(mock_globals):
    mock_redis = AsyncMock()
    # Mock time.time() to 100.0, so past is 90.0
    mock_redis.get.return_value = "90.0"

    with patch("router.main.get_redis", return_value=mock_redis), \
         patch("router.main.get_breaker") as mock_get_breaker, \
         patch("time.time", return_value=100.0):

        mock_breaker = AsyncMock()
        mock_get_breaker.return_value = mock_breaker

        # Set cooldown to a positive value first to ensure it gets zeroed
        router.main._ollama_cooldown_until = 999.0

        await sync_cooldowns_from_valkey()

        # remaining = 90.0 - 100.0 = -10.0 (<= 0)
        # cooldown = 0.0
        assert router.main._ollama_cooldown_until == 0.0
        mock_breaker.sync_from_valkey.assert_awaited_once_with(mock_redis)

@pytest.mark.asyncio
async def test_sync_redis_value_none_past_cooldown(mock_globals):
    mock_redis = AsyncMock()
    mock_redis.get.return_value = None

    with patch("router.main.get_redis", return_value=mock_redis), \
         patch("router.main.get_breaker") as mock_get_breaker, \
         patch("time.monotonic", return_value=50.0):

        mock_breaker = AsyncMock()
        mock_get_breaker.return_value = mock_breaker

        # Current cooldown is in the past
        router.main._ollama_cooldown_until = 40.0

        await sync_cooldowns_from_valkey()

        assert router.main._ollama_cooldown_until == 0.0
        mock_breaker.sync_from_valkey.assert_awaited_once_with(mock_redis)

@pytest.mark.asyncio
async def test_sync_redis_value_none_future_cooldown(mock_globals):
    mock_redis = AsyncMock()
    mock_redis.get.return_value = None

    with patch("router.main.get_redis", return_value=mock_redis), \
         patch("router.main.get_breaker") as mock_get_breaker, \
         patch("time.monotonic", return_value=50.0):

        mock_breaker = AsyncMock()
        mock_get_breaker.return_value = mock_breaker

        # Current cooldown is in the future
        router.main._ollama_cooldown_until = 60.0

        await sync_cooldowns_from_valkey()

        # Should not be modified
        assert router.main._ollama_cooldown_until == 60.0
        mock_breaker.sync_from_valkey.assert_awaited_once_with(mock_redis)

@pytest.mark.asyncio
async def test_sync_redis_exception(mock_globals):
    mock_redis = AsyncMock()
    # Throw an exception during get
    mock_redis.get.side_effect = Exception("Redis error")

    # Initialize globals with non-None/non-zero values
    router.main._redis_client = mock_redis
    router.main._redis_last_init_attempt = 0.0

    with patch("router.main.get_redis", return_value=mock_redis), \
         patch("time.monotonic", return_value=123.45):

        await sync_cooldowns_from_valkey()

        # Verify error handling resets redis client and updates last init attempt
        assert router.main._redis_client is None
        assert router.main._redis_last_init_attempt == 123.45
