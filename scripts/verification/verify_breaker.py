#!/usr/bin/env python3
"""Verification test for the agy circuit breaker."""

import sys
from pathlib import Path

# Dynamic project root discovery
root = Path(__file__).resolve()
while root.parent != root and not (root / ".git").exists():
    root = root.parent
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "router"))

from router.circuit_breaker import get_breaker

b = get_breaker()
assert b.is_allowed(), 'Tier 0 should be open'

for sub in (b.google, b.vendor):
    assert sub.is_allowed()
    sub.record_failure()
    assert sub.tier == 1, 'Should be at Tier 1'
    assert not sub.is_allowed(), 'Tier 1 should block (cooldown active)'
    # Force cooldown expiry
    sub.cooldown_until = 0
    assert sub.is_allowed(), 'Probe should be granted'
    assert sub.probe_granted
    sub.record_failure()  # probe fails
    assert sub.tier == 2, 'Should advance to Tier 2'

assert b.tier == 2
print('All assertions passed')
