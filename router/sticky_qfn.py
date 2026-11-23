"""Sticky session + slot-overflow + cooldown router for Qwen3.8-Flash-Next (QFN).

Splits chat-pool requests (``strata-qwen``, ``local-qwen``) across x570 local Strata
(``locallama-qwen``, primary up to ``LOCAL_QWEN_PRIMARY_MAX_SLOTS`` concurrent requests)
and GX10 Strata (``gb10-strata``, overflow), keeping multi-turn sessions pinned to one
host via Valkey (``qfn:pin:chat:{session_id}``) with bounded in-memory fallback.

Home Assistant Assist requests (``local-qwen-hass``) default to GX10 (``strata-qwen-hass``)
and fail over strictly within the LAN to x570 (``locallama-qwen-hass``).
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("sticky-qfn")

HOSTS: tuple[str, str] = ("x570", "gx10")
CHAT_POOL_MODELS: frozenset[str] = frozenset({"strata-qwen", "local-qwen"})
HASS_POOL_MODEL = "local-qwen-hass"

CHAT_TARGETS: dict[str, str] = {
    "x570": "locallama-qwen",
    "gx10": "gb10-strata",
}
HASS_TARGETS: dict[str, str] = {
    "gx10": "strata-qwen-hass",
    "x570": "locallama-qwen-hass",
}

_TRANSIENT_STATUS_CODES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
_FALSE_VALUES: frozenset[str] = frozenset({"0", "false", "no", "off"})
_ROUTE_REASONS: tuple[str, ...] = (
    "pinned",
    "primary",
    "overflow",
    "failover_cooldown",
    "failover",
    "hass_primary",
    "hass_failover",
)


def make_pin_session_key(session_id: str | None, tenant: object = None) -> str | None:
    """Return a tenant-scoped opaque hash key for session pinning (never logs raw IDs)."""
    if not session_id:
        return None
    tenant_str = str(tenant).strip() if tenant is not None else ""
    tenant_tag = hashlib.sha256(tenant_str.encode("utf-8")).hexdigest()[:12] if tenant_str else "anon"
    sess_tag = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return f"{tenant_tag}:{sess_tag}"


def is_transient_status(status_code: int) -> bool:
    """Return True for transient upstream status codes that justify failover."""
    return status_code in _TRANSIENT_STATUS_CODES


def _peer(host: str) -> str:
    return "gx10" if host == "x570" else "x570"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in _FALSE_VALUES


def _env_pos_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
        if value > 0:
            return value
    except ValueError:
        pass
    logger.warning("Invalid %s=%r; defaulting to %d", name, raw, default)
    return default


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Routing plan for one QFN request."""

    primary_host: str
    fallback_host: str
    primary_model: str
    fallback_model: str
    reason: str


class StickyQfnRouter:
    """Tracks per-host in-flight slots, cooldowns, and session pins for QFN."""

    def __init__(self, max_local_pins: int = 4096) -> None:
        """Initialize empty slot counters, cooldowns, local pin cache, and metrics."""
        self._max_local_pins = max_local_pins
        self._inflight: dict[str, int] = {h: 0 for h in HOSTS}
        self._cooldown_until: dict[str, float] = {h: 0.0 for h in HOSTS}
        self._local_pins: dict[str, tuple[str, float]] = {}
        self.metrics: dict[str, int] = {reason: 0 for reason in _ROUTE_REASONS}

    def reset(self) -> None:
        """Reset all mutable routing state (primarily for tests)."""
        self._inflight = {h: 0 for h in HOSTS}
        self._cooldown_until = {h: 0.0 for h in HOSTS}
        self._local_pins.clear()
        self.metrics = {reason: 0 for reason in _ROUTE_REASONS}

    @property
    def enabled(self) -> bool:
        """Return whether sticky primary+overflow QFN routing is enabled."""
        return _env_bool("LOCAL_QWEN_STICKY_ROUTING", True)

    def primary_max_slots(self) -> int:
        """Return max concurrent chat slots on primary x570 before overflowing to gx10."""
        return _env_pos_int("LOCAL_QWEN_PRIMARY_MAX_SLOTS", 1)

    def sticky_ttl_seconds(self) -> int:
        """Return sliding session pin TTL in seconds."""
        return _env_pos_int("LOCAL_QWEN_STICKY_TTL_SECONDS", 1800)

    def cooldown_seconds(self) -> int:
        """Return host cooldown window in seconds after transient failure."""
        return _env_pos_int("LOCAL_QWEN_COOLDOWN_SECONDS", 60)

    def hass_primary(self) -> str:
        """Return configured primary host for HASS Assist requests ('gx10' or 'x570')."""
        val = (os.getenv("LOCAL_QWEN_HASS_PRIMARY") or "gx10").strip().lower()
        return val if val in HOSTS else "gx10"

    def is_cooled_down(self, host: str) -> bool:
        """Return True while host is inside its transient-failure cooldown window."""
        return time.monotonic() < self._cooldown_until[host]

    def mark_transient_failure(self, host: str) -> None:
        """Place host into cooldown after a transient upstream failure."""
        self._cooldown_until[host] = time.monotonic() + self.cooldown_seconds()

    def acquire(self, host: str) -> Callable[[], None]:
        """Increment host in-flight counter and return an idempotent release callback."""
        self._inflight[host] += 1
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self._inflight[host] = max(0, self._inflight[host] - 1)

        return release

    def _evict_local_pins(self, now: float) -> None:
        expired = [k for k, (_, exp) in self._local_pins.items() if exp <= now]
        for k in expired:
            del self._local_pins[k]
        while len(self._local_pins) >= self._max_local_pins:
            oldest = next(iter(self._local_pins))
            del self._local_pins[oldest]

    async def get_pin(self, redis: Any, pool: str, session_id: str | None) -> str | None:
        """Look up pinned host for (pool, session_id) in Redis, falling back to local memory."""
        if not session_id:
            return None
        key = f"qfn:pin:{pool}:{session_id}"
        if redis is not None:
            try:
                val = await redis.get(key)
                if val in HOSTS:
                    return str(val)
            except Exception as exc:
                logger.warning("Sticky QFN Redis get_pin failed (pool=%s): %s", pool, type(exc).__name__)
        entry = self._local_pins.get(key)
        if entry is not None:
            host, exp = entry
            if exp > time.monotonic():
                return host
            self._local_pins.pop(key, None)
        return None

    async def set_pin(
        self,
        redis: Any,
        pool: str,
        session_id: str | None,
        host: str,
        ttl_seconds: int | None = None,
    ) -> None:
        """Persist host pin in local memory and Redis with sliding TTL."""
        if not session_id or host not in HOSTS:
            return
        ttl = ttl_seconds if ttl_seconds is not None and ttl_seconds > 0 else self.sticky_ttl_seconds()
        now = time.monotonic()
        key = f"qfn:pin:{pool}:{session_id}"
        self._local_pins.pop(key, None)
        self._evict_local_pins(now)
        self._local_pins[key] = (host, now + ttl)
        if redis is not None:
            try:
                await redis.setex(key, ttl, host)
            except Exception as exc:
                logger.warning("Sticky QFN Redis set_pin failed (pool=%s): %s", pool, type(exc).__name__)

    def _plan_chat_decision(self, pinned: str | None) -> tuple[RouteDecision, str | None, str]:
        if pinned is not None:
            fb = _peer(pinned)
            if not self.is_cooled_down(pinned):
                decision = RouteDecision(pinned, fb, CHAT_TARGETS[pinned], CHAT_TARGETS[fb], "pinned")
                return decision, pinned, "pinned"
            # Pinned host is cooled down: try peer first without overwriting pin until fallback succeeds.
            decision = RouteDecision(fb, pinned, CHAT_TARGETS[fb], CHAT_TARGETS[pinned], "failover_cooldown")
            return decision, None, "failover_cooldown"

        x570_cooled = self.is_cooled_down("x570")
        gx10_cooled = self.is_cooled_down("gx10")
        x570_avail = not x570_cooled and self._inflight["x570"] < self.primary_max_slots()

        if x570_avail:
            primary, fallback, reason = "x570", "gx10", "primary"
        elif not gx10_cooled:
            reason = "failover_cooldown" if x570_cooled else "overflow"
            primary, fallback = "gx10", "x570"
        else:
            primary, fallback, reason = "x570", "gx10", "primary"

        decision = RouteDecision(primary, fallback, CHAT_TARGETS[primary], CHAT_TARGETS[fallback], reason)
        return decision, primary, reason

    async def _resolve_chat_route(
        self, redis: Any, session_id: str | None, *, acquire_slot: bool
    ) -> tuple[RouteDecision, Callable[[], None] | None]:
        pinned = await self.get_pin(redis, "chat", session_id)
        decision, pin_target, metric_key = self._plan_chat_decision(pinned)
        release = self.acquire(decision.primary_host) if acquire_slot else None
        self.metrics[metric_key] += 1
        if pin_target is not None:
            try:
                await self.set_pin(redis, "chat", session_id, pin_target)
            except BaseException:
                if release is not None:
                    release()
                raise
        return decision, release

    async def reserve_chat_route(self, redis: Any, session_id: str | None) -> tuple[RouteDecision, Callable[[], None]]:
        """Atomically plan, acquire the primary slot, and persist the pin for a chat request."""
        decision, release = await self._resolve_chat_route(redis, session_id, acquire_slot=True)
        assert release is not None
        return decision, release

    async def select_chat_route(self, redis: Any, session_id: str | None) -> RouteDecision:
        """Select primary and fallback chat targets honoring sticky pins, slots, and cooldowns."""
        decision, _ = await self._resolve_chat_route(redis, session_id, acquire_slot=False)
        return decision

    async def repin(self, redis: Any, pool: str, session_id: str | None, host: str) -> None:
        """Update session pin after successful fallback and increment failover counter."""
        await self.set_pin(redis, pool, session_id, host)
        self.metrics["failover"] += 1

    def select_hass_route(self) -> RouteDecision:
        """Select primary and fallback HASS targets honoring cooldowns."""
        primary = self.hass_primary()
        fallback = _peer(primary)
        if self.is_cooled_down(primary) and not self.is_cooled_down(fallback):
            self.metrics["hass_failover"] += 1
            return RouteDecision(fallback, primary, HASS_TARGETS[fallback], HASS_TARGETS[primary], "hass_failover")
        self.metrics["hass_primary"] += 1
        return RouteDecision(primary, fallback, HASS_TARGETS[primary], HASS_TARGETS[fallback], "hass_primary")

    def record_hass_failover(self) -> None:
        """Increment HASS failover counter after runtime fallback succeeds."""
        self.metrics["hass_failover"] += 1

    def prometheus_lines(self) -> list[str]:
        """Format sticky QFN inflight and route counters in Prometheus text format."""
        lines = [
            "# HELP qfn_inflight_requests Current in-flight requests per Qwen3.8-Flash-Next host",
            "# TYPE qfn_inflight_requests gauge",
        ]
        for host in HOSTS:
            lines.append(f'qfn_inflight_requests{{host="{host}"}} {self._inflight[host]}')
        lines.append("# HELP qfn_route_total Cumulative Qwen3.8-Flash-Next sticky/overflow routing decisions")
        lines.append("# TYPE qfn_route_total counter")
        for reason, count in self.metrics.items():
            lines.append(f'qfn_route_total{{reason="{reason}"}} {count}')
        return lines
