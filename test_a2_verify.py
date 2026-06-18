#!/usr/bin/env python3
"""Verify circuit breaker integration into agy_proxy.py"""
import sys
sys.path.insert(0, '/home/gpav/Vrac/LAB/AI/LLM-Routing/router')

from circuit_breaker import get_breaker
from agy_proxy import try_agy_proxy
import asyncio, time

b = get_breaker()
b.tier = 3
b.cooldown_until = time.time() + 18000
b.probe_granted = False

result = asyncio.run(try_agy_proxy('test prompt'))
assert result is None, f'Breaker should return None when blocked, got: {result}'
print('Integration verified: blocked breaker returns None from try_agy_proxy')
