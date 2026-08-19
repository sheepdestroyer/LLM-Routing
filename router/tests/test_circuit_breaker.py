import pytest
from unittest.mock import patch
from router.circuit_breaker import PerModelBreaker, DualCircuitBreaker, get_breaker

def test_per_model_breaker_is_currently_allowed():
    breaker = PerModelBreaker("test_model")

    # Test when tier is 0 (open)
    breaker.tier = 0
    assert breaker.is_currently_allowed() is True

    # Test when tier > 0 and cooldown hasn't expired
    with patch('time.time', return_value=100.0):
        breaker.tier = 1
        breaker.cooldown_until = 200.0
        assert breaker.is_currently_allowed() is False

    # Test when tier > 0, cooldown expired, probe not granted
    with patch('time.time', return_value=250.0):
        breaker.tier = 1
        breaker.cooldown_until = 200.0
        breaker.probe_granted = False
        assert breaker.is_currently_allowed() is True

    # Test when tier > 0, cooldown expired, probe already granted
    with patch('time.time', return_value=250.0):
        breaker.tier = 1
        breaker.cooldown_until = 200.0
        breaker.probe_granted = True
        assert breaker.is_currently_allowed() is False
