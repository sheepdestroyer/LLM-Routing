import os
import time
from unittest.mock import patch, MagicMock

import pytest

import router.main as main

@pytest.fixture(autouse=True)
def reset_redis_globals(monkeypatch):
    """Reset the global variables before and after each test."""
    monkeypatch.delenv("VALKEY_URL", raising=False)
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
def test_valkey_port_invalid_fallback(mock_logger_warning, mock_monotonic):
    """_valkey_port() should log a warning and fall back to 6379 on invalid port."""
    port = main._valkey_port()
    assert port == 6379
    mock_logger_warning.assert_called_once()
    assert "Invalid Valkey port" in mock_logger_warning.call_args[0][0]

@patch("router.main.time.monotonic")
@patch("router.main.aioredis.Redis")
@patch("router.main.logger.warning")
@patch.dict(os.environ, {"VALKEY_HOST": "my-host", "VALKEY_PORT": "1234"})
def test_get_redis_initialization_failure(mock_logger_warning, mock_redis, mock_monotonic):
    """If aioredis.Redis throws, it should catch the exception, log a warning, and return None."""
    main._redis_client = None
    main._redis_last_init_attempt = 100.0

    # Time elapsed is 10.0 seconds
    mock_monotonic.return_value = 110.0

    mock_redis.side_effect = Exception("Connection refused")

    client = main.get_redis()

    assert client is None
    assert main._redis_client is None
    assert main._redis_last_init_attempt == 110.0
    mock_logger_warning.assert_called_once()
    assert "Connection refused" in mock_logger_warning.call_args[0][0]

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

@patch("router.main.time.monotonic")
@patch("router.main.aioredis.Redis.from_url")
@patch.dict(os.environ, {"VALKEY_URL": "redis://my-url:1234"})
def test_get_redis_simulation_flow_url(mock_from_url, mock_monotonic):
    """Simulate the full flow for from_url: failure -> cooldown -> success -> cached."""
    # State is reset by the autouse fixture reset_redis_globals

    # 1. First attempt fails
    mock_monotonic.return_value = 10.0
    mock_from_url.side_effect = Exception("Connection error")
    assert main.get_redis() is None
    assert main._redis_last_init_attempt == 10.0

    # 2. Second attempt during cooldown (e.g. 12.0s)
    mock_monotonic.return_value = 12.0
    mock_from_url.reset_mock()
    assert main.get_redis() is None
    mock_from_url.assert_not_called()

    # 3. Third attempt after cooldown (e.g. 16.0s) succeeds
    mock_monotonic.return_value = 16.0
    mock_redis_instance = MagicMock()
    mock_from_url.side_effect = None
    mock_from_url.return_value = mock_redis_instance
    client = main.get_redis()
    assert client is mock_redis_instance
    assert main._redis_client is mock_redis_instance
    mock_from_url.assert_called_once()

    # 4. Fourth attempt returns cached instance
    mock_monotonic.return_value = 18.0
    mock_from_url.reset_mock()
    assert main.get_redis() is mock_redis_instance
    mock_from_url.assert_not_called()
