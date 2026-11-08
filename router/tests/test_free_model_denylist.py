"""stealth/crouton-alpha is listed free with tools but is region-blocked (upstream 403).

OpenRouter's public catalog still lists it (2026-11-08: free, tools, 1,048,576 context), so the
roster sync would register it again on every run without the denylist entry.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import router.main as main

CROUTON = {
    "id": "stealth/crouton-alpha",
    "name": "Stealth: Crouton Alpha",
    "supported_parameters": ["tools", "tool_choice", "reasoning"],
    "pricing": {"prompt": "0", "completion": "0"},
    "context_length": 1048576,
}
GEMMA = {
    "id": "google/gemma-4-31b-it:free",
    "name": "Google: Gemma 4 31B (free)",
    "supported_parameters": ["tools", "tool_choice"],
    "pricing": {"prompt": "0", "completion": "0"},
    "context_length": 262144,
}


def _catalog_client(*rows: dict) -> AsyncMock:
    catalog = MagicMock(status_code=200)
    catalog.json.return_value = {"data": list(rows)}
    client = AsyncMock()
    client.get.return_value = catalog
    client.post.return_value = MagicMock(status_code=200)
    return client


def test_crouton_alpha_is_denylisted():
    assert any(CROUTON["id"].startswith(prefix) for prefix in main.FREE_MODEL_DENYLIST)
    assert not any(GEMMA["id"].startswith(prefix) for prefix in main.FREE_MODEL_DENYLIST)


async def test_crouton_alpha_is_left_out_of_the_free_roster():
    client = _catalog_client(CROUTON, GEMMA)
    with patch.object(main, "get_http_client", return_value=client), patch.object(main, "_AA_SCORES_LOADED", True):
        free = await main._fetch_openrouter_free_models()
    assert [m["id"] for m in free] == [GEMMA["id"]]


async def test_crouton_alpha_is_never_registered_in_an_agent_tier():
    client = _catalog_client(CROUTON, GEMMA)
    with (
        patch.object(main, "get_http_client", return_value=client),
        patch.object(main, "_AA_SCORES_LOADED", True),
        patch.object(main, "_purge_stale_deployments", new_callable=AsyncMock),
        patch.dict(os.environ, {"DATABASE_URL": "postgresql://u:p@127.0.0.1:5432/db"}),
    ):
        await main.sync_adaptive_router_roster("sk-test")
    targets = {call.kwargs["json"]["litellm_params"]["model"] for call in client.post.call_args_list}
    assert targets == {f"openrouter/{GEMMA['id']}"}
