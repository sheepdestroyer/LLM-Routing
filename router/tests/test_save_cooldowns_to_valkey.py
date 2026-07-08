import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import time
import router.main as main

@pytest.fixture(autouse=True)
def reset_globals(monkeypatch):
    """Reset global variables related to cooldowns and redis."""
    original_client = main._redis_client
    original_last_attempt = main._redis_last_init_attempt
    original_cooldown = main._ollama_cooldown_until

    main._redis_client = None
    main._redis_last_init_attempt = 0.0
    main._ollama_cooldown_until = 0.0

    yield

    main._redis_client = original_client
    main._redis_last_init_attempt = original_last_attempt
    main._ollama_cooldown_until = original_cooldown

@pytest.mark.asyncio
@patch("router.main.get_redis")
async def test_save_cooldowns_no_redis(mock_get_redis):
    mock_get_redis.return_value = None
    await main.save_cooldowns_to_valkey()
    mock_get_redis.assert_called_once()

@pytest.mark.asyncio
@patch("router.main.get_redis")
@patch("router.main.get_breaker")
@patch("router.main.time.monotonic")
@patch("router.main.time.time")
async def test_save_cooldowns_with_cooldown(mock_time, mock_monotonic, mock_get_breaker, mock_get_redis):
    # Setup mocks
    mock_redis = AsyncMock()
    mock_get_redis.return_value = mock_redis

    mock_breaker = AsyncMock()
    mock_get_breaker.return_value = mock_breaker

    mock_monotonic.return_value = 100.0
    mock_time.return_value = 1600000000.0

    # Set active cooldown
    main._ollama_cooldown_until = 110.0 # 10 seconds remaining

    await main.save_cooldowns_to_valkey()

    # Check if redis.set was called correctly for ollama cooldown
    mock_redis.set.assert_called_once_with(
        "cooldown:ollama",
        str(1600000000.0 + 10.0),
        ex=10
    )

    # Check if breaker.save_to_valkey was called
    mock_breaker.save_to_valkey.assert_called_once_with(mock_redis)

@pytest.mark.asyncio
@patch("router.main.get_redis")
@patch("router.main.get_breaker")
@patch("router.main.time.monotonic")
async def test_save_cooldowns_without_cooldown(mock_monotonic, mock_get_breaker, mock_get_redis):
    # Setup mocks
    mock_redis = AsyncMock()
    mock_get_redis.return_value = mock_redis

    mock_breaker = AsyncMock()
    mock_get_breaker.return_value = mock_breaker

    mock_monotonic.return_value = 100.0

    # Set expired cooldown
    main._ollama_cooldown_until = 90.0 # expired

    await main.save_cooldowns_to_valkey()

    # Check if redis.delete was called for ollama cooldown
    mock_redis.delete.assert_called_once_with("cooldown:ollama")

    # Check if breaker.save_to_valkey was called
    mock_breaker.save_to_valkey.assert_called_once_with(mock_redis)

@pytest.mark.asyncio
@patch("router.main.get_redis")
@patch("router.main.time.monotonic")
async def test_save_cooldowns_exception(mock_monotonic, mock_get_redis):
    # Setup mock to raise exception
    mock_redis = AsyncMock()
    # Making one of the awaited methods raise an exception
    mock_redis.delete.side_effect = Exception("Test Error")
    mock_get_redis.return_value = mock_redis

    mock_monotonic.return_value = 100.0
    # Set expired cooldown so it triggers delete which raises exception
    main._ollama_cooldown_until = 90.0

    # Should not raise exception
    await main.save_cooldowns_to_valkey()

    # Exception handler resets redis client
    assert main._redis_client is None
    assert main._redis_last_init_attempt == 100.0
