#!/usr/bin/env python3
"""Verification test for the agy circuit breaker."""

from router.circuit_breaker import get_breaker

b = get_breaker()
assert b.is_allowed() == True, 'Tier 0 should be open'
b.record_failure()
assert b.tier == 1, 'Should be at Tier 1'
assert b.is_allowed() == False, 'Tier 1 should block (cooldown active)'
# Force cooldown expiry
b.cooldown_until = 0
assert b.is_allowed() == True, 'Probe should be granted'
assert b.probe_granted == True
b.record_failure()  # probe fails
assert b.tier == 2, 'Should advance to Tier 2'
print('All assertions passed')
