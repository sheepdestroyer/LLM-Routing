"""Model sync leaves unchanged deployments alone and still applies real changes.

LiteLLM's GET /model/info never returns api_key (it pops it) and masks other credential-like
params, so comparing every desired param with what /model/info lists saw each deployment with an
api_key as changed: prod v0.1.93 logged "updated: 27, unchanged: 5" on every run. The write-only
params are now tracked through an HMAC digest stored on the deployment instead. Ignoring them
outright would be the dangerous fix: a changed key would never reach LiteLLM.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from router.model_sync import WRITE_ONLY_DIGEST_KEY, ModelRegistrySync, is_write_only_param

MASTER = "sk-test-master"


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.patch.return_value = MagicMock(status_code=200)
    client.post.return_value = MagicMock(status_code=200)
    return client


@pytest.fixture
def engine(mock_client):
    return ModelRegistrySync(
        litellm_url="http://litellm:4000",
        master_key=MASTER,
        strata_server_url="http://192.168.0.31:8080",
        client=mock_client,
    )


def _targets(engine: ModelRegistrySync) -> list[dict]:
    return [
        *engine.build_locallama_models(),
        *engine.build_strata_models(),
        *engine.build_agy_models(),
        *engine.build_ollama_models(),
        *engine.build_openrouter_models(),
    ]


def _target(engine: ModelRegistrySync, name: str) -> dict:
    return next(t for t in _targets(engine) if t["model_name"] == name)


def _listed(engine, written: dict, *, digest: str | bool | None = True) -> dict:
    """The DB row GET /model/info lists after `written` was synced: write-only params absent.

    digest=True stores the digest of what was written, None/False stores none (a row written
    before this fix), a string stores that value.
    """
    params = {k: v for k, v in written["litellm_params"].items() if not is_write_only_param(k, v)}
    info = {"id": f"id-{written['model_name']}", "db_model": True, **written.get("model_info", {})}
    stored = engine.write_only_digest(written["litellm_params"]) if digest is True else digest
    if stored:
        info[WRITE_ONLY_DIGEST_KEY] = stored
    return {"model_name": written["model_name"], "litellm_params": params, "model_info": info}


def _grouped(*rows: dict) -> dict[str, list[dict]]:
    return {row["model_name"]: [row] for row in rows}


def _with_params(target: dict, **params) -> dict:
    return {**target, "litellm_params": {**target["litellm_params"], **params}}


def _patch_payload(mock_client) -> dict:
    assert mock_client.patch.call_count == 1
    return mock_client.patch.call_args.kwargs["json"]


@pytest.mark.parametrize(
    "name",
    [t["model_name"] for t in _targets(ModelRegistrySync(litellm_url="http://x", master_key=MASTER))],
)
async def test_an_unchanged_deployment_is_not_patched(engine, mock_client, name):
    target = _target(engine, name)
    action, changed = await engine.upsert_model(target, _grouped(_listed(engine, target)))
    assert (action, changed) == ("unchanged", False)
    mock_client.patch.assert_not_called()
    mock_client.post.assert_not_called()


async def test_a_full_sync_of_unchanged_deployments_patches_nothing(engine, mock_client):
    rows = [_listed(engine, t) for t in _targets(engine)]
    info = MagicMock(status_code=200)
    info.json.return_value = {"data": rows}
    agy = MagicMock(status_code=200)
    agy.json.return_value = {"models": [{"id": "gemini-3.8-flash-low"}]}
    mock_client.get.side_effect = [info, agy, info, info]

    results = await engine.sync_all_models()

    assert results["updated"] == 0
    assert results["unchanged"] == len(rows) == 32
    mock_client.patch.assert_not_called()


async def test_the_first_sync_after_the_upgrade_stamps_the_digest_once(engine, mock_client):
    target = _target(engine, "locallama-qwen")
    action, _ = await engine.upsert_model(target, _grouped(_listed(engine, target, digest=None)))
    assert action == "updated"
    payload = _patch_payload(mock_client)
    assert payload["model_info"] == {
        "id": "id-locallama-qwen",
        WRITE_ONLY_DIGEST_KEY: engine.write_only_digest(target["litellm_params"]),
    }
    assert payload["litellm_params"] == target["litellm_params"]  # the key comes from the spec

    mock_client.patch.reset_mock()
    action, _ = await engine.upsert_model(target, _grouped(_listed(engine, target)))
    assert action == "unchanged"
    mock_client.patch.assert_not_called()


async def test_a_changed_key_is_still_applied(engine, mock_client):
    """/model/info lists the same visible params either way: only the digest sees the change."""
    old = _target(engine, "agy-gemini")
    new = _with_params(old, api_key="rotated-token")
    listed = _listed(engine, old)
    assert listed["litellm_params"] == _listed(engine, new)["litellm_params"]

    action, _ = await engine.upsert_model(new, _grouped(listed))

    assert action == "updated"
    payload = _patch_payload(mock_client)
    assert payload["litellm_params"]["api_key"] == "rotated-token"
    assert payload["model_info"][WRITE_ONLY_DIGEST_KEY] == engine.write_only_digest(new["litellm_params"])
    assert payload["model_info"][WRITE_ONLY_DIGEST_KEY] != listed["model_info"][WRITE_ONLY_DIGEST_KEY]


async def test_a_changed_env_reference_key_is_applied(engine, mock_client):
    old = _target(engine, "ollama-deepseek-v4-pro")
    new = _with_params(old, api_key="os.environ/OLLAMA_API_KEY_2")
    action, _ = await engine.upsert_model(new, _grouped(_listed(engine, old)))
    assert action == "updated"
    assert _patch_payload(mock_client)["litellm_params"]["api_key"] == "os.environ/OLLAMA_API_KEY_2"


async def test_a_changed_price_is_applied(engine, mock_client):
    new = _target(engine, "ollama-deepseek-v4-flash")
    old = _with_params(new, input_cost_per_token=0.0000001)
    action, _ = await engine.upsert_model(new, _grouped(_listed(engine, old)))
    assert action == "updated"
    payload = _patch_payload(mock_client)
    assert payload["litellm_params"]["input_cost_per_token"] == 0.00000014
    assert payload["litellm_params"]["api_key"] == "os.environ/OLLAMA_API_KEY"


async def test_a_changed_limit_is_applied(engine, mock_client):
    new = _target(engine, "locallama-qwen")
    old = {**new, "model_info": {**new["model_info"], "max_input_tokens": 131072}}
    action, _ = await engine.upsert_model(new, _grouped(_listed(engine, old)))
    assert action == "updated"
    payload = _patch_payload(mock_client)
    assert payload["model_info"]["max_input_tokens"] == 240896
    assert payload["litellm_params"]["api_key"] == "local-token"


async def test_visible_param_drift_resends_the_key_from_the_spec(engine, mock_client):
    new = _target(engine, "strata-qwen")
    old = _with_params(new, api_base="http://127.0.0.1:8087/v1")
    action, _ = await engine.upsert_model(new, _grouped(_listed(engine, old)))
    assert action == "updated"
    payload = _patch_payload(mock_client)
    assert payload["litellm_params"]["api_base"] == "http://192.168.0.31:8080/v1"
    assert payload["litellm_params"]["api_key"] == "local-token"


async def test_a_spec_without_write_only_params_needs_no_digest(engine, mock_client):
    target = _target(engine, "openrouter-auto")
    assert engine.write_only_digest(target["litellm_params"]) is None

    action, _ = await engine.upsert_model(target, _grouped(_listed(engine, target)))
    assert action == "unchanged"

    old = _with_params(target, request_timeout=60)
    action, _ = await engine.upsert_model(target, _grouped(_listed(engine, old)))
    assert action == "updated"
    assert WRITE_ONLY_DIGEST_KEY not in _patch_payload(mock_client)["model_info"]


async def test_create_records_the_digest_only_when_needed(engine, mock_client):
    keyed = _target(engine, "agy-opus")
    await engine.upsert_model(keyed, {})
    sent = mock_client.post.call_args.kwargs["json"]
    assert sent["model_info"][WRITE_ONLY_DIGEST_KEY] == engine.write_only_digest(keyed["litellm_params"])
    assert sent["litellm_params"]["api_key"] == "dummy"
    assert WRITE_ONLY_DIGEST_KEY not in keyed["model_info"]  # the spec itself is not mutated

    keyless = _target(engine, "openrouter-auto")
    await engine.upsert_model(keyless, {})
    assert WRITE_ONLY_DIGEST_KEY not in mock_client.post.call_args.kwargs["json"]["model_info"]


def test_the_digest_is_keyed_and_reveals_nothing(engine):
    params = {"model": "openai/x", "api_base": "http://a/v1", "api_key": "local-token"}
    digest = engine.write_only_digest(params)
    assert digest is not None and digest.startswith("hmac-sha256:")
    assert "local-token" not in digest
    other_master = ModelRegistrySync(litellm_url="http://x", master_key="sk-other")
    assert other_master.write_only_digest(params) != digest
    # Visible params are compared as listed; they do not feed the digest.
    assert engine.write_only_digest({**params, "api_base": "http://b/v1"}) == digest
    assert engine.write_only_digest({**params, "api_key": "other-token"}) != digest


@pytest.mark.parametrize(
    ("name", "value", "write_only"),
    [
        ("api_key", "local-token", True),
        ("client_secret", "s", True),
        ("aws_secret_access_key", "s", True),
        ("vertex_credentials", {"type": "service_account"}, True),
        ("extra_headers", {"Authorization": "Bearer x"}, True),
        ("extra_body", [{"x-api-key": "k"}], True),
        ("extra_body", {"chat_template_kwargs": {"enable_thinking": False}}, False),
        ("extra_body", [{"seed": 1}], False),
        ("input_cost_per_token", 1.4e-07, False),
        ("max_tokens", 4096, False),
        ("request_timeout", 600, False),
        ("api_base", "http://127.0.0.1:8083/v1", False),
        ("model", "openai/locallama-qwen", False),
    ],
)
def test_write_only_params_mirror_litellm_masking(name, value, write_only):
    assert is_write_only_param(name, value) is write_only


def test_ollama_prices_are_litellm_params(engine):
    """LiteLLM drops pricing from model_info on /model/new and PATCH: a model_info price was never stored."""
    for target in engine.build_ollama_models():
        assert "input_cost_per_token" in target["litellm_params"]
        assert "output_cost_per_token" in target["litellm_params"]
        assert not {"input_cost_per_token", "output_cost_per_token"} & set(target["model_info"])
