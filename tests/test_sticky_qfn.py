"""Tests for sticky session + slot-overflow + cooldown QFN routing."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml
from httpx import ASGITransport

import router.main as rm
from router.sticky_qfn import (
    CHAT_POOL_MODELS,
    HASS_POOL_MODEL,
    StickyQfnRouter,
    is_transient_status,
    make_pin_session_key,
)


class _SSEStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


@pytest.fixture(autouse=True)
def _reset_sticky_router():
    rm.sticky_qfn_router.reset()
    yield
    rm.sticky_qfn_router.reset()


def test_env_helpers_and_transient_status(monkeypatch):
    qfn = StickyQfnRouter()

    # Default config
    for var in (
        "LOCAL_QWEN_STICKY_ROUTING",
        "LOCAL_QWEN_PRIMARY_MAX_SLOTS",
        "LOCAL_QWEN_STICKY_TTL_SECONDS",
        "LOCAL_QWEN_COOLDOWN_SECONDS",
        "LOCAL_QWEN_HASS_PRIMARY",
    ):
        monkeypatch.delenv(var, raising=False)

    assert qfn.enabled is True
    assert qfn.primary_max_slots() == 1
    assert qfn.sticky_ttl_seconds() == 1800
    assert qfn.cooldown_seconds() == 60
    assert qfn.hass_primary() == "gx10"

    # Explicit overrides
    monkeypatch.setenv("LOCAL_QWEN_STICKY_ROUTING", "off")
    assert qfn.enabled is False
    monkeypatch.setenv("LOCAL_QWEN_STICKY_ROUTING", "  ")
    assert qfn.enabled is True
    monkeypatch.setenv("LOCAL_QWEN_PRIMARY_MAX_SLOTS", "2")
    assert qfn.primary_max_slots() == 2
    monkeypatch.setenv("LOCAL_QWEN_STICKY_TTL_SECONDS", "900")
    assert qfn.sticky_ttl_seconds() == 900
    monkeypatch.setenv("LOCAL_QWEN_COOLDOWN_SECONDS", "45")
    assert qfn.cooldown_seconds() == 45
    monkeypatch.setenv("LOCAL_QWEN_HASS_PRIMARY", "x570")
    assert qfn.hass_primary() == "x570"

    # Invalid overrides fall back cleanly
    monkeypatch.setenv("LOCAL_QWEN_PRIMARY_MAX_SLOTS", "0")
    assert qfn.primary_max_slots() == 1
    monkeypatch.setenv("LOCAL_QWEN_STICKY_TTL_SECONDS", "invalid")
    assert qfn.sticky_ttl_seconds() == 1800
    monkeypatch.setenv("LOCAL_QWEN_COOLDOWN_SECONDS", "-5")
    assert qfn.cooldown_seconds() == 60
    monkeypatch.setenv("LOCAL_QWEN_HASS_PRIMARY", "cloud")
    assert qfn.hass_primary() == "gx10"

    for code in (408, 429, 500, 502, 503, 504):
        assert is_transient_status(code) is True
    for code in (200, 400, 401, 404):
        assert is_transient_status(code) is False

    # Tenant-scoped opaque pin keys prevent cross-client session collisions
    assert make_pin_session_key(None, "client-a") is None
    assert make_pin_session_key("shared-id", None) == make_pin_session_key("shared-id", "")
    key_a = make_pin_session_key("shared-id", "divorce-ia")
    key_b = make_pin_session_key("shared-id", "hermes")
    assert key_a is not None and key_b is not None and key_a != key_b
    assert "shared-id" not in key_a and "divorce-ia" not in key_a


def test_acquire_release_is_idempotent():
    qfn = StickyQfnRouter()
    release = qfn.acquire("x570")
    assert qfn._inflight["x570"] == 1
    release()
    assert qfn._inflight["x570"] == 0
    # Second call must be a no-op
    release()
    assert qfn._inflight["x570"] == 0


@pytest.mark.asyncio
async def test_pin_storage_redis_and_bounded_local_fallback():
    qfn = StickyQfnRouter(max_local_pins=2)

    # Empty session or invalid host are no-ops
    assert await qfn.get_pin(None, "chat", None) is None
    await qfn.set_pin(None, "chat", None, "x570")
    await qfn.set_pin(None, "chat", "s0", "unknown")
    assert qfn._local_pins == {}

    # Redis hit wins
    redis_ok = AsyncMock()
    redis_ok.get.return_value = "gx10"
    assert await qfn.get_pin(redis_ok, "chat", "s1") == "gx10"
    await qfn.set_pin(redis_ok, "chat", "s1", "gx10", ttl_seconds=120)
    redis_ok.setex.assert_awaited_once_with("qfn:pin:chat:s1", 120, "gx10")

    # Redis returns invalid/None -> falls back to local pins
    redis_miss = AsyncMock()
    redis_miss.get.return_value = "invalid"
    assert await qfn.get_pin(redis_miss, "chat", "s1") == "gx10"
    assert await qfn.get_pin(redis_miss, "chat", "missing") is None

    # Redis exception on get/setex falls back cleanly to local pins
    redis_err = AsyncMock()
    redis_err.get.side_effect = RuntimeError("valkey down")
    redis_err.setex.side_effect = RuntimeError("valkey down")
    await qfn.set_pin(redis_err, "chat", "s2", "x570", ttl_seconds=60)
    assert await qfn.get_pin(redis_err, "chat", "s2") == "x570"

    # Capacity eviction drops oldest entry; expired entry is purged before eviction
    await qfn.set_pin(None, "chat", "s3", "gx10", ttl_seconds=60)
    assert "qfn:pin:chat:s1" not in qfn._local_pins
    assert len(qfn._local_pins) == 2

    qfn._local_pins["qfn:pin:chat:s2"] = ("x570", 0.0)
    assert await qfn.get_pin(None, "chat", "s2") is None

    qfn._local_pins["qfn:pin:chat:expired"] = ("x570", 0.0)
    await qfn.set_pin(None, "chat", "s4", "x570", ttl_seconds=60)
    assert "qfn:pin:chat:expired" not in qfn._local_pins


@pytest.mark.asyncio
async def test_select_chat_route_pinning_overflow_and_cooldowns(monkeypatch):
    qfn = StickyQfnRouter()
    monkeypatch.setenv("LOCAL_QWEN_PRIMARY_MAX_SLOTS", "1")
    redis = AsyncMock()
    redis.get.return_value = None

    # 1. Fresh session lands on primary x570 and pins it
    d1 = await qfn.select_chat_route(redis, "sess-1")
    assert (d1.primary_host, d1.fallback_host, d1.primary_model, d1.fallback_model, d1.reason) == (
        "x570",
        "gx10",
        "locallama-qwen",
        "gb10-strata",
        "primary",
    )
    rel1 = qfn.acquire(d1.primary_host)

    # 2. Second concurrent session overflows to gx10
    d2 = await qfn.select_chat_route(redis, "sess-2")
    assert (d2.primary_host, d2.fallback_host, d2.primary_model, d2.fallback_model, d2.reason) == (
        "gx10",
        "x570",
        "gb10-strata",
        "locallama-qwen",
        "overflow",
    )

    # 3. Subsequent turn on sess-1 stays pinned to x570 even when x570 slot is full
    d1_next = await qfn.select_chat_route(redis, "sess-1")
    assert d1_next.primary_host == "x570"
    assert d1_next.reason == "pinned"

    # 4. Subsequent turn on sess-2 stays pinned to gx10 even after sess-1 releases x570
    rel1()
    d2_next = await qfn.select_chat_route(redis, "sess-2")
    assert d2_next.primary_host == "gx10"
    assert d2_next.reason == "pinned"

    # 5. Cooldown on pinned host routes via peer WITHOUT overwriting pin until repin()
    qfn.mark_transient_failure("x570")
    d1_cooled = await qfn.select_chat_route(redis, "sess-1")
    assert d1_cooled.primary_host == "gx10"
    assert d1_cooled.fallback_host == "x570"
    assert d1_cooled.reason == "failover_cooldown"
    assert qfn._local_pins["qfn:pin:chat:sess-1"][0] == "x570"

    await qfn.repin(redis, "chat", "sess-1", "gx10")
    assert qfn._local_pins["qfn:pin:chat:sess-1"][0] == "gx10"
    assert qfn.metrics["failover"] == 1

    # 6. Fresh session while x570 cooled down goes to gx10 with failover_cooldown
    d3 = await qfn.select_chat_route(redis, "sess-3")
    assert d3.primary_host == "gx10"
    assert d3.reason == "failover_cooldown"

    # 7. Fresh session when BOTH hosts cooled down defaults to x570 primary
    qfn.mark_transient_failure("gx10")
    d4 = await qfn.select_chat_route(redis, "sess-4")
    assert d4.primary_host == "x570"
    assert d4.reason == "primary"

    # 8. Unidentified session (session_id=None) still routes & records failover safely
    qfn.reset()
    d_anon = await qfn.select_chat_route(redis, None)
    assert d_anon.primary_host == "x570"
    assert d_anon.reason == "primary"
    await qfn.repin(redis, "chat", None, "gx10")
    assert qfn.metrics["failover"] == 1

    # 9. Concurrent reserve_chat_route() reserves slot BEFORE awaiting set_pin (no race)
    import asyncio

    qfn.reset()
    entered_setex = asyncio.Event()
    release_setex = asyncio.Event()

    async def slow_setex(key, ttl, val):
        entered_setex.set()
        await release_setex.wait()

    redis_slow = AsyncMock()
    redis_slow.get.return_value = None
    redis_slow.setex.side_effect = slow_setex

    task1 = asyncio.create_task(qfn.reserve_chat_route(redis_slow, "race-1"))
    await entered_setex.wait()
    task2 = asyncio.create_task(qfn.reserve_chat_route(redis_slow, "race-2"))
    await asyncio.sleep(0)
    release_setex.set()
    (r1_dec, r1_rel), (r2_dec, r2_rel) = await asyncio.gather(task1, task2)
    r1_rel()
    r2_rel()
    assert r1_dec.primary_host == "x570" and r1_dec.reason == "primary"
    assert r2_dec.primary_host == "gx10" and r2_dec.reason == "overflow"
    assert qfn._inflight == {"x570": 0, "gx10": 0}

    # Cooldown failover via reserve_chat_route preserves pin (pin_target=None branch)
    qfn.mark_transient_failure("x570")
    c_dec, c_rel = await qfn.reserve_chat_route(redis, "race-1")
    c_rel()
    assert c_dec.primary_host == "gx10" and c_dec.reason == "failover_cooldown"
    assert qfn._local_pins["qfn:pin:chat:race-1"][0] == "x570"


def test_select_hass_route_and_prometheus_lines(monkeypatch):
    qfn = StickyQfnRouter()
    monkeypatch.delenv("LOCAL_QWEN_HASS_PRIMARY", raising=False)

    d1 = qfn.select_hass_route()
    assert (d1.primary_host, d1.primary_model, d1.fallback_model, d1.reason) == (
        "gx10",
        "strata-qwen-hass",
        "locallama-qwen-hass",
        "hass_primary",
    )

    qfn.mark_transient_failure("gx10")
    d2 = qfn.select_hass_route()
    assert (d2.primary_host, d2.primary_model, d2.fallback_model, d2.reason) == (
        "x570",
        "locallama-qwen-hass",
        "strata-qwen-hass",
        "hass_failover",
    )

    qfn.mark_transient_failure("x570")
    d3 = qfn.select_hass_route()
    assert d3.primary_host == "gx10"
    assert d3.reason == "hass_primary"

    monkeypatch.setenv("LOCAL_QWEN_HASS_PRIMARY", "x570")
    qfn.reset()
    d4 = qfn.select_hass_route()
    assert d4.primary_host == "x570"
    assert d4.primary_model == "locallama-qwen-hass"
    qfn.record_hass_failover()

    prom = "\n".join(qfn.prometheus_lines())
    assert 'qfn_inflight_requests{host="x570"} 0' in prom
    assert 'qfn_inflight_requests{host="gx10"} 0' in prom
    assert 'qfn_route_total{reason="hass_primary"} 1' in prom
    assert 'qfn_route_total{reason="hass_failover"} 1' in prom


def _json_ok(content: str = "ok") -> MagicMock:
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    return resp


def _json_err(status_code: int, text: str = "err") -> MagicMock:
    return MagicMock(status_code=status_code, text=text)


async def _post_chat(http_client: AsyncMock, model: str, *, session_id: str | None = "sess-a", stream: bool = False):
    headers = {"Authorization": "Bearer test-key"}
    if session_id:
        headers["x-session-id"] = session_id
    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-key"}),
        patch("router.main.get_http_client", return_value=http_client),
        patch("router.main.get_redis", return_value=None),
        patch("router.main.get_langfuse", return_value=None),
        patch("router.main.save_persisted_stats", new=AsyncMock()),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        patch("router.main.record_tool_usage"),
    ):
        transport = ASGITransport(app=rm.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": model, "stream": stream, "messages": [{"role": "user", "content": "hi"}]},
                headers=headers,
            )
            await resp.aread()
            return resp


@pytest.mark.asyncio
@pytest.mark.parametrize("client_model", sorted(CHAT_POOL_MODELS))
async def test_chat_pool_routing_sticky_and_overflow(client_model):
    http = AsyncMock()
    http.post.return_value = _json_ok()

    # Hold one x570 slot to force overflow for a new session
    release_x570 = rm.sticky_qfn_router.acquire("x570")
    try:
        r_over = await _post_chat(http, client_model, session_id="overflow-sess")
        assert r_over.status_code == 200
        assert r_over.headers["x-llm-backend"] == "gx10"
        assert r_over.headers["x-llm-route-reason"] == "overflow"
        assert http.post.call_args.kwargs["json"]["model"] == "gb10-strata"
    finally:
        release_x570()

    # Now x570 is free: fresh session lands on x570
    r_prim = await _post_chat(http, client_model, session_id="primary-sess")
    assert r_prim.status_code == 200
    assert r_prim.headers["x-llm-backend"] == "x570"
    assert r_prim.headers["x-llm-route-reason"] == "primary"
    assert http.post.call_args.kwargs["json"]["model"] == "locallama-qwen"

    # Follow-up on overflow-sess stays pinned to gx10 even though x570 is free
    r_pin = await _post_chat(http, client_model, session_id="overflow-sess")
    assert r_pin.status_code == 200
    assert r_pin.headers["x-llm-backend"] == "gx10"
    assert r_pin.headers["x-llm-route-reason"] == "pinned"
    assert rm.sticky_qfn_router._inflight == {"x570": 0, "gx10": 0}


@pytest.mark.asyncio
async def test_chat_pool_failover_repin_and_streaming():
    # Primary fails with 503 stream status, fallback succeeds streaming SSE
    err_stream = MagicMock(status_code=503)
    err_stream.aread = AsyncMock(return_value=b"busy")
    err_stream.aclose = AsyncMock()

    ok_stream = MagicMock(status_code=200)
    ok_stream.aiter_bytes = lambda: _SSEStream(
        [b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n', b"data: [DONE]\n\n"]
    )
    ok_stream.aclose = AsyncMock()

    http = AsyncMock()
    http.build_request = MagicMock(return_value=MagicMock())
    http.send.side_effect = [err_stream, ok_stream]

    r = await _post_chat(http, "strata-qwen", session_id="stream-failover", stream=True)
    assert r.status_code == 200
    assert r.headers["x-llm-backend"] == "gx10"
    assert r.headers["x-llm-route-reason"] == "failover"
    assert rm.sticky_qfn_router.is_cooled_down("x570") is True
    assert rm.sticky_qfn_router._local_pins[f"qfn:pin:chat:{make_pin_session_key('stream-failover')}"][0] == "gx10"
    assert rm.sticky_qfn_router._inflight == {"x570": 0, "gx10": 0}


@pytest.mark.asyncio
async def test_chat_pool_non_transient_and_double_failure_behavior():
    # Non-transient 400 must NOT fail over or trip cooldown
    http_400 = AsyncMock()
    http_400.post.return_value = _json_err(400)
    r400 = await _post_chat(http_400, "local-qwen", session_id="err-400")
    assert r400.status_code == 400
    assert http_400.post.call_count == 1
    assert rm.sticky_qfn_router.is_cooled_down("x570") is False

    # strata-qwen double transient failure must NEVER escalate to Ollama/cloud
    rm.sticky_qfn_router.reset()
    http_strata_503 = AsyncMock()
    http_strata_503.post.side_effect = [_json_err(503), _json_err(503)]
    r_strata = await _post_chat(http_strata_503, "strata-qwen", session_id="double-strata")
    assert r_strata.status_code == 503
    assert [c.kwargs["json"]["model"] for c in http_strata_503.post.call_args_list] == [
        "locallama-qwen",
        "gb10-strata",
    ]
    assert rm.sticky_qfn_router.is_cooled_down("x570") is True
    assert rm.sticky_qfn_router.is_cooled_down("gx10") is True

    # local-qwen double transient failure escalates to ollama-deepseek-v4-pro
    rm.sticky_qfn_router.reset()
    http_local_escalate = AsyncMock()
    http_local_escalate.post.side_effect = [_json_err(503), _json_err(502), _json_ok("cloud ok")]
    r_local = await _post_chat(http_local_escalate, "local-qwen", session_id="double-local")
    assert r_local.status_code == 200
    assert [c.kwargs["json"]["model"] for c in http_local_escalate.post.call_args_list] == [
        "locallama-qwen",
        "gb10-strata",
        "ollama-deepseek-v4-pro",
    ]

    # local-qwen fallback non-transient 400 does NOT escalate to cloud
    rm.sticky_qfn_router.reset()
    http_fb_400 = AsyncMock()
    http_fb_400.post.side_effect = [_json_err(503), _json_err(400)]
    r_fb400 = await _post_chat(http_fb_400, "local-qwen", session_id="fb-400")
    assert r_fb400.status_code == 400
    assert [c.kwargs["json"]["model"] for c in http_fb_400.post.call_args_list] == [
        "locallama-qwen",
        "gb10-strata",
    ]


@pytest.mark.asyncio
async def test_hass_pool_routing_and_lan_only_failover():
    http_ok = AsyncMock()
    http_ok.post.return_value = _json_ok("lights on")
    r_ok = await _post_chat(http_ok, HASS_POOL_MODEL, session_id="hass-1")
    assert r_ok.status_code == 200
    assert r_ok.headers["x-llm-backend"] == "gx10"
    assert r_ok.headers["x-llm-route-reason"] == "hass_primary"
    assert http_ok.post.call_args.kwargs["json"]["model"] == "strata-qwen-hass"

    # Transient GX10 error fails over to x570 locallama-qwen-hass
    http_fb = AsyncMock()
    http_fb.post.side_effect = [_json_err(503), _json_ok("lights on fallback")]
    r_fb = await _post_chat(http_fb, HASS_POOL_MODEL, session_id="hass-2")
    assert r_fb.status_code == 200
    assert r_fb.headers["x-llm-backend"] == "x570"
    assert r_fb.headers["x-llm-route-reason"] == "hass_failover"
    assert [c.kwargs["json"]["model"] for c in http_fb.post.call_args_list] == [
        "strata-qwen-hass",
        "locallama-qwen-hass",
    ]

    # Double transient HASS failure stays strictly LAN-only (never cloud)
    rm.sticky_qfn_router.reset()
    http_fail = AsyncMock()
    http_fail.post.side_effect = [_json_err(503), _json_err(503)]
    r_fail = await _post_chat(http_fail, HASS_POOL_MODEL, session_id="hass-3")
    assert r_fail.status_code == 503
    assert [c.kwargs["json"]["model"] for c in http_fail.post.call_args_list] == [
        "strata-qwen-hass",
        "locallama-qwen-hass",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "explicit_model",
    ["locallama-qwen", "gb10-strata", "locallama-qwen-hass", "strata-qwen-hass"],
)
async def test_explicit_targets_bypass_sticky_pool(explicit_model):
    http = AsyncMock()
    http.post.return_value = _json_ok()
    r = await _post_chat(http, explicit_model, session_id="explicit-sess")
    assert r.status_code == 200
    assert "x-llm-backend" not in r.headers
    assert http.post.call_args.kwargs["json"]["model"] == explicit_model


@pytest.mark.asyncio
async def test_sticky_routing_kill_switch(monkeypatch):
    monkeypatch.setenv("LOCAL_QWEN_STICKY_ROUTING", "false")
    http = AsyncMock()
    http.post.return_value = _json_ok()
    r = await _post_chat(http, "strata-qwen", session_id="kill-switch")
    assert r.status_code == 200
    assert "x-llm-backend" not in r.headers
    assert http.post.call_args.kwargs["json"]["model"] == "strata-qwen"


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_qfn_metrics():
    rm.sticky_qfn_router.acquire("x570")
    transport = ASGITransport(app=rm.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch("router.main.sync_stats_from_valkey", new=AsyncMock()),
            patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        ):
            r = await client.get("/metrics")
            assert r.status_code == 200
            assert 'qfn_inflight_requests{host="x570"} 1' in r.text
            assert 'qfn_inflight_requests{host="gx10"} 0' in r.text
            assert 'qfn_route_total{reason="primary"} 0' in r.text


def test_context_limits_and_litellm_fallback_contracts():
    for model in ("gb10-strata", "locallama-qwen", "strata-qwen-hass", "locallama-qwen-hass", "local-qwen-hass"):
        assert rm.ROUTER_CONTEXT_LIMITS[model] == 262144

    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "litellm/config.yaml").read_text())
    fallbacks = {name: targets for entry in config["litellm_settings"]["fallbacks"] for name, targets in entry.items()}
    assert fallbacks["local-qwen"] == ["gb10-strata", "ollama-deepseek-v4-pro", "openrouter-auto"]
    assert fallbacks["local-qwen-hass"] == ["strata-qwen-hass"]
    assert "strata-qwen-hass" not in fallbacks
    assert "locallama-qwen-hass" not in fallbacks
    assert "strata-qwen-hass" in config["litellm_settings"]["public_model_groups"]

    # Langfuse managed model definition includes strata-qwen-hass
    strata_defs = [entry for entry in rm.LANGFUSE_MANAGED_MODELS if entry[0] == "strata-qwen-model-def"]
    assert len(strata_defs) == 1
    assert re.match(strata_defs[0][2], "strata-qwen-hass")
    assert re.match(strata_defs[0][2], "openai/strata-qwen-hass")


@pytest.mark.asyncio
async def test_cancellation_setup_errors_cooldown_repin_and_stream_failure_coverage(monkeypatch):
    qfn = StickyQfnRouter()
    monkeypatch.setenv("LOCAL_QWEN_PRIMARY_MAX_SLOTS", "1")

    # 1. Cancellation during set_pin releases acquired slot (and select_chat_route re-raises cleanly)
    redis_cancel = AsyncMock()
    redis_cancel.get.return_value = None
    redis_cancel.setex.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await qfn.reserve_chat_route(redis_cancel, "cancel-sess-1")
    assert qfn._inflight == {"x570": 0, "gx10": 0}
    with pytest.raises(asyncio.CancelledError):
        await qfn.select_chat_route(redis_cancel, "cancel-sess-2")

    # 2. Mid-stream error marks host transient failure without leaking slot
    class _BrokenSSE:
        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            raise RuntimeError("mid-stream reset")

    broken_stream = MagicMock(status_code=200)
    broken_stream.aiter_bytes = lambda: _BrokenSSE()
    broken_stream.aclose = AsyncMock()

    http_stream_err = AsyncMock()
    http_stream_err.build_request = MagicMock(return_value=MagicMock())
    http_stream_err.send.return_value = broken_stream

    r_stream_err = await _post_chat(http_stream_err, "strata-qwen", session_id="stream-err", stream=True)
    assert r_stream_err.status_code == 200
    assert rm.sticky_qfn_router.is_cooled_down("x570") is True
    assert rm.sticky_qfn_router._inflight == {"x570": 0, "gx10": 0}

    # 2b. Fallback mid-stream error marks fallback host cooled down cleanly too
    rm.sticky_qfn_router.reset()
    err_503 = MagicMock(status_code=503)
    err_503.aread = AsyncMock(return_value=b"busy")
    err_503.aclose = AsyncMock()
    broken_fb_stream = MagicMock(status_code=200)
    broken_fb_stream.aiter_bytes = lambda: _BrokenSSE()
    broken_fb_stream.aclose = AsyncMock()
    http_fb_stream_err = AsyncMock()
    http_fb_stream_err.build_request = MagicMock(return_value=MagicMock())
    http_fb_stream_err.send.side_effect = [err_503, broken_fb_stream]
    r_fb_stream_err = await _post_chat(http_fb_stream_err, "strata-qwen", session_id="fb-stream-err", stream=True)
    assert r_fb_stream_err.status_code == 200
    assert rm.sticky_qfn_router.is_cooled_down("x570") is True
    assert rm.sticky_qfn_router.is_cooled_down("gx10") is True
    assert rm.sticky_qfn_router._inflight == {"x570": 0, "gx10": 0}

    # 3. Non-sticky stream error covers False branch of on_stream_error check
    http_non_sticky_err = AsyncMock()
    http_non_sticky_err.build_request = MagicMock(return_value=MagicMock())
    broken_stream_2 = MagicMock(status_code=200)
    broken_stream_2.aiter_bytes = lambda: _BrokenSSE()
    broken_stream_2.aclose = AsyncMock()
    http_non_sticky_err.send.return_value = broken_stream_2
    r_explicit_err = await _post_chat(http_non_sticky_err, "gb10-strata", session_id="exp-stream-err", stream=True)
    assert r_explicit_err.status_code == 200

    # 4. Pinned host in cooldown repins to peer upon cooldown failover success
    rm.sticky_qfn_router.reset()
    pin_key = make_pin_session_key("cool-repin")
    assert pin_key is not None
    await rm.sticky_qfn_router.set_pin(None, "chat", pin_key, "x570")
    rm.sticky_qfn_router.mark_transient_failure("x570")
    http_cool_ok = AsyncMock()
    http_cool_ok.post.return_value = _json_ok("served on gx10")
    r_cool = await _post_chat(http_cool_ok, "strata-qwen", session_id="cool-repin")
    assert r_cool.status_code == 200
    assert r_cool.headers["x-llm-backend"] == "gx10"
    assert r_cool.headers["x-llm-route-reason"] == "failover_cooldown"
    assert rm.sticky_qfn_router._local_pins[f"qfn:pin:chat:{pin_key}"][0] == "gx10"

    # 5. Setup error inside execute_proxy before HTTP releases slot via outer finally
    rm.sticky_qfn_router.reset()
    with patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-key"}):
        transport = ASGITransport(app=rm.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with (
                patch(
                    "router.main._validate_litellm_master_key",
                    side_effect=rm.HTTPException(status_code=500, detail="setup key error"),
                ),
                patch("router.main.get_redis", return_value=None),
                patch("router.main.get_langfuse", return_value=None),
                patch("router.main.save_persisted_stats", new=AsyncMock()),
                patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
            ):
                r_setup = await client.post(
                    "/v1/chat/completions",
                    json={"model": "strata-qwen", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer test-key", "x-session-id": "setup-err"},
                )
    assert r_setup.status_code == 500
    assert rm.sticky_qfn_router._inflight == {"x570": 0, "gx10": 0}

    # 6. Cancellation during repin after failover_cooldown releases slot cleanly
    rm.sticky_qfn_router.reset()
    pin_key_2 = make_pin_session_key("repin-cancel")
    assert pin_key_2 is not None
    await rm.sticky_qfn_router.set_pin(None, "chat", pin_key_2, "x570")
    rm.sticky_qfn_router.mark_transient_failure("x570")
    http_cool_cancel = AsyncMock()
    http_cool_cancel.post.return_value = _json_ok("ok")
    with patch.dict(os.environ, {"LITELLM_MASTER_KEY": "test-key"}):
        transport = ASGITransport(app=rm.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with (
                patch("router.main.get_http_client", return_value=http_cool_cancel),
                patch("router.main.get_redis", return_value=None),
                patch("router.main.get_langfuse", return_value=None),
                patch("router.main.save_persisted_stats", new=AsyncMock()),
                patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
                patch("router.main.record_tool_usage"),
                patch.object(rm.sticky_qfn_router, "repin", side_effect=asyncio.CancelledError()),
                pytest.raises(asyncio.CancelledError),
            ):
                await client.post(
                    "/v1/chat/completions",
                    json={"model": "strata-qwen", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer test-key", "x-session-id": "repin-cancel"},
                )
    assert rm.sticky_qfn_router._inflight == {"x570": 0, "gx10": 0}
