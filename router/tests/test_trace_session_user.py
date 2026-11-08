"""Trace-level user and session on the router's Langfuse spans, streaming or not.

Langfuse groups a trace into a session (and a user) from the ``session.id`` / ``user.id``
attributes that ``propagate_attributes`` stamps on spans started inside its context. Up to
v0.1.94 the router entered that context only for non-streaming requests, so a streaming
trace (every Hermes agent call) carried user and session in span metadata only.

These tests run the real Langfuse SDK with an in-memory exporter, so they check the exported
attributes, not calls on a mock.
"""

import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport
from langfuse import Langfuse, LangfuseOtelSpanAttributes
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from router.main import app

SESSION_ATTR = LangfuseOtelSpanAttributes.TRACE_SESSION_ID
USER_ATTR = LangfuseOtelSpanAttributes.TRACE_USER_ID
SESSION_ID = "20261109_101500_abc123"
KEY_USER = "boy-hermes"
SSE_CHUNKS = [
    b'data: {"choices": [{"delta": {"content": "po"}}]}\n\n',
    b'data: {"choices": [{"delta": {"content": "ng"}}]}\n\n',
    b"data: [DONE]\n\n",
]


class _SSEStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


@pytest.fixture
def langfuse_spans():
    """A real Langfuse client exporting to memory; yields (client, exporter)."""
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=f"pk-lf-test-{uuid.uuid4()}",
        secret_key="sk-lf-test",
        base_url="http://127.0.0.1:9",
        tracer_provider=TracerProvider(),
        span_exporter=exporter,
    )
    yield client, exporter
    client.shutdown()


def _litellm_client():
    ok = MagicMock(status_code=200)
    ok.json.return_value = {
        "choices": [{"message": {"content": "pong"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 1},
    }
    stream = MagicMock(status_code=200)
    stream.aiter_bytes = lambda: _SSEStream(SSE_CHUNKS)
    stream.aclose = AsyncMock()
    http = AsyncMock()
    http.post.return_value = ok
    http.send.return_value = stream
    http.build_request = MagicMock(return_value=MagicMock())
    return http


async def _as_hermes_key(request):
    """What _authenticate_client_request sets for a LiteLLM virtual key."""
    request.state.auth_key_alias = "hermes-agent-boy"
    request.state.auth_user_id = KEY_USER
    return "sk-virtual"


async def _chat(client_lf, *, stream, headers):
    with (
        patch.dict(os.environ, {"LITELLM_MASTER_KEY": "sk-test-master"}),
        patch("router.main._authenticate_client_request", side_effect=_as_hermes_key),
        patch("router.main.get_http_client", return_value=_litellm_client()),
        patch("router.main.get_langfuse", return_value=client_lf),
        patch("router.main.save_persisted_stats", new=AsyncMock()),
        patch("router.main.sync_cooldowns_from_valkey", new=AsyncMock()),
        patch("router.main.record_tool_usage"),
    ):
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "strata-qwen",
                    "stream": stream,
                    "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
                },
                headers={"Authorization": "Bearer sk-virtual", **headers},
            )
            body = await r.aread()
    return r, body


def _spans(client_lf, exporter):
    client_lf.flush()
    return {s.name: s for s in exporter.get_finished_spans()}


@pytest.mark.parametrize("stream", [True, False], ids=["streaming", "non-streaming"])
async def test_trace_carries_session_and_user(langfuse_spans, stream):
    client_lf, exporter = langfuse_spans
    r, body = await _chat(client_lf, stream=stream, headers={"x-session-id": SESSION_ID})

    assert r.status_code == 200
    assert b"pong" in body or b'"po"' in body
    spans = _spans(client_lf, exporter)
    assert {"triage-strata-qwen", "litellm-proxy"} <= spans.keys()
    for name in ("triage-strata-qwen", "litellm-proxy"):
        attrs = spans[name].attributes
        assert attrs.get(SESSION_ATTR) == SESSION_ID, name
        assert attrs.get(USER_ATTR) == KEY_USER, name


async def test_streaming_explicit_user_header_wins(langfuse_spans):
    client_lf, exporter = langfuse_spans
    await _chat(client_lf, stream=True, headers={"x-session-id": SESSION_ID, "x-user-id": "boy-other"})

    spans = _spans(client_lf, exporter)
    assert spans["triage-strata-qwen"].attributes.get(USER_ATTR) == "boy-other"
    assert spans["litellm-proxy"].attributes.get(USER_ATTR) == "boy-other"


async def test_streaming_context_is_closed_before_the_response_streams(langfuse_spans):
    """The context is exited in the request's task: nothing leaks into later spans."""
    client_lf, exporter = langfuse_spans
    await _chat(client_lf, stream=True, headers={"x-session-id": SESSION_ID})

    client_lf.start_observation(name="after-request").end()
    spans = _spans(client_lf, exporter)
    assert spans["triage-strata-qwen"].attributes.get(SESSION_ATTR) == SESSION_ID
    assert SESSION_ATTR not in spans["after-request"].attributes
    assert USER_ATTR not in spans["after-request"].attributes
