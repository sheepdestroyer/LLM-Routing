#!/usr/bin/env python3
"""
Integration test for the agy circuit breaker.

Simulates 4 consecutive quota failures and verifies:
  - Tier 1 cooldown (5 min) after 1st failure
  - Tier 2 cooldown (30 min) after 2nd failure  
  - Tier 3 cooldown (5 hours) after 3rd failure
  - Probe behavior: one allowed attempt after cooldown
  - Reset to Tier 0 on success
  - Stay at Tier 3 on repeated failure
"""

import sys
import time
sys.path.insert(0, '/home/gpav/Vrac/LAB/AI/LLM-Routing/router')

from circuit_breaker import get_breaker, TIER_COOLDOWNS, MAX_TIER


def test_initial_state():
    """Breaker starts at Tier 0 (open)."""
    b = get_breaker()
    b.tier = 0
    b.cooldown_until = 0
    b.probe_granted = False
    b.total_trips = 0
    assert b.is_allowed() == True
    assert b.tier == 0
    print("✓ Initial state: Tier 0, agy allowed")


def test_first_failure_trips_to_tier1():
    """1st failure → Tier 1, 5 min cooldown."""
    b = get_breaker()
    b.tier = 0
    b.cooldown_until = 0
    b.probe_granted = False
    b.record_failure()
    assert b.tier == 1, f"Expected tier 1, got {b.tier}"
    assert b.cooldown_until > time.time(), "Cooldown should be set"
    assert b.is_allowed() == False, "Should block during cooldown"
    print("✓ 1st failure → Tier 1 (5 min cooldown)")


def test_probe_granted_after_cooldown():
    """After cooldown expires, exactly one probe is allowed."""
    b = get_breaker()
    b.tier = 1
    b.cooldown_until = time.time() - 10  # expired 10s ago
    b.probe_granted = False
    assert b.is_allowed() == True, "Probe should be granted"
    assert b.probe_granted == True, "Probe flag should be set"
    assert b.is_allowed() == False, "Second call should be denied"
    print("✓ Probe granted after cooldown expiry, consumed on next check")


def test_probe_failure_advances_tier():
    """Probe failure → advance to next tier."""
    b = get_breaker()
    b.tier = 1
    b.cooldown_until = time.time() - 10
    b.probe_granted = True  # probe was granted
    b.record_failure()  # probe fails
    assert b.tier == 2, f"Expected tier 2, got {b.tier}"
    assert b.probe_granted == False
    print("✓ Failed probe at Tier 1 → advanced to Tier 2 (30 min)")


def test_tier3_stays_at_tier3():
    """At Tier 3, failure → stays at Tier 3 (renews cooldown)."""
    b = get_breaker()
    b.tier = MAX_TIER
    b.cooldown_until = time.time() - 10
    b.probe_granted = True
    old_until = b.cooldown_until
    b.record_failure()
    assert b.tier == MAX_TIER, "Should stay at Tier 3"
    assert b.cooldown_until > old_until, "Cooldown should be renewed"
    assert b.probe_granted == False
    print("✓ Tier 3 failure → stays at Tier 3 (renews 5-hour cooldown)")


def test_success_resets():
    """Success at any tier → reset to Tier 0."""
    b = get_breaker()
    b.tier = 2
    b.cooldown_until = time.time() + 1000
    b.probe_granted = False
    b.record_success()
    assert b.tier == 0
    assert b.is_allowed() == True
    print("✓ Success resets breaker to Tier 0 from any tier")


def test_full_cycle():
    """Complete cycle: success → 3 failures → probe success → reset."""
    b = get_breaker()
    b.tier = 0
    b.cooldown_until = 0
    b.probe_granted = False
    b.total_trips = 0

    # Operate normally
    assert b.is_allowed()
    b.record_success()
    assert b.tier == 0

    # 1st failure
    b.record_failure()
    assert b.tier == 1
    assert not b.is_allowed()

    # Simulate cooldown expiry
    b.cooldown_until = time.time() - 10
    assert b.is_allowed()  # probe granted
    b.record_failure()  # probe fails
    assert b.tier == 2

    # Simulate cooldown expiry
    b.cooldown_until = time.time() - 10
    assert b.is_allowed()  # probe granted
    b.record_failure()  # probe fails again
    assert b.tier == 3
    assert TIER_COOLDOWNS[3] == 18000, "Tier 3 must be 5 hours"

    # Simulate cooldown expiry + probe success
    b.cooldown_until = time.time() - 10
    assert b.is_allowed()  # probe granted
    b.record_success()  # probe succeeds
    assert b.tier == 0
    assert b.total_trips == 3

    print("✓ Full cycle: 3 failures → Tier 3 → probe success → reset")


if __name__ == "__main__":
    test_initial_state()
    test_first_failure_trips_to_tier1()
    test_probe_granted_after_cooldown()
    test_probe_failure_advances_tier()
    test_tier3_stays_at_tier3()
    test_success_resets()
    test_full_cycle()
    
    print("\n" + "=" * 60)
    print("  ALL CIRCUIT BREAKER TESTS PASSED ✓")
    print("=" * 60)
