
import pytest
import time
from unittest.mock import patch

from router.circuit_breaker import DualCircuitBreaker, PerModelBreaker

def test_dual_circuit_breaker_is_allowed_peek():
    breaker = DualCircuitBreaker()

    # Both sub-breakers open (tier 0) -> should be allowed
    assert breaker.google.tier == 0
    assert breaker.vendor.tier == 0
    assert breaker.is_allowed_peek() is True

    # Google tripped but not cooldown expired -> vendor is still allowed
    with patch("time.time", return_value=1000.0):
        breaker.google.tier = 1
        breaker.google.cooldown_until = 1500.0
        assert breaker.is_allowed_peek() is True

    # Both tripped, none expired -> False
    with patch("time.time", return_value=1000.0):
        breaker.google.tier = 1
        breaker.google.cooldown_until = 1500.0
        breaker.vendor.tier = 1
        breaker.vendor.cooldown_until = 1500.0
        assert breaker.is_allowed_peek() is False

    # Both tripped, one expired, probe not granted -> True
    with patch("time.time", return_value=1600.0):
        assert breaker.is_allowed_peek() is True

    # Both tripped, one expired but probe granted -> False
    with patch("time.time", return_value=1600.0):
        breaker.vendor.probe_granted = True
        assert breaker.is_allowed_peek() is True # Google still allowed
        breaker.google.probe_granted = True
        assert breaker.is_allowed_peek() is False

def test_per_model_breaker_is_currently_allowed():
    breaker = PerModelBreaker("test-model")

    # Tier 0 -> should be allowed
    assert breaker.tier == 0
    assert breaker.is_currently_allowed() is True

    with patch("time.time", return_value=1000.0):
        breaker.tier = 1
        breaker.cooldown_until = 1500.0
        # Not expired -> should be False
        assert breaker.is_currently_allowed() is False

    with patch("time.time", return_value=1600.0):
        # Expired -> should be True (probe not granted yet)
        assert breaker.is_currently_allowed() is True

        # Once probe is granted, should be False
        breaker.probe_granted = True
        assert breaker.is_currently_allowed() is False
