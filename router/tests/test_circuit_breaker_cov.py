import asyncio
import time
from unittest.mock import AsyncMock, patch
import pytest

from router.circuit_breaker import (
    DualCircuitBreaker,
    PerModelBreaker,
    get_breaker,
    get_google_breaker,
    get_vendor_breaker,
    MAX_TIER,
    TIER_COOLDOWNS,
)


def _reset(b: DualCircuitBreaker):
    for sub in (b.google, b.vendor):
        sub.tier = 0
        sub.cooldown_until = 0.0
        sub.probe_granted = False
        sub.total_trips = 0
        sub.last_trip_time = 0.0


def test_get_breakers_singletons():
    b = get_breaker()
    assert isinstance(b, DualCircuitBreaker)
    assert get_google_breaker() is b.google
    assert get_vendor_breaker() is b.vendor


def test_per_model_breaker_init():
    p = PerModelBreaker("test")
    assert p.name == "test"
    assert p.tier == 0
    assert p.cooldown_until == 0.0
    assert p.probe_granted is False
    assert p.total_trips == 0
    assert p.last_trip_time == 0.0


def test_per_model_breaker_is_allowed():
    p = PerModelBreaker("test")
    # tier 0 -> allowed
    assert p.is_allowed() is True

    # blocked (in cooldown)
    p.tier = 1
    p.cooldown_until = time.time() + 100
    p.probe_granted = False
    assert p.is_allowed() is False

    # cooldown expired, probe not yet granted -> probe granted
    p.cooldown_until = time.time() - 1
    assert p.is_allowed() is True
    assert p.probe_granted is True

    # cooldown expired, but probe already granted -> blocked
    assert p.is_allowed() is False


def test_per_model_breaker_is_currently_allowed():
    p = PerModelBreaker("test")
    # tier 0
    assert p.is_currently_allowed() is True

    # tier > 0, in cooldown
    p.tier = 1
    p.cooldown_until = time.time() + 100
    assert p.is_currently_allowed() is False

    # cooldown expired, probe not granted
    p.cooldown_until = time.time() - 1
    p.probe_granted = False
    assert p.is_currently_allowed() is True
    # Verify non-mutating
    assert p.probe_granted is False

    # cooldown expired, probe already granted
    p.probe_granted = True
    assert p.is_currently_allowed() is False


def test_per_model_breaker_record_success():
    p = PerModelBreaker("test")
    # tier 0 success
    p.record_success()
    assert p.tier == 0

    # tier > 0 success
    p.tier = 2
    p.cooldown_until = time.time() + 500
    p.probe_granted = True
    p.record_success()
    assert p.tier == 0
    assert p.cooldown_until == 0.0
    assert p.probe_granted is False


def test_per_model_breaker_record_failure_tiers():
    p = PerModelBreaker("test")
    now = 100000.0

    with patch("time.time", return_value=now):
        # failure from tier 0 -> tier 1
        p.record_failure()
        assert p.tier == 1
        assert p.cooldown_until == now + TIER_COOLDOWNS[1]
        assert p.total_trips == 1
        assert p.last_trip_time == now
        assert p.probe_granted is False

        # failure from tier 1 -> tier 2
        p.record_failure()
        assert p.tier == 2
        assert p.cooldown_until == now + TIER_COOLDOWNS[2]
        assert p.total_trips == 2

        # failure from tier 2 -> tier 3 (MAX_TIER)
        p.record_failure()
        assert p.tier == 3
        assert p.cooldown_until == now + TIER_COOLDOWNS[3]
        assert p.total_trips == 3

        # failure from tier 3 -> stays tier 3
        p.record_failure()
        assert p.tier == 3
        assert p.cooldown_until == now + TIER_COOLDOWNS[3]
        assert p.total_trips == 4


def test_per_model_breaker_status():
    p = PerModelBreaker("status_test")
    now = 200000.0

    with patch("time.time", return_value=now):
        p.tier = 1
        p.cooldown_until = now + 120.0
        p.probe_granted = False
        p.total_trips = 2
        p.last_trip_time = now - 60.0

        st = p.status()
        assert st["name"] == "status_test"
        assert st["tier"] == 1
        assert st["allowed"] is False
        assert st["cooldown_remaining_seconds"] == 120
        assert st["cooldown_total_seconds"] == TIER_COOLDOWNS[1]
        assert st["total_trips"] == 2
        assert st["last_trip_time"] == now - 60.0
        assert st["probe_granted"] is False

        # cooldown in past
        p.cooldown_until = now - 50.0
        st2 = p.status()
        assert st2["cooldown_remaining_seconds"] == 0
        assert st2["allowed"] is True


@pytest.mark.anyio
async def test_per_model_breaker_sync_from_valkey():
    p = PerModelBreaker("sync_test")

    # None redis client
    await p.sync_from_valkey(None)

    # Empty raw state
    mock_redis = AsyncMock()
    mock_redis.hgetall.return_value = {}
    await p.sync_from_valkey(mock_redis)

    # String values
    mock_redis.hgetall.return_value = {
        "tier": "2",
        "cooldown_until": "12345.5",
        "probe_granted": "True",
        "total_trips": "3",
        "last_trip_time": "12000.0",
    }
    await p.sync_from_valkey(mock_redis)
    assert p.tier == 2
    assert p.cooldown_until == 12345.5
    assert p.probe_granted is True
    assert p.total_trips == 3
    assert p.last_trip_time == 12000.0

    # Bytes values
    mock_redis.hgetall.return_value = {
        b"tier": b"1",
        b"cooldown_until": b"54321.0",
        b"probe_granted": b"0",
        b"total_trips": b"5",
        b"last_trip_time": b"54000.0",
    }
    await p.sync_from_valkey(mock_redis)
    assert p.tier == 1
    assert p.cooldown_until == 54321.0
    assert p.probe_granted is False
    assert p.total_trips == 5
    assert p.last_trip_time == 54000.0

    # Exception caught and logged
    mock_redis.hgetall.side_effect = RuntimeError("Valkey fail")
    with patch("router.circuit_breaker.logger.warning") as mock_warn:
        await p.sync_from_valkey(mock_redis)
        mock_warn.assert_called_once()


@pytest.mark.anyio
async def test_per_model_breaker_save_to_valkey():
    p = PerModelBreaker("save_test")
    p.tier = 2
    p.cooldown_until = 200000.0
    p.probe_granted = True
    p.total_trips = 7
    p.last_trip_time = 195000.0

    # None redis client
    await p.save_to_valkey(None)

    # Valid save with cooldown in future
    mock_redis = AsyncMock()
    now = 198000.0  # cooldown_until - now = 2000s; ttl = max(3600, 2000 + 3600) = 5600
    with patch("time.time", return_value=now):
        await p.save_to_valkey(mock_redis)
        mock_redis.hset.assert_awaited_once_with(
            "circuit_breaker:save_test",
            mapping={
                "tier": "2",
                "cooldown_until": "200000.0",
                "probe_granted": "True",
                "total_trips": "7",
                "last_trip_time": "195000.0",
            },
        )
        mock_redis.expire.assert_awaited_once_with("circuit_breaker:save_test", 5600)

    # Valid save with cooldown in past (TTL min 3600)
    mock_redis.reset_mock()
    p.probe_granted = False
    now = 205000.0  # cooldown_until - now = -5000s; ttl = max(3600, -5000 + 3600) = 3600
    with patch("time.time", return_value=now):
        await p.save_to_valkey(mock_redis)
        mock_redis.expire.assert_awaited_once_with("circuit_breaker:save_test", 3600)

    # Exception caught and logged
    mock_redis.reset_mock()
    mock_redis.hset.side_effect = RuntimeError("Valkey save err")
    with patch("router.circuit_breaker.logger.warning") as mock_warn:
        await p.save_to_valkey(mock_redis)
        mock_warn.assert_called_once()


def test_dual_circuit_breaker_behavior():
    b = DualCircuitBreaker()
    _reset(b)

    # is_allowed: google True, vendor True
    assert b.is_allowed() is True

    # is_allowed: google False, vendor True
    b.google.tier = 1
    b.google.cooldown_until = time.time() + 100
    assert b.is_allowed() is True

    # is_allowed: google False, vendor False
    b.vendor.tier = 1
    b.vendor.cooldown_until = time.time() + 100
    assert b.is_allowed() is False

    # is_allowed_peek
    _reset(b)
    assert b.is_allowed_peek() is True

    b.google.tier = 1
    b.google.cooldown_until = time.time() + 100
    assert b.is_allowed_peek() is True  # vendor is still allowed

    b.vendor.tier = 1
    b.vendor.cooldown_until = time.time() + 100
    assert b.is_allowed_peek() is False  # both blocked

    # record_failure trips both
    _reset(b)
    b.record_failure()
    assert b.google.tier == 1
    assert b.vendor.tier == 1

    # record_success resets both
    b.record_success()
    assert b.google.tier == 0
    assert b.vendor.tier == 0

    # tier property
    b.google.tier = 1
    b.vendor.tier = 3
    assert b.tier == 3
    b.google.tier = 2
    b.vendor.tier = 0
    assert b.tier == 2

    # status
    st = b.status()
    assert "google" in st
    assert "vendor" in st
    assert st["google"]["name"] == "google"
    assert st["vendor"]["name"] == "vendor"


@pytest.mark.anyio
async def test_dual_circuit_breaker_valkey_sync_and_save():
    b = DualCircuitBreaker()
    mock_redis = AsyncMock()

    with (
        patch.object(b.google, "sync_from_valkey", new_callable=AsyncMock) as mock_g_sync,
        patch.object(b.vendor, "sync_from_valkey", new_callable=AsyncMock) as mock_v_sync,
    ):
        await b.sync_from_valkey(mock_redis)
        mock_g_sync.assert_awaited_once_with(mock_redis)
        mock_v_sync.assert_awaited_once_with(mock_redis)

    with (
        patch.object(b.google, "save_to_valkey", new_callable=AsyncMock) as mock_g_save,
        patch.object(b.vendor, "save_to_valkey", new_callable=AsyncMock) as mock_v_save,
    ):
        await b.save_to_valkey(mock_redis)
        mock_g_save.assert_awaited_once_with(mock_redis)
        mock_v_save.assert_awaited_once_with(mock_redis)
