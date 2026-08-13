import copy
import json
import pytest
from unittest.mock import patch, AsyncMock
import router.main as main


@pytest.fixture(autouse=True)
def reset_globals():
    """Reset global variables related to stats and redis."""
    original_client = main._redis_client
    original_last_attempt = main._redis_last_init_attempt
    original_stats = copy.deepcopy(main.stats)

    main._redis_client = None
    main._redis_last_init_attempt = 0.0

    yield

    main._redis_client = original_client
    main._redis_last_init_attempt = original_last_attempt
    main.stats = original_stats


@pytest.mark.asyncio
@patch("router.main.get_redis")
async def test_save_stats_no_redis(mock_get_redis):
    mock_get_redis.return_value = None
    await main.save_stats_to_valkey()
    mock_get_redis.assert_called_once()


@pytest.mark.asyncio
@patch("router.main.get_redis")
async def test_save_stats_success(mock_get_redis):
    mock_redis = AsyncMock()
    mock_get_redis.return_value = mock_redis

    main.stats["total_requests"] = 42
    main.stats["timeline"] = [{"tool": "shell", "tokens": 100}]

    await main.save_stats_to_valkey()

    assert mock_redis.set.call_count == 2
    # Verify router:stats call
    stats_call = [call for call in mock_redis.set.call_args_list if call.args[0] == "router:stats"]
    assert len(stats_call) == 1
    saved_data = json.loads(stats_call[0].args[1])
    assert saved_data["total_requests"] == 42

    # Verify router:timeline call
    timeline_call = [call for call in mock_redis.set.call_args_list if call.args[0] == "router:timeline"]
    assert len(timeline_call) == 1
    saved_timeline = json.loads(timeline_call[0].args[1])
    assert saved_timeline == [{"tool": "shell", "tokens": 100}]


@pytest.mark.asyncio
@patch("router.main.get_redis")
@patch("router.main.time.monotonic")
async def test_save_stats_exception(mock_monotonic, mock_get_redis):
    mock_redis = AsyncMock()
    mock_redis.set.side_effect = Exception("Redis write error")
    mock_get_redis.return_value = mock_redis
    mock_monotonic.return_value = 100.0

    await main.save_stats_to_valkey()

    assert main._redis_client is None
    assert main._redis_last_init_attempt == 100.0


@pytest.mark.asyncio
@patch("router.main.get_redis")
async def test_sync_stats_no_redis(mock_get_redis):
    mock_get_redis.return_value = None
    await main.sync_stats_from_valkey()
    mock_get_redis.assert_called_once()


@pytest.mark.asyncio
@patch("router.main.get_redis")
async def test_sync_stats_success(mock_get_redis):
    mock_redis = AsyncMock()
    remote_stats = {
        "total_requests": 100,
        "simple_requests": 50,
        "medium_requests": 20,
        "complex_requests": 15,
        "reasoning_requests": 10,
        "advanced_requests": 5,
        "cache_hits": 30,
        "last_triage_decision": "gemini-2.5-flash",
        "total_triage_time_ms": 1000.0,
        "total_proxy_time_ms": 5000.0,
        "prompt_tokens": 15000,
        "completion_tokens": 8000,
        "tool_tokens": {"tree": 100, "shell": 200},
        "routing_paths": {"google_oauth_direct": 70, "litellm_fallback": 30},
    }
    remote_timeline = [
        {"tool": "shell", "tokens": 50},
        {"tool": "view", "tokens": 120},
    ]

    async def mock_get(key):
        if key == "router:stats":
            return json.dumps(remote_stats)
        if key == "router:timeline":
            return json.dumps(remote_timeline)
        return None

    mock_redis.get.side_effect = mock_get
    mock_get_redis.return_value = mock_redis

    main.stats["total_requests"] = 10
    main.stats["total_triage_time_ms"] = 100.0
    main.stats["total_proxy_time_ms"] = 200.0

    await main.sync_stats_from_valkey()

    assert main.stats["total_requests"] == 100
    assert main.stats["simple_requests"] == 50
    assert main.stats["last_triage_decision"] == "gemini-2.5-flash"
    assert main.stats["avg_triage_latency_ms"] == 10.0
    assert main.stats["avg_proxy_latency_ms"] == 50.0
    assert main.stats["tool_tokens"]["shell"] == 200
    assert main.stats["timeline"] == remote_timeline


@pytest.mark.asyncio
@patch("router.main.get_redis")
@patch("router.main.time.monotonic")
async def test_sync_stats_exception(mock_monotonic, mock_get_redis):
    mock_redis = AsyncMock()
    mock_redis.get.side_effect = Exception("Redis read error")
    mock_get_redis.return_value = mock_redis
    mock_monotonic.return_value = 100.0

    await main.sync_stats_from_valkey()

    assert main._redis_client is None
    assert main._redis_last_init_attempt == 100.0


@pytest.mark.asyncio
@patch("router.main.save_stats_to_valkey", new_callable=AsyncMock)
@patch("router.main.sync_stats_from_valkey", new_callable=AsyncMock)
async def test_valkey_stats_persistence_wrapper(mock_sync, mock_save):
    persistence = main.ValkeyStatsPersistence()
    await persistence.sync()
    mock_sync.assert_called_once()
    await persistence.save()
    mock_save.assert_called_once()
