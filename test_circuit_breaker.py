#!/usr/bin/env python3
"""
Integration test for the agy dual circuit breaker.

Simulates consecutive quota failures and verifies:
  - Independent google and vendor breakers
  - Tier 1 cooldown (5 min) after 1st failure
  - Tier 2 cooldown (30 min) after 2nd failure  
  - Tier 3 cooldown (5 hours) after 3rd failure
  - Probe behavior: one allowed attempt after cooldown
  - Reset to Tier 0 on success
  - Stay at Tier 3 on repeated failure
  - Backward compatibility of master breaker methods
"""

import sys
import time
import asyncio
import pytest
from unittest.mock import patch, AsyncMock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from router.circuit_breaker import get_breaker, TIER_COOLDOWNS, MAX_TIER


def reset_breakers():
    b = get_breaker()
    for sub in (b.google, b.vendor):
        sub.tier = 0
        sub.cooldown_until = 0.0
        sub.probe_granted = False
        sub.total_trips = 0
        sub.last_trip_time = 0.0


def test_initial_state():
    """Breaker starts at Tier 0 (open)."""
    reset_breakers()
    b = get_breaker()
    assert b.is_allowed()
    assert b.tier == 0
    assert b.google.is_allowed()
    assert b.vendor.is_allowed()
    print("✓ Initial state: Tier 0, allowed")


def test_first_failure_trips_to_tier1():
    """1st failure → Tier 1, 5 min cooldown."""
    reset_breakers()
    b = get_breaker()
    
    b.google.record_failure()
    assert b.google.tier == 1
    assert b.google.cooldown_until > time.time()
    assert not b.google.is_allowed()

    # Master breaker is still allowed because vendor is allowed (backward compatible fallback)
    assert b.is_allowed()
    print("✓ 1st failure → Tier 1 (5 min cooldown) on google breaker")


def test_probe_granted_after_cooldown():
    """After cooldown expires, exactly one probe is allowed."""
    reset_breakers()
    b = get_breaker()
    
    b.google.tier = 1
    b.google.cooldown_until = time.time() - 10  # expired 10s ago
    b.google.probe_granted = False
    
    assert b.google.is_allowed(), "Probe should be granted"
    assert b.google.probe_granted, "Probe flag should be set"
    assert not b.google.is_allowed(), "Second call should be denied"
    print("✓ Probe granted after cooldown expiry, consumed on next check")


def test_probe_failure_advances_tier():
    """Probe failure → advance to next tier."""
    reset_breakers()
    b = get_breaker()
    
    b.google.tier = 1
    b.google.cooldown_until = time.time() - 10
    b.google.probe_granted = True  # probe was granted
    b.google.record_failure()  # probe fails
    
    assert b.google.tier == 2, f"Expected tier 2, got {b.google.tier}"
    assert not b.google.probe_granted
    print("✓ Failed probe at Tier 1 → advanced to Tier 2 (30 min)")


def test_tier3_stays_at_tier3():
    """At Tier 3, failure → stays at Tier 3 (renews cooldown)."""
    reset_breakers()
    b = get_breaker()
    
    b.google.tier = MAX_TIER
    b.google.cooldown_until = time.time() - 10
    b.google.probe_granted = True
    old_until = b.google.cooldown_until
    b.google.record_failure()
    
    assert b.google.tier == MAX_TIER, "Should stay at Tier 3"
    assert b.google.cooldown_until > old_until, "Cooldown should be renewed"
    assert not b.google.probe_granted
    print("✓ Tier 3 failure → stays at Tier 3 (renews 5-hour cooldown)")


def test_success_resets():
    """Success at any tier → reset to Tier 0."""
    reset_breakers()
    b = get_breaker()
    
    b.google.tier = 2
    b.google.cooldown_until = time.time() + 1000
    b.google.probe_granted = False
    b.google.record_success()
    
    assert b.google.tier == 0
    assert b.google.is_allowed()
    print("✓ Success resets breaker to Tier 0 from any tier")


def test_backward_compatibility():
    """Master breaker record_failure and record_success affect both breakers."""
    reset_breakers()
    b = get_breaker()
    
    b.record_failure()
    assert b.google.tier == 1
    assert b.vendor.tier == 1
    assert not b.is_allowed()  # both blocked

    b.record_success()
    assert b.google.tier == 0
    assert b.vendor.tier == 0
    assert b.is_allowed()
    print("✓ Master record_failure and record_success maintain compatibility")


def test_dual_breaker_tier_max_logic():
    """Master breaker tier returns max of sub-breakers."""
    reset_breakers()
    b = get_breaker()

    test_cases = [
        (0, 0, 0),
        (1, 0, 1),
        (0, 2, 2),
        (3, 3, 3),
        (3, 1, 3),
    ]
    for google_tier, vendor_tier, expected_tier in test_cases:
        b.google.tier = google_tier
        b.vendor.tier = vendor_tier
        assert b.tier == expected_tier, f"Expected tier {expected_tier} for google={google_tier}, vendor={vendor_tier}, but got {b.tier}"

    print("✓ Dual breaker tier correctly evaluates to max of sub-breakers")


def test_full_cycle():
    """Complete cycle: success → 3 failures → probe success → reset."""
    reset_breakers()
    b = get_breaker()
    sub = b.google

    # Operate normally
    assert sub.is_allowed()
    sub.record_success()
    assert sub.tier == 0

    # 1st failure
    sub.record_failure()
    assert sub.tier == 1
    assert not sub.is_allowed()

    # Simulate cooldown expiry
    sub.cooldown_until = time.time() - 10
    assert sub.is_allowed()  # probe granted
    sub.record_failure()  # probe fails
    assert sub.tier == 2

    # Simulate cooldown expiry
    sub.cooldown_until = time.time() - 10
    assert sub.is_allowed()  # probe granted
    sub.record_failure()  # probe fails again
    assert sub.tier == 3
    assert TIER_COOLDOWNS[3] == 18000, "Tier 3 must be 5 hours"

    # Simulate cooldown expiry + probe success
    sub.cooldown_until = time.time() - 10
    assert sub.is_allowed()  # probe granted
    sub.record_success()  # probe succeeds
    assert sub.tier == 0
    assert sub.total_trips == 3

    print("✓ Full cycle: 3 failures → Tier 3 → probe success → reset")

def test_sync_from_valkey_exception_handling():
    """Exception during Valkey sync is caught and logged."""
    reset_breakers()
    b = get_breaker()


    mock_redis = AsyncMock()
    mock_redis.hgetall.side_effect = Exception("Simulated connection error")

    with patch("router.circuit_breaker.logger.warning") as mock_logger_warning:
        asyncio.run(b.google.sync_from_valkey(mock_redis))

        mock_redis.hgetall.assert_called_once_with("circuit_breaker:google")
        mock_logger_warning.assert_called_once_with(
            "Valkey circuit_breaker [google] sync failed: Simulated connection error"
        )
    print("✓ Valkey sync exception handling")


@pytest.mark.anyio
async def test_save_to_valkey_success():
    """Verify state is correctly serialized and persisted to Valkey."""
    b = get_breaker()
    sub = b.google
    sub.tier = 2
    sub.cooldown_until = 1234567890.0
    sub.probe_granted = True
    sub.total_trips = 5
    sub.last_trip_time = 1234567000.0

    mock_redis = AsyncMock()

    with patch('time.time', return_value=1234560000.0):
        await sub.save_to_valkey(mock_redis)

    expected_state = {
        "tier": "2",
        "cooldown_until": "1234567890.0",
        "probe_granted": "True",
        "total_trips": "5",
        "last_trip_time": "1234567000.0",
    }

    mock_redis.hset.assert_awaited_once_with("circuit_breaker:google", mapping=expected_state)
    # TTL logic: max(3600.0, cooldown_until - now + 3600.0)
    # max(3600.0, 1234567890.0 - 1234560000.0 + 3600.0) = max(3600.0, 7890.0 + 3600.0) = 11490
    mock_redis.expire.assert_awaited_once_with("circuit_breaker:google", 11490)
    print("✓ Valkey save succeeds with correct data and TTL")


@pytest.mark.anyio
async def test_save_to_valkey_no_client():
    """Verify early return when redis client is None."""
    b = get_breaker()
    sub = b.google
    # Should not raise exception
    await sub.save_to_valkey(None)
    print("✓ Valkey save handles None client safely")


@pytest.mark.anyio
async def test_save_to_valkey_exception_handling():
    """Verify exceptions during Valkey save are caught and logged."""
    b = get_breaker()
    sub = b.google

    mock_redis = AsyncMock()
    mock_redis.hset.side_effect = Exception("Connection lost")

    with patch('router.circuit_breaker.logger') as mock_logger:
        await sub.save_to_valkey(mock_redis)
        mock_logger.warning.assert_called_once()
if __name__ == "__main__":
    test_initial_state()
    test_first_failure_trips_to_tier1()
    test_probe_granted_after_cooldown()
    test_probe_failure_advances_tier()
    test_tier3_stays_at_tier3()
    test_success_resets()
    test_backward_compatibility()
    test_full_cycle()
    test_dual_breaker_tier_max_logic()
    test_sync_from_valkey_exception_handling()
    asyncio.run(test_save_to_valkey_success())
    asyncio.run(test_save_to_valkey_no_client())
    asyncio.run(test_save_to_valkey_exception_handling())

    print("\n" + "=" * 60)
    print("  ALL CIRCUIT BREAKER TESTS PASSED ✓")
    print("=" * 60)
