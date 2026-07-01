#!/usr/bin/env python3
"""Verify circuit breaker integration into agy_proxy.py"""
import sys
from pathlib import Path

# Dynamic project root discovery
root = Path(__file__).resolve()
while root.parent != root and not (root / ".git").exists():
    root = root.parent
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "router"))

from circuit_breaker import get_breaker
from agy_proxy import try_agy_proxy
import asyncio, time

b = get_breaker()
for sub in (b.google, b.vendor):
    sub.tier = 3
    sub.cooldown_until = time.time() + 18000
    sub.probe_granted = False

result = asyncio.run(try_agy_proxy('test prompt'))
assert result is None, f'Breaker should return None when blocked, got: {result}'
print('Integration verified: blocked breaker returns None from try_agy_proxy')
