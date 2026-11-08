"""/v1/models advertises for the agent-*-core tiers what the router itself accepts.

Each agent-*-core group mixes the config.yaml bootstrap deployment (local Qwen, max_input_tokens
240,896) with the free OpenRouter roster, and LiteLLM lists the widest *configured* limit of a
group. Before #738 the roster sync wrote each free model's catalog context, so the groups showed
up to 1,048,576 (stealth/crouton-alpha); since #738 only the bootstrap is configured and they show
240,896. Neither is what a request through the router can use: the completion pre-screen rejects
a prompt that does not fit ROUTER_CONTEXT_LIMITS (32,768 for agent-simple-core).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.main import DEFAULT_ROUTER_CONTEXT_LIMIT, ROUTER_CONTEXT_LIMITS, proxy_models

AGENT_TIERS = (
    "agent-simple-core",
    "agent-medium-core",
    "agent-complex-core",
    "agent-reasoning-core",
    "agent-advanced-core",
)


def _litellm_entry(model_id: str, window: int | None, output: int | None = None) -> dict:
    """An entry as LiteLLM v1.108 /v1/models lists it."""
    entry: dict = {"id": model_id, "object": "model", "created": 1677610602, "owned_by": "openai", "mode": "chat"}
    if window is not None:
        entry["max_input_tokens"] = window
    if output is not None:
        entry["max_output_tokens"] = output
    return entry


async def _listed(entries: list[dict]) -> dict[str, dict]:
    response = MagicMock(status_code=200)
    response.json.return_value = {"data": entries}
    client = AsyncMock()
    client.get.return_value = response
    with patch("router.main.get_http_client", return_value=client):
        result = await proxy_models()
    return {entry["id"]: entry for entry in json.loads(result.body)["data"]}


@pytest.mark.parametrize(
    ("reported", "case"),
    [
        (1048576, "widest free model configured (before #738)"),
        (240896, "only the local bootstrap configured (v0.1.93)"),
        (1000000, "catalog limits configured again, crouton-alpha gone"),
    ],
)
async def test_agent_tiers_never_advertise_more_than_the_router_accepts(reported, case):
    listed = await _listed([_litellm_entry(tier, reported, output=943718) for tier in AGENT_TIERS])
    for tier in AGENT_TIERS:
        limit = ROUTER_CONTEXT_LIMITS[tier]
        entry = listed[tier]
        assert entry["context_length"] == entry["max_input_tokens"] == min(reported, limit), (tier, case)
        assert entry["max_tokens"] <= entry["context_length"]
        assert entry["max_output_tokens"] <= entry["context_length"]


async def test_v0193_agent_tiers_as_clients_see_them():
    """The prod v0.1.93 listing: 240,896 everywhere, 943,718 output on simple-core (crouton-alpha)."""
    listed = await _listed(
        [_litellm_entry(tier, 240896, output=943718 if tier == "agent-simple-core" else 65536) for tier in AGENT_TIERS]
    )
    assert {tier: listed[tier]["context_length"] for tier in AGENT_TIERS} == {
        "agent-simple-core": 32768,
        "agent-medium-core": 240896,
        "agent-complex-core": 240896,
        "agent-reasoning-core": 240896,
        "agent-advanced-core": 240896,
    }
    assert listed["agent-simple-core"]["max_output_tokens"] == 32768
    assert listed["agent-advanced-core"]["max_output_tokens"] == 65536


async def test_single_model_groups_keep_their_window():
    listed = await _listed(
        [
            _litellm_entry("locallama-qwen", 240896),
            _litellm_entry("agy-gemini", 1048576, output=65536),
            _litellm_entry("locallama-nomic-embed", 2048),
            _litellm_entry("litellm-divorce-ia", 1050000),  # no router limit: listed as is
            _litellm_entry("whisper-1", None),
        ]
    )
    assert listed["locallama-qwen"]["context_length"] == 240896
    assert listed["agy-gemini"]["context_length"] == 1048576
    assert listed["agy-gemini"]["max_output_tokens"] == 65536
    assert listed["locallama-nomic-embed"]["context_length"] == 2048
    assert listed["litellm-divorce-ia"]["context_length"] == 1050000
    assert "context_length" not in listed["whisper-1"]


def test_every_agent_tier_has_an_explicit_router_limit():
    """Unlisted models fall back to the default in the pre-screen but are not capped in /v1/models."""
    for tier in AGENT_TIERS:
        assert tier in ROUTER_CONTEXT_LIMITS
    # The three middle tiers keep the pre-screen limit they always had (the default).
    for tier in ("agent-medium-core", "agent-complex-core", "agent-reasoning-core"):
        assert ROUTER_CONTEXT_LIMITS[tier] == DEFAULT_ROUTER_CONTEXT_LIMIT
