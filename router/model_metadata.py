"""Conservative OpenRouter metadata refresh for stock LiteLLM DB deployments.

Only the exact ``openrouter/`` prefix is removed. Aliases, routing, credentials,
access controls and non-owned metadata are never echoed back. Catalog I/O is
cached independently of the deployment scan and is never on the completion path.

Qualified against stock LiteLLM 1.104.2: PATCH merges partial parameters, nested
model_info.metadata is replaceable, arbitrary fields cannot be deleted, and no
CAS exists. The instance lock excludes our own writers, NOT concurrent Admin-UI
saves. Read-back detects but cannot prevent that residual race. Provenance and
changes are sent coherently in one PATCH; only verified read-back is reported as
success. Unverified writes are explicitly failures, not transactional rollbacks.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import time
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)
CATALOG_URL = "https://openrouter.ai/api/v1/models"
CATALOG_TTL = 3600.0
CATALOG_MAX_BYTES = 16 * 1024 * 1024
RETRY_BASE = 30.0
RETRY_MAX = 3600.0
PROVENANCE_KEY = "llm_routing.openrouter_metadata"
PRICES = {
    "prompt": "input_cost_per_token",
    "completion": "output_cost_per_token",
    "input_cache_read": "cache_read_input_token_cost",
    "input_cache_write": "cache_creation_input_token_cost",
}
SCOPES = ("model_info", "litellm_params")


def _decimal(value: Any) -> Decimal | None:
    """Validate provider numbers without treating bool, unknown or infinity as zero."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0:
        return None
    return number


def _integer(value: Any) -> int | None:
    number = _decimal(value)
    if number is None or number != number.to_integral_value() or number > 2**53:
        return None
    return int(number)


def _rate(value: Any) -> float | None:
    number = _decimal(value)
    if number is None:
        return None
    rate = float(number)
    if not math.isfinite(rate) or (rate == 0 and number != 0):
        return None
    return rate


def _strings(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return sorted(set(value))


def normalize_openrouter_model(model: dict[str, Any]) -> dict[str, Any]:
    """Pure projection, with unknown evidence explicitly listed in ``unresolved``.

    context_length is a combined-context ceiling, exposed as context_window and
    max_input_tokens (not an independently available input budget). LiteLLM's
    max_tokens convention is OUTPUT, so it is only populated from the output cap.
    No blind subtraction of maximum output is performed.

    OpenRouter min_prompt_tokens means STRICTLY GREATER, matching stock 1.104.2
    OpenRouter billing. Stock tiered_pricing uses start < prompt <= end, with
    last-tier fallback above the final end. Integer thresholds are never rounded.
    Each complete tier folds applicable overrides in catalog order (later wins).
    Optional unknown cache rates are omitted: stock falls back to the input rate,
    not free caching. Unknown input/output or conditions cannot safely form a table.
    """
    info: dict[str, Any] = {}
    prices: dict[str, Any] = {}
    unresolved: list[str] = []
    context = _integer(model.get("context_length"))
    if context is None or context == 0:
        unresolved.append("context_length")
    else:
        info.update(context_window=context, max_input_tokens=context)
    top = model.get("top_provider")
    output = _integer(top.get("max_completion_tokens")) if isinstance(top, dict) else None
    if output is None or output == 0:
        unresolved.append("max_completion_tokens")
    else:
        info.update(max_output_tokens=output, max_tokens=output)
    architecture = model.get("architecture")
    architecture = architecture if isinstance(architecture, dict) else {}
    inputs = _strings(architecture.get("input_modalities"))
    outputs = _strings(architecture.get("output_modalities"))
    if outputs is not None and "text" in outputs:
        info["mode"] = "chat"
    else:
        unresolved.append("mode")
    parameters = _strings(model.get("supported_parameters"))
    if inputs is None:
        unresolved.append("input_modalities")
    else:
        info.update(
            supported_modalities=inputs, supports_vision="image" in inputs, supports_audio_input="audio" in inputs
        )
    if outputs is None:
        unresolved.append("output_modalities")
    else:
        info.update(output_modalities=outputs, supports_audio_output="audio" in outputs)
    if parameters is None:
        unresolved.append("supported_parameters")
    else:
        info.update(
            supported_parameters=parameters,
            supports_function_calling=bool({"tools", "tool_choice"}.intersection(parameters)),
            supports_parallel_function_calling="parallel_tool_calls" in parameters,
            supports_reasoning=bool({"reasoning", "reasoning_effort"}.intersection(parameters)),
        )
    pricing = model.get("pricing")
    pricing = pricing if isinstance(pricing, dict) else {}
    for source, field in PRICES.items():
        rate = _rate(pricing.get(source))
        if rate is None:
            # Cache prices are optional; their absence is not a claim of free caching.
            if source in pricing or source in ("prompt", "completion"):
                unresolved.append(source)
        else:
            prices[field] = rate
    overrides = pricing.get("overrides", [])
    valid: list[tuple[int, dict[str, float]]] = []
    tier_error = not isinstance(overrides, list)
    if isinstance(overrides, list):
        for override in overrides:
            if not isinstance(override, dict):
                tier_error = True
                continue
            threshold = _integer(override.get("min_prompt_tokens"))
            if threshold is None or threshold == 2**53 or set(override) - ({"min_prompt_tokens"} | set(PRICES)):
                tier_error = True
                continue
            changes = {PRICES[key]: _rate(value) for key, value in override.items() if key in PRICES}
            if not changes or any(value is None for value in changes.values()):
                tier_error = True
                continue
            valid.append((threshold, changes))  # type: ignore[arg-type]
    table: list[dict[str, Any]] = []
    if valid:
        starts = sorted({0, *(threshold for threshold, _ in valid)})
        for start, end in zip(starts, [*starts[1:], 2**53], strict=True):
            tier = dict(prices)
            for minimum, tier_changes in valid:
                if minimum <= start:
                    tier.update(tier_changes)
            if not {"input_cost_per_token", "output_cost_per_token"} <= tier.keys():
                tier_error = True
            table.append({"range": [start, end], **tier})
    if tier_error:
        unresolved.append("pricing_overrides")
    else:
        # [] is qualified as a real replacement on PATCH, unlike null deletion.
        prices["tiered_pricing"] = table
    return {"model_info": info, "litellm_params": prices, "unresolved": sorted(unresolved)}


def _equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(_equal(a, b) for a, b in zip(left, right, strict=True))
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            isinstance(left, dict)
            and isinstance(right, dict)
            and left.keys() == right.keys()
            and all(_equal(value, right[key]) for key, value in left.items())
        )
    return bool(left == right)


class OpenRouterMetadataSync:
    """One long-lived instance must serve startup, timer and manual reconciliation."""

    def __init__(self, litellm_url: str, master_key: str, client: httpx.AsyncClient | None = None) -> None:
        self.litellm_url = litellm_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {master_key}"}
        self._client = client
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        self._catalog: dict[str, dict[str, Any]] | None = None
        self._catalog_at = 0.0
        self._catalog_failures = 0
        self._retry_at = 0.0
        self._status: dict[str, Any] = {"status": "not_run"}

    @property
    def status(self) -> dict[str, Any]:
        return copy.deepcopy(self._status)

    def get_status(self) -> dict[str, Any]:
        return self.status

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0, follow_redirects=False)
        return self._client

    async def _models(self, ident: str | None = None) -> list[dict[str, Any]]:
        client = await self._http()
        response = await client.get(
            f"{self.litellm_url}/model/info",
            headers=self._headers,
            params={"litellm_model_id": ident} if ident is not None else {},
            timeout=15.0,
        )
        response.raise_for_status()
        rows = response.json()["data"]
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("Invalid deployment list")
        return rows

    async def _exact(self, ident: str) -> dict[str, Any]:
        rows = await self._models(ident)
        matched = [row for row in rows if (row.get("model_info") or {}).get("id") == ident]
        if len(matched) != 1:
            raise ValueError("Deployment no longer uniquely available")
        return matched[0]

    async def _get_catalog(self, force: bool) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        # Manual force refresh invalidates TTL, never a provider retry deadline.
        if now < self._retry_at:
            raise ValueError("Catalog retry deferred")
        if (
            not force
            and not self._catalog_failures
            and self._catalog is not None
            and now - self._catalog_at < CATALOG_TTL
        ):
            return self._catalog
        client = await self._http()
        # Raw Request bypasses ALL shared defaults: headers, cookies, query and
        # auth. Keep the injected transport, strict TLS and no redirects.
        request = httpx.Request("GET", CATALOG_URL, extensions={"timeout": httpx.Timeout(15.0).as_dict()})
        try:
            response = await client.send(request, auth=None, follow_redirects=False, stream=True)
            try:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > CATALOG_MAX_BYTES:
                        raise ValueError("Catalog exceeds size limit")
                    body.extend(chunk)
                rows = json.loads(body)["data"]
                if not isinstance(rows, list) or not rows:
                    raise ValueError("Invalid catalog")
                catalog: dict[str, dict[str, Any]] = {}
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                        raise ValueError("Invalid catalog row")
                    if row["id"] in catalog:
                        raise ValueError("Ambiguous catalog identity")
                    catalog[row["id"]] = row
            finally:
                await response.aclose()
        except Exception as exc:
            self._catalog_failures = min(self._catalog_failures + 1, 8)
            delay = min(RETRY_MAX, RETRY_BASE * 2 ** (self._catalog_failures - 1))
            if isinstance(exc, httpx.HTTPStatusError):
                delay = max(delay, self._retry_after(exc.response.headers.get("Retry-After")))
            self._retry_at = time.monotonic() + delay
            raise
        self._catalog, self._catalog_at = catalog, time.monotonic()
        self._catalog_failures, self._retry_at = 0, 0.0
        return catalog

    @staticmethod
    def _retry_after(value: str | None) -> float:
        if value is None:
            return 0.0
        try:
            seconds = float(max(0, min(int(RETRY_MAX), int(value))))
        except ValueError:
            try:
                seconds = parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                return 0.0
        return max(0.0, min(RETRY_MAX, seconds))

    @staticmethod
    def _target(row: dict[str, Any]) -> str | None:
        params = row.get("litellm_params")
        target = params.get("model") if isinstance(params, dict) else None
        if not isinstance(target, str) or not target.startswith("openrouter/"):
            return None
        return target[len("openrouter/") :]

    @staticmethod
    def _plan(
        row: dict[str, Any], target: str | None, normalized: dict[str, Any] | None, reason: str
    ) -> dict[str, Any]:
        info = row["model_info"]
        metadata = info.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("Cannot preserve malformed metadata")
        metadata = copy.deepcopy(metadata or {})
        old = metadata.get(PROVENANCE_KEY, {})
        if not isinstance(old, dict) or (
            PROVENANCE_KEY in metadata and (old.get("version") != 1 or old.get("deployment_id") != info["id"])
        ):
            raise ValueError("Unsupported provenance")
        previous = old.get("last_written", {})
        if not isinstance(previous, dict) or not all(isinstance(previous.get(scope, {}), dict) for scope in SCOPES):
            raise ValueError("Malformed ownership")
        if any("_above_" in field for scope in SCOPES for field in previous.get(scope, {})):
            # Literal-tier provenance was never deployed/qualified. Do not migrate it.
            raise ValueError("Unsupported literal-tier ownership")
        allowed = {
            "model_info": {
                "context_window",
                "max_input_tokens",
                "max_output_tokens",
                "max_tokens",
                "mode",
                "supported_modalities",
                "output_modalities",
                "supported_parameters",
                "supports_vision",
                "supports_audio_input",
                "supports_audio_output",
                "supports_function_calling",
                "supports_parallel_function_calling",
                "supports_reasoning",
                *PRICES.values(),
            },
            "litellm_params": {*PRICES.values(), "tiered_pricing"},
        }
        if any(set(previous.get(scope, {})) - allowed[scope] for scope in SCOPES):
            raise ValueError("Unsupported owned fields")
        sources = old.get("field_sources", {})
        if not isinstance(sources, dict):
            raise ValueError("Malformed source identities")
        sources = copy.deepcopy(sources)
        written: dict[str, dict[str, Any]] = {scope: {} for scope in SCOPES}
        changes: dict[str, dict[str, Any]] = {scope: {} for scope in SCOPES}
        unresolved = list(normalized["unresolved"]) if normalized is not None else [reason]
        if reason == "catalog_unavailable" and normalized is not None:
            unresolved.append(reason)
        overrides: list[str] = []
        for scope in SCOPES:
            current = row.get(scope) or {}
            desired = dict(normalized[scope]) if normalized is not None else {}
            owned = previous.get(scope, {})
            if scope == "litellm_params" and desired.get("tiered_pricing"):
                # A runtime table supersedes flat prices. Never silently bypass a
                # manual flat override, including a base price changed after adoption.
                conflicts = [
                    f"{price_scope}.{field}"
                    for price_scope in ("litellm_params",)
                    for field, value in (row.get(price_scope) or {}).items()
                    if (field in PRICES.values() or any(field.startswith(f"{base}_above_") for base in PRICES.values()))
                    and value is not None
                    and (field not in previous.get(price_scope, {}) or not _equal(value, previous[price_scope][field]))
                ]
                if conflicts:
                    overrides.extend(conflicts)
                    unresolved.append("pricing_overrides")
                    desired.pop("tiered_pricing")
                    # Disable our unchanged table so it cannot mask the new manual price.
                    if "tiered_pricing" in owned:
                        desired["tiered_pricing"] = []
            if (
                scope == "litellm_params"
                and "tiered_pricing" in owned
                and "tiered_pricing" not in desired
                and old.get("source_id") != target
            ):
                desired["tiered_pricing"] = []
                unresolved.append("litellm_params.tiered_pricing")
            for field in sorted(set(owned) | set(desired)):
                path = f"{scope}.{field}"
                value = current.get(field)
                if field in owned and not _equal(value, owned[field]):
                    overrides.append(path)
                    sources.pop(path, None)
                    continue
                if field not in owned and value is not None:
                    overrides.append(path)
                    continue
                if field in desired:
                    wanted = desired[field]
                    written[scope][field] = wanted
                    if field == "tiered_pricing" and (normalized is None or field not in normalized[scope]):
                        sources.setdefault(path, old.get("source_id", target))
                    else:
                        sources[path] = target
                    if not _equal(value, wanted):
                        changes[scope][field] = wanted
                else:
                    # Only base/cache custom prices support null clearing. Limits,
                    # capabilities cannot be removed in stock; tier tables use [].
                    # Keep unavailable same-target evidence during transient I/O.
                    if field in PRICES.values() and (normalized is not None or old.get("source_id") != target):
                        written[scope][field] = None
                        sources.setdefault(path, old.get("source_id", target))
                        if value is not None:
                            changes[scope][field] = None
                        unresolved.append(path)
                        continue
                    written[scope][field] = owned[field]
                    sources.setdefault(path, old.get("source_id", target))
                    unresolved.append(path)
        state = {
            "version": 1,
            "deployment_id": info["id"],
            "source_id": target,
            "last_written": written,
            "field_sources": sources,
            "status": (
                "stale"
                if reason in ("provider_changed", "catalog_unavailable")
                else "unresolved"
                if normalized is None
                else "stale"
                if unresolved
                else "current"
            ),
            "unresolved": sorted(set(unresolved)),
            "overrides": sorted(set(overrides)),
        }
        metadata[PROVENANCE_KEY] = state
        payload: dict[str, Any] = {"model_info": {"id": info["id"], **changes["model_info"]}}
        if old != state:
            payload["model_info"]["metadata"] = metadata
        if changes["litellm_params"]:
            payload["litellm_params"] = changes["litellm_params"]
        dirty = len(payload["model_info"]) > 1 or "litellm_params" in payload
        # Sanitized diff: only derived fields and namespaced state, no unrelated metadata.
        return {"payload": payload, "dirty": dirty, "changes": changes, "provenance": state}

    async def reconcile(self, dry_run: bool = False, force_catalog_refresh: bool = False) -> dict[str, Any]:
        """Best-effort refresh; failures warn without interfering with chat routing."""
        async with self._lock:
            counts = dict.fromkeys(
                ("scanned", "skipped", "updated", "would_update", "unchanged", "unresolved", "failed"), 0
            )
            report: dict[str, Any] = {"dry_run": dry_run, "counts": counts, "deployments": [], "errors": []}
            try:
                rows = await self._models()
            except Exception as exc:
                report["errors"].append(f"deployment_scan:{type(exc).__name__}")
                logger.warning("OpenRouter metadata deployment scan failed (%s)", type(exc).__name__)
                self._status = copy.deepcopy(report)
                return report
            try:
                catalog = await self._get_catalog(force_catalog_refresh)
                reason = "missing_catalog_model"
            except Exception as exc:
                catalog = self._catalog or {}
                reason = "catalog_unavailable"
                report["errors"].append(f"catalog:{type(exc).__name__}")
                logger.warning("OpenRouter metadata catalog unavailable (%s)", type(exc).__name__)
            seen: set[str] = set()
            for row in rows:
                counts["scanned"] += 1
                info = row.get("model_info")
                info = info if isinstance(info, dict) else {}
                target = self._target(row)
                ident = info.get("id")
                metadata = info.get("metadata")
                has_provenance = isinstance(metadata, dict) and PROVENANCE_KEY in metadata
                if (
                    info.get("db_model") is not True
                    or (target is None and not has_provenance)
                    or not isinstance(ident, str)
                    or ident in seen
                ):
                    counts["skipped"] += 1
                    continue
                seen.add(ident)
                result: dict[str, Any] = {"id": ident, "source_id": target}
                report["deployments"].append(result)
                try:
                    fresh = await self._exact(ident)
                    routing_target = (row.get("litellm_params") or {}).get("model")
                    if (fresh.get("litellm_params") or {}).get("model") != routing_target or fresh["model_info"].get(
                        "db_model"
                    ) is not True:
                        raise ValueError("Deployment target changed during scan")
                    normalized = normalize_openrouter_model(catalog[target]) if target in catalog else None
                    plan = self._plan(fresh, target, normalized, "provider_changed" if target is None else reason)
                    result.update(changes=plan["changes"], provenance=plan["provenance"])
                    if plan["provenance"]["status"] != "current":
                        counts["unresolved"] += 1
                    if not plan["dirty"]:
                        result["status"] = "unchanged"
                    elif dry_run:
                        result["status"] = "would_update"
                    else:
                        client = await self._http()
                        response = await client.patch(
                            f"{self.litellm_url}/model/{quote(ident, safe='')}/update",
                            headers=self._headers,
                            json=plan["payload"],
                            timeout=15.0,
                        )
                        response.raise_for_status()
                        verified = await self._exact(ident)
                        if (verified.get("litellm_params") or {}).get("model") != routing_target:
                            raise ValueError("Target changed during write")
                        for scope, fields in plan["payload"].items():
                            if any(
                                not _equal((verified.get(scope) or {}).get(key), value) for key, value in fields.items()
                            ):
                                raise ValueError("Metadata write did not verify")
                        result["status"] = "updated"
                    counts[result["status"]] += 1
                except Exception as exc:
                    counts["failed"] += 1
                    result.update(status="failed", error=type(exc).__name__)
                    logger.warning("OpenRouter metadata deployment reconciliation failed (%s)", type(exc).__name__)
            self._status = copy.deepcopy(report)
            return report
