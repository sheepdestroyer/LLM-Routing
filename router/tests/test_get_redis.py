import os
import time
from unittest.mock import patch, MagicMock

import pytest

import router.main as main

@pytest.fixture(autouse=True)
def reset_redis_globals():
    """Reset the global variables before and after each test."""
    original_client = main._redis_client
    original_last_attempt = main._redis_last_init_attempt

    main._redis_client = None
    main._redis_last_init_attempt = 0.0

    yield

    main._redis_client = original_client
    main._redis_last_init_attempt = original_last_attempt

def test_get_redis_already_initialized():
    """If the client is already initialized, it should return the client immediately."""
    mock_client = MagicMock()
    main._redis_client = mock_client

    assert main.get_redis() is mock_client

@patch("router.main.time.monotonic")
def test_get_redis_cooldown(mock_monotonic):
    """If init failed recently, it should return None without attempting to initialize."""
    main._redis_client = None
    main._redis_last_init_attempt = 100.0

    # Time elapsed is less than 5.0 seconds
    mock_monotonic.return_value = 103.0

    assert main.get_redis() is None

@patch("router.main.time.monotonic")
@patch("router.main.aioredis.Redis")
@patch.dict(os.environ, {"VALKEY_HOST": "my-host", "VALKEY_PORT": "1234"})
def test_get_redis_initialization_success(mock_redis, mock_monotonic):
    """If sufficient time has passed, it should initialize and return the client."""
    main._redis_client = None
    main._redis_last_init_attempt = 100.0

    # Time elapsed is 10.0 seconds (greater than 5.0)
    mock_monotonic.return_value = 110.0

    mock_redis_instance = MagicMock()
    mock_redis.return_value = mock_redis_instance

    client = main.get_redis()

    assert client is mock_redis_instance
    assert main._redis_client is mock_redis_instance
    assert main._redis_last_init_attempt == 110.0
    mock_redis.assert_called_once_with(host="my-host", port=1234, decode_responses=True, socket_timeout=1.0)

@patch("router.main.time.monotonic")
@patch("router.main.logger.warning")
@patch.dict(os.environ, {"VALKEY_HOST": "my-host", "VALKEY_PORT": "invalid"})
def test_get_redis_initialization_failure(mock_logger_warning, mock_monotonic):
    """If initialization fails, it should catch the exception, log a warning, and return None."""
    main._redis_client = None
    main._redis_last_init_attempt = 100.0

    # Time elapsed is 10.0 seconds
    mock_monotonic.return_value = 110.0

    # The int() conversion of VALKEY_PORT will raise ValueError
    client = main.get_redis()

    assert client is None
    assert main._redis_client is None
    assert main._redis_last_init_attempt == 110.0
    mock_logger_warning.assert_called_once()
    assert "Failed to initialize Valkey client" in mock_logger_warning.call_args[0][0]

@patch("router.main.time.monotonic")
@patch("router.main.aioredis.Redis")
@patch("router.main.logger.warning")
@patch.dict(os.environ, {"VALKEY_HOST": "my-host", "VALKEY_PORT": "1234"})
def test_get_redis_initialization_exception(mock_logger_warning, mock_redis, mock_monotonic):
    """If aioredis.Redis throws an exception, it should catch it and return None."""
    main._redis_client = None
    main._redis_last_init_attempt = 100.0

    mock_monotonic.return_value = 110.0

    mock_redis.side_effect = Exception("Test Exception")

    client = main.get_redis()

    assert client is None
    assert main._redis_client is None
    assert main._redis_last_init_attempt == 110.0
    mock_logger_warning.assert_called_once()
    assert "Test Exception" in mock_logger_warning.call_args[0][0]
