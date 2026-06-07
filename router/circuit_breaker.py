"""
Circuit breaker for Google Cloud Code Assist quota exhaustion.

3-tier exponential cooldown:
  Tier 1: 5 min (300s)
  Tier 2: 30 min (1800s)  
  Tier 3: 5 hours (18000s) — matches official Google quota refresh window

After each cooldown, one probe request is allowed. If it succeeds
the breaker resets. If quota is still exhausted, the breaker advances
to the next tier (or stays at Tier 3).
"""

import time
import logging

logger = logging.getLogger("circuit-breaker")

# Cooldown durations in seconds
TIER_COOLDOWNS = {
    1: 300,     # 5 minutes
    2: 1800,    # 30 minutes
    3: 18000,   # 5 hours
}

MAX_TIER = 3


class AgyCircuitBreaker:
    """Tracks quota exhaustion state with exponential cooldown."""

    def __init__(self):
        # tier: 0 = open (agy allowed), 1-3 = cooldown active
        self.tier: int = 0
        self.cooldown_until: float = 0.0
        # probe_granted: True when cooldown expired and next request is the probe
        self.probe_granted: bool = False
        # Statistics
        self.total_trips: int = 0
        self.last_trip_time: float = 0.0

    def is_allowed(self) -> bool:
        """
        Check whether an agy request is currently allowed.
        
        Returns True if:
          - tier == 0 (breaker open, agy operational)
          - probe_granted (cooldown expired, one probe attempt allowed)
        
        If a cooldown has just expired and no probe was granted yet,
        grant the probe and return True.
        """
        now = time.time()

        if self.tier == 0:
            return True

        # Check if cooldown has expired
        if now >= self.cooldown_until:
            # Cooldown expired — grant exactly one probe
            if not self.probe_granted:
                self.probe_granted = True
                logger.info(
                    f"Circuit breaker: Tier {self.tier} cooldown expired. "
                    f"Granting 1 probe request to test agy availability."
                )
                return True
            # Already granted and consumed — stay blocked until reset
            return False

        # Cooldown still active
        remaining = self.cooldown_until - now
        logger.debug(
            f"Circuit breaker: agy blocked (tier {self.tier}, "
            f"{remaining:.0f}s remaining)"
        )
        return False

    def record_success(self):
        """Called when an agy request succeeds — resets the breaker to Tier 0."""
        if self.tier > 0:
            logger.info(
                f"Circuit breaker: agy probe succeeded — resetting from "
                f"Tier {self.tier} to Tier 0 (open)"
            )
        self.tier = 0
        self.cooldown_until = 0.0
        self.probe_granted = False

    def record_failure(self):
        """
        Called when agy returns quota-exhausted.
        
        If we were in a probe window, consume the probe and advance to next tier.
        If we were in Tier 0, trip to Tier 1.
        If we were already at Tier 3, stay at Tier 3 (renew cooldown).
        """
        now = time.time()
        self.total_trips += 1
        self.last_trip_time = now

        if self.tier == 0:
            # First failure — trip to Tier 1
            new_tier = 1
        elif self.probe_granted:
            # Probe failed — advance to next tier (or stay at max)
            new_tier = min(self.tier + 1, MAX_TIER)
        else:
            # Already in cooldown (shouldn't normally happen — 
            # is_allowed() would have blocked this)
            new_tier = min(self.tier + 1, MAX_TIER)

        cooldown = TIER_COOLDOWNS[new_tier]
        self.tier = new_tier
        self.cooldown_until = now + cooldown
        self.probe_granted = False

        if new_tier == MAX_TIER:
            logger.warning(
                f"Circuit breaker: TRIPPED to Tier {new_tier} — "
                f"agy blocked for {cooldown / 3600:.1f} hours "
                f"(until official quota refresh). "
                f"Total trips: {self.total_trips}"
            )
        else:
            logger.warning(
                f"Circuit breaker: advanced to Tier {new_tier} — "
                f"agy blocked for {cooldown / 60:.0f} min. "
                f"Total trips: {self.total_trips}"
            )

    def status(self) -> dict:
        """Return a structured status dict for the dashboard."""
        now = time.time()
        remaining = max(0, self.cooldown_until - now)
        return {
            "tier": self.tier,
            "agy_allowed": self.is_allowed(),
            "cooldown_remaining_seconds": int(remaining),
            "cooldown_total_seconds": TIER_COOLDOWNS.get(self.tier, 0),
            "total_trips": self.total_trips,
            "last_trip_time": self.last_trip_time,
            "probe_granted": self.probe_granted,
        }


# Module-level singleton — all agy-related code imports this instance
_breaker = AgyCircuitBreaker()


def get_breaker() -> AgyCircuitBreaker:
    """Return the module-level circuit breaker singleton."""
    return _breaker
