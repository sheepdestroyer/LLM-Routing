#!/usr/bin/env python3
"""Verify circuit breaker integration into agy_proxy.py"""
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
