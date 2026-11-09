"""TASK-353: a timed-out vision request is never retried, on Strata or on the local fallback.

DIVORCE-IA's interactive callers send a per-request ``timeout`` of 100 s inside a 210 s client wait
(DIVORCE-IA #820). Up to v0.1.97 only ``strata-qwen-vl`` had a retry policy: its fallback
``locallama-qwen-vl`` (and ``local-qwen-vl``) used the router-wide ``num_retries: 1``, so a request
that timed out on both made three attempts (300 s), and llama-router kept reading after the client left.

How LiteLLM v1.108.0 resolves this (``Router.async_function_with_retries`` and
``router_utils/get_retry_from_policy.py``):
- each fallback hop runs its own retry loop, keyed by the hop's model group (``kwargs["model"]``),
  so a policy on ``strata-qwen-vl`` does not cover ``locallama-qwen-vl``;
- a group policy wins over the request's ``num_retries`` (except ``num_retries: 0``) and over the
  deployment's ``num_retries``;
- the field is picked by the exception's MRO, most specific first: ``litellm.Timeout`` reads
  ``TimeoutErrorRetries``; a field left unset falls through to ``DefaultRetries``, so the timeout
  field must be set to 0 explicitly;
- a connection reset or refusal reaches the policy as ``InternalServerError("Connection error.")``;
  4xx codes LiteLLM leaves unmapped for openai-compatible providers (403, 413) are a plain ``APIError``
  and read ``DefaultRetries``.
"""

from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parents[1] / "litellm/config.yaml"
VISION_GROUPS = ("strata-qwen-vl", "locallama-qwen-vl", "local-qwen-vl")
FOUR_XX = ("BadRequestErrorRetries", "AuthenticationErrorRetries", "ContentPolicyViolationErrorRetries")
# DIVORCE-IA interactive vision callers (DIVORCE-IA #820): per-request timeout inside the client wait.
INTERACTIVE_TIMEOUT_S = 100
INTERACTIVE_CLIENT_WAIT_S = 210


def _config() -> dict:
    return yaml.safe_load(CONFIG.read_text())


def _fallback_chain(config: dict, start: str) -> list[str]:
    """Model groups a request to ``start`` can reach, in the order LiteLLM tries them."""
    fallbacks = {name: targets for entry in config["litellm_settings"]["fallbacks"] for name, targets in entry.items()}
    chain, todo = [], [start]
    while todo:
        group = todo.pop(0)
        if group not in chain:
            chain.append(group)
            todo.extend(fallbacks.get(group, []))
    return chain


def _timeout_retries(config: dict, group: str) -> int:
    """Retries LiteLLM grants a ``litellm.Timeout`` in ``group``."""
    router = config["router_settings"]
    policy = (router.get("model_group_retry_policy") or {}).get(group) or router.get("retry_policy")
    for field in ("TimeoutErrorRetries", "DefaultRetries"):
        if policy and policy.get(field) is not None:
            return policy[field]
    return router["num_retries"]


def _timed_out_attempts(config: dict, group: str) -> int:
    """Attempts one request makes when every attempt hits its per-request timeout."""
    return sum(1 + _timeout_retries(config, hop) for hop in _fallback_chain(config, group))


def test_every_vision_group_never_retries_a_timeout():
    policies = _config()["router_settings"]["model_group_retry_policy"]
    for group in VISION_GROUPS:
        assert policies[group]["TimeoutErrorRetries"] == 0, group  # explicit: unset would mean DefaultRetries


def test_transient_errors_are_still_retried_once_and_mapped_4xx_never():
    policies = _config()["router_settings"]["model_group_retry_policy"]
    for group in VISION_GROUPS:
        policy = policies[group]
        assert policy["InternalServerErrorRetries"] == 1, group  # 5xx, connection reset or refused
        assert policy["ServiceUnavailableErrorRetries"] == 1, group  # 503 (llama-router busy or loading)
        assert policy["DefaultRetries"] == 1, group  # errors with no field, e.g. an unmapped 403 or 413
        assert policy["NotFoundErrorRetries"] == 0, group
        for field in FOUR_XX:
            assert policy[field] == 0, (group, field)


def test_the_vision_route_has_one_policy():
    policies = _config()["router_settings"]["model_group_retry_policy"]
    assert policies["locallama-qwen-vl"] == policies["strata-qwen-vl"]
    assert policies["local-qwen-vl"] == policies["strata-qwen-vl"]


def test_worst_case_is_two_timeouts_on_the_route_and_one_on_the_local_profile():
    config = _config()
    assert _fallback_chain(config, "strata-qwen-vl") == ["strata-qwen-vl", "locallama-qwen-vl"]
    assert _timed_out_attempts(config, "strata-qwen-vl") == 2  # GX10, then the local fallback once
    assert _timed_out_attempts(config, "locallama-qwen-vl") == 1
    assert _timed_out_attempts(config, "local-qwen-vl") == 1  # reader B of the two-reader backfill
    assert _timed_out_attempts(config, "strata-qwen-vl") * INTERACTIVE_TIMEOUT_S <= INTERACTIVE_CLIENT_WAIT_S


def test_scope_is_the_vision_route_only():
    """The router-wide retry settings are unchanged: other groups keep retrying a timeout once."""
    config = _config()
    router = config["router_settings"]
    assert router["num_retries"] == 1
    assert "retry_policy" not in router  # a global policy would also cover every non-vision group
    assert _timeout_retries(config, "strata-qwen") == 1  # owner decision 2026-10-23 (manual section 9g)
    assert _timeout_retries(config, "locallama-qwen") == 1
    assert set(router["model_group_retry_policy"]) == {"strata-qwen", *VISION_GROUPS}
