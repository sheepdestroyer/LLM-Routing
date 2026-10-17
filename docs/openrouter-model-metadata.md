# OpenRouter model metadata refresh

LLM-Routing enriches OpenRouter-backed LiteLLM database deployments from the public OpenRouter catalog. It uses the supported management API of stock LiteLLM; no private LiteLLM fork is required.

## Stable names versus underlying models

Clients keep calling the deployment's public `model_name`, such as `litellm-divorce-ia`. An administrator can change `litellm_params.model` to another underlying OpenRouter model while keeping the alias and deployment ID. Metadata refresh never changes that target, alias, credential, reasoning setting, API base, access group, or fallback policy.

Identity matching is exact: `openrouter/openai/gpt-6.1-sol` maps to catalog ID `openai/gpt-6.1-sol`. Nested IDs and suffixes are retained; no nearest-version substitution is attempted.

The initial scope is OpenRouter database deployments. YAML-only deployments are not edited through the API. Other providers are not automatically enriched. An owned deployment leaving OpenRouter must be reported as outside scope/stale rather than retain a misleading current-source status.

## Refresh lifecycle

- Startup reconciliation runs after model registrations finish.
- A background worker scans deployments every 60 seconds, reusing a catalog cached for up to one hour.
- Manual reconciliation can force a catalog refresh, subject to provider retry safety.
- Completion and Responses requests do not fetch the catalog.
- Refresh is eventual; changing the target in LiteLLM's Admin UI is not an atomic metadata-update transaction.

One long-lived reconciler instance shares its catalog and lock across startup, timer and manual operations. Endpoint or credential configuration changes require restarting the router.

## Ownership and manual overrides

Versioned provenance is stored in:

`model_info.metadata["llm_routing.openrouter_metadata"]`

It records the deployment ID, source identity, last-written fields, per-field sources, overrides and unresolved/stale state. Unrelated metadata is preserved.

The reconciler fills missing values and updates fields it previously wrote only while their current values still match its last-written values. A differing operator value becomes a preserved override. Explicit `0` prices and `false` capabilities are valid overrides, not missing values.

Existing non-null values with no reconciler provenance are preserved. This includes legacy hardcoded metadata and fields supplied by LiteLLM's enriched `/model/info` projection. The API does not expose enough information to safely infer whether such values were deliberate operator overrides. They require deliberate review/adoption; this feature does not silently take ownership of them.

For example, an alias whose token limits and prices are null can be enriched automatically. An alias with manually specified prices retains those prices even after its underlying model changes.

## Limits and capabilities

Catalog input modalities and supported parameters supply capability evidence. Missing evidence remains unresolved rather than inventing support.

OpenRouter `context_length` is a combined context ceiling. `max_input_tokens` reflects that ceiling, not an independently guaranteed input budget when output is also requested. The advertised completion cap supplies `max_output_tokens` and LiteLLM's output-oriented `max_tokens`. The normalizer does not blindly subtract maximum output from combined context.

Mode must be supported by catalog evidence; an unsupported modality must not be reported as completely enriched.

## Pricing and actual billing

Base prompt, completion and cache prices are validated as finite, nonnegative per-token rates. Optional unknown prices are not replaced with zero.

OpenRouter threshold overrides are translated into stock LiteLLM's `litellm_params.tiered_pricing` range tables. A field appearing in API JSON is not proof it affects billing: arbitrary flat threshold fields in `litellm_params` are not registered by LiteLLM 1.104.2, while pricing fields sent only in `model_info` are stripped by the management API. The supported range-table representation has been qualified through actual PATCH, raw database persistence and the stock cost calculator, including an arbitrary 272001-token threshold and cached-token/output accounting.

One range is selected by total prompt tokens and its rates apply to the request, rather than charging graduated slices. Matching is `range_start < prompt_tokens <= range_end`, preserving OpenRouter's strict-greater threshold semantics without rounding to thousands. Effective override rates are folded in catalog order. Tables are replaced as a whole: a known target without overrides uses `tiered_pricing: []`, removing the previous automatically owned table without deleting individual flat keys or setting paid rates to zero. Deliberate table overrides remain preserved.

Unsupported pricing conditions, such as unqualified time-window rules, are reported rather than approximated.

## Failure and concurrency boundaries

- Invalid, empty, oversized or ambiguous catalog snapshots must not replace a usable cache.
- Provider outages/throttling use bounded retry behavior and stale/error reporting.
- Unknown new targets retain explicitly marked unresolved/stale owned fields; old model values are not claimed to describe the new model.
- Supported PATCH updates preserve unrelated fields. Explicit null deletion is restricted to certain fields in stock LiteLLM 1.104.2; most stored limits/capabilities and arbitrary tier keys cannot be reset with null.
- The lock serializes this reconciler's writers. Stock LiteLLM lacks conditional model updates, so concurrent Admin-UI saves remain a residual lost-update race. Read-back can detect mismatches but cannot prevent every race.
- Success is reported only after reading back the exact deployment and verifying its target and written values. A failed verification is not a transactional rollback.

Public catalog requests must not inherit proxy authorization, API keys or cookies. Diagnostic reports contain only sanitized derived-field differences and provenance, not credentials or unrelated metadata.

## Dashboard button

After editing a model in LiteLLM, open the LLM-Routing dashboard and use **Refresh model metadata**. Enter an administrator key in the masked transient field. The key is cleared on submission and is not stored in browser storage, cookies or the URL. LAN access to the dashboard does not itself grant administrator rights.

The button invokes a forced, metadata-only refresh; it never initiates managed registry creation/deletion. It is disabled while the request runs and displays updated/unchanged, preserved-override, unresolved and failed results. Authentication errors and partial failures are shown explicitly. Provider retry deadlines still apply to a forced refresh.

## Authenticated administration

These routes belong to the existing router service; no new listener or port is introduced. They require an explicitly configured `ROUTER_API_KEY` or `LITELLM_MASTER_KEY`; ordinary client virtual keys are insufficient.

Read last reconciliation status without fetching the provider catalog:

```sh
curl --fail-with-body \
  -H "Authorization: Bearer $ROUTER_API_KEY" \
  https://llm-routing.dev.vendeuvre.lan/admin/model-metadata/status
```

Preview metadata reconciliation without deployment writes:

```sh
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $ROUTER_API_KEY" \
  'https://llm-routing.dev.vendeuvre.lan/admin/sync-models?metadata_only=true&dry_run=true&force_catalog_refresh=true'
```

Apply only metadata reconciliation:

```sh
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $ROUTER_API_KEY" \
  'https://llm-routing.dev.vendeuvre.lan/admin/sync-models?metadata_only=true'
```

The legacy `results` object remains in the sync response; the `metadata` report is additive. Any `dry_run=true` request bypasses registry creation, deletion and updates. With no flags, the endpoint retains managed registry synchronization and additionally reconciles metadata. Honor local TLS trust rather than adding `-k`.

These commands describe the feature contract; availability on a deployed endpoint depends on installing a release containing it.

## Validation and rollout

Unit and lifecycle tests cover ownership, overrides, exact identities, malformed catalogs, retry behavior, authorization, cancellation and idempotency. Runtime qualification uses disposable loopback-only LiteLLM/PostgreSQL containers, synthetic catalog/usage fixtures, and the real stock API/database/cost calculator. Synthetic usage cost tests do not prove real upstream generation, Langfuse tracing or provider invoice parity.

Run the full existing lint, formatting, type-check and 100% statement/branch coverage gates before push. Require independent review and passing CI before merge. Deploy only a qualified published release; do not hotpatch production containers or change production model records during development.
