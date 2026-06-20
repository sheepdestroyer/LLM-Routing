"""
Circuit breaker for Google Cloud Code Assist quota exhaustion.

TWO independent breakers:
  google_breaker  — for Google model calls (Gemini via agy or direct OAuth)
  vendor_breaker  — for vendor model calls (Claude Opus, GPT-OSS via agy)

Each has independent 3-tier exponential cooldown:
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


class PerModelBreaker:
    """Tracks quota exhaustion for a specific model family with exponential cooldown."""

    def __init__(self, name: str):
        """Initialize the per-model circuit breaker with a name and default tier/cooldown states."""
        self.name = name
        self.tier: int = 0        # 0 = open (allowed), 1-3 = cooldown active
        self.cooldown_until: float = 0.0
        self.probe_granted: bool = False
        self.total_trips: int = 0
        self.last_trip_time: float = 0.0

    def is_allowed(self) -> bool:
        """Check whether this model family is currently allowed."""
        now = time.time()

        if self.tier == 0:
            return True

        if now >= self.cooldown_until:
            if not self.probe_granted:
                self.probe_granted = True
                logger.info(
                    f"Circuit breaker [{self.name}]: Tier {self.tier} cooldown expired. "
                    f"Granting 1 probe request."
                )
                return True
            return False

        remaining = self.cooldown_until - now
        logger.debug(
            f"Circuit breaker [{self.name}]: blocked (tier {self.tier}, "
            f"{remaining:.0f}s remaining)"
        )
        return False

    def is_currently_allowed(self) -> bool:
        """Check whether this model family is allowed, without mutating state."""
        now = time.time()
        if self.tier == 0:
            return True
        if now >= self.cooldown_until:
            return not self.probe_granted
        return False

    def record_success(self):
        """Reset breaker to Tier 0 on successful request."""
        if self.tier > 0:
            logger.info(
                f"Circuit breaker [{self.name}]: probe succeeded — resetting from "
                f"Tier {self.tier} to Tier 0 (open)"
            )
        self.tier = 0
        self.cooldown_until = 0.0
        self.probe_granted = False

    def record_failure(self):
        """Advance to next cooldown tier on rate-limit/quota failure."""
        now = time.time()
        self.total_trips += 1
        self.last_trip_time = now

        if self.tier == 0:
            new_tier = 1
        else:
            new_tier = min(self.tier + 1, MAX_TIER)

        cooldown = TIER_COOLDOWNS[new_tier]
        self.tier = new_tier
        self.cooldown_until = now + cooldown
        self.probe_granted = False

        if new_tier == MAX_TIER:
            logger.warning(
                f"Circuit breaker [{self.name}]: TRIPPED to Tier {new_tier} — "
                f"blocked for {cooldown / 3600:.1f}h "
                f"(total trips: {self.total_trips})"
            )
        else:
            logger.warning(
                f"Circuit breaker [{self.name}]: advanced to Tier {new_tier} — "
                f"blocked for {cooldown / 60:.0f}min "
                f"(total trips: {self.total_trips})"
            )

    def status(self) -> dict:
        """Return structured status for the dashboard."""
        now = time.time()
        remaining = max(0, self.cooldown_until - now)
        return {
            "name": self.name,
            "tier": self.tier,
            "allowed": self.is_allowed(),
            "cooldown_remaining_seconds": int(remaining),
            "cooldown_total_seconds": TIER_COOLDOWNS.get(self.tier, 0),
            "total_trips": self.total_trips,
            "last_trip_time": self.last_trip_time,
            "probe_granted": self.probe_granted,
        }

    async def sync_from_valkey(self, redis_client) -> None:
        """Synchronize circuit breaker state from Valkey."""
        if not redis_client:
            return
        try:
            state = await redis_client.hgetall(f"circuit_breaker:{self.name}")
            if state:
                self.tier = int(state.get("tier", "0"))
                self.cooldown_until = float(state.get("cooldown_until", "0.0"))
                self.probe_granted = state.get("probe_granted", "False") == "True"
                self.total_trips = int(state.get("total_trips", "0"))
                self.last_trip_time = float(state.get("last_trip_time", "0.0"))
        except Exception as e:
            logger.warning(f"Valkey circuit_breaker [{self.name}] sync failed: {e}")

    async def save_to_valkey(self, redis_client) -> None:
        """Persist circuit breaker state to Valkey."""
        if not redis_client:
            return
        try:
            key = f"circuit_breaker:{self.name}"
            state = {
                "tier": str(self.tier),
                "cooldown_until": str(self.cooldown_until),
                "probe_granted": "True" if self.probe_granted else "False",
                "total_trips": str(self.total_trips),
                "last_trip_time": str(self.last_trip_time),
            }
            await redis_client.hset(key, mapping=state)
            now = time.time()
            ttl = int(max(3600.0, self.cooldown_until - now + 3600.0))
            await redis_client.expire(key, ttl)
        except Exception as e:
            logger.warning(f"Valkey circuit_breaker [{self.name}] save failed: {e}")



class DualCircuitBreaker:
    """
    Master breaker with two independent model-family breakers.
    Backward-compatible with existing get_breaker() calls.
    """

    def __init__(self):
        """Initialize the dual circuit breaker with separate google and vendor sub-breakers."""
        self.google = PerModelBreaker("google")
        self.vendor = PerModelBreaker("vendor")

    # Backward-compat: old code calls get_breaker().is_allowed()
    # Default to checking BOTH — if either allows, return allowed.
    # This ensures old code without model awareness works correctly.
    def is_allowed(self) -> bool:
        """Check if either the google or vendor breaker allows the request (backward-compat)."""
        return self.google.is_allowed() or self.vendor.is_allowed()

    def is_allowed_peek(self) -> bool:
        """Check if either sub-breaker is allowed, without mutating state."""
        return self.google.is_currently_allowed() or self.vendor.is_currently_allowed()

    def record_failure(self):
        """Backward-compat: trip both breakers (conservative for old code)."""
        self.google.record_failure()
        self.vendor.record_failure()

    def record_success(self):
        """Backward-compat: reset both breakers."""
        self.google.record_success()
        self.vendor.record_success()

    @property
    def tier(self) -> int:
        """Return the maximum cooldown tier across both google and vendor sub-breakers."""
        return max(self.google.tier, self.vendor.tier)

    def status(self) -> dict:
        """Return the aggregated status dictionary of both sub-breakers for the dashboard."""
        return {
            "google": self.google.status(),
            "vendor": self.vendor.status(),
        }

    async def sync_from_valkey(self, redis_client) -> None:
        """Synchronize both sub-breakers from Valkey."""
        import asyncio
        await asyncio.gather(
            self.google.sync_from_valkey(redis_client),
            self.vendor.sync_from_valkey(redis_client)
        )

    async def save_to_valkey(self, redis_client) -> None:
        """Persist both sub-breakers to Valkey."""
        import asyncio
        await asyncio.gather(
            self.google.save_to_valkey(redis_client),
            self.vendor.save_to_valkey(redis_client)
        )



# Module-level singleton
_breaker = DualCircuitBreaker()


def get_breaker() -> DualCircuitBreaker:
    """Return the dual circuit breaker singleton."""
    return _breaker


def get_google_breaker() -> PerModelBreaker:
    """Return the Google-specific breaker."""
    return _breaker.google


def get_vendor_breaker() -> PerModelBreaker:
    """Return the vendor (Claude/GPT) breaker."""
    return _breaker.vendor
