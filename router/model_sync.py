"""Model Registry Synchronization Engine for LiteLLM DB Models.

Unifies model naming under <provider>-<model> convention:
  - locallama-* (host llama-server, whisper-server, classifier)
  - agy-* (host agy daemon, auto-discovering latest Gemini & vendor models)
  - ollama-* (Ollama cloud/local models)
  - openrouter-* (OpenRouter models & TTS)
  - legacy aliases (ensuring zero disruption for external clients)

Idempotent: inspects existing DB models, prunes duplicate deployments,
updates changed configurations, and registers new models.
Fails closed: if /model/info cannot be fetched, never blindly creates duplicates.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger("model_sync")

DEPRECATED_MODEL_NAMES = [
    "ollama/GPT-5.6 Luna (max)",
    "ollama/gpt-5.6-luna",
    "gpt-5.6-luna",
    "local-qwen",
    "local-qwen-hass",
    "local-qwen-routing",
    "whisper-1",
    "gpt-4o-mini-transcribe",
    "gpt-4o-mini-tts",
    "tts-1",
    "llm-routing-agy",
    "llm-routing-agy-sse",
    "agy-sse",
    "ollama-gpt-5.6-luna",
    "ollama-gpt-5.6-luna-max",
    "openrouter-tts",
]


class ModelInfoFetchError(Exception):
    """Raised when /model/info cannot be fetched reliably from LiteLLM."""


class ModelRegistrySync:
    """Manages idempotent synchronization of models in LiteLLM's PostgreSQL database."""

    def __init__(
        self,
        litellm_url: str,
        master_key: str,
        agy_daemon_url: str = "http://127.0.0.1:5005",
        llama_server_url: str = "http://127.0.0.1:8083",
        whisper_server_url: str = "http://127.0.0.1:8084",
        classifier_url: str = "http://127.0.0.1:8086",
        ollama_api_base: str = "https://api.ollama.com",
        openrouter_api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.litellm_url = litellm_url.rstrip("/")
        self.master_key = master_key
        self.agy_daemon_url = agy_daemon_url.rstrip("/")
        self.llama_server_url = llama_server_url.rstrip("/")
        self.whisper_server_url = whisper_server_url.rstrip("/")
        self.classifier_url = classifier_url.rstrip("/")
        self.ollama_api_base = ollama_api_base.rstrip("/")
        self.openrouter_api_key = openrouter_api_key or os.getenv("OPENROUTER_API_KEY", "")
        self.ollama_api_key = os.getenv("OLLAMA_API_KEY", "")
        self._external_client = client
        self._owned_client: httpx.AsyncClient | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.master_key}",
            "Content-Type": "application/json",
        }

    async def __aenter__(self) -> ModelRegistrySync:
        if self._external_client is None and self._owned_client is None:
            self._owned_client = httpx.AsyncClient(timeout=15.0)
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close owned HTTP client if one was created."""
        if self._owned_client is not None:
            try:
                await self._owned_client.aclose()
            except Exception as e:
                logger.debug(f"Error closing owned client: {e}")
            finally:
                self._owned_client = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._external_client is not None:
            return self._external_client
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient(timeout=15.0)
        return self._owned_client

    async def get_existing_models(self) -> dict[str, list[dict[str, Any]]]:
        """Fetch all models from LiteLLM and group them by model_name.

        Fails closed by raising ModelInfoFetchError on network/HTTP errors.
        """
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{self.litellm_url}/model/info",
                headers=self.headers,
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.error(f"Failed to fetch /model/info: HTTP {resp.status_code}")
                raise ModelInfoFetchError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json().get("data", [])
            grouped: dict[str, list[dict[str, Any]]] = {}
            for item in data:
                name = item.get("model_name")
                if not name:
                    continue
                grouped.setdefault(name, []).append(item)
            return grouped
        except ModelInfoFetchError:
            raise
        except Exception as e:
            logger.error(f"Error fetching existing models from LiteLLM: {e}")
            raise ModelInfoFetchError(str(e)) from e

    async def prune_duplicates(
        self,
        grouped_models: dict[str, list[dict[str, Any]]],
        managed_names: set[str] | None = None,
    ) -> int:
        """Prune duplicate deployments for managed models in LiteLLM DB, keeping the latest."""
        client = await self._get_client()
        pruned = 0
        for model_name, deployments in grouped_models.items():
            if managed_names is not None and model_name not in managed_names:
                continue

            if len(deployments) <= 1:
                continue

            # Sort deployments by updated_at or created_at if present
            sorted_deps = sorted(
                deployments,
                key=lambda d: str((d.get("model_info") or {}).get("updated_at") or (d.get("model_info") or {}).get("created_at") or ""),
            )

            # Keep the last deployment, delete preceding duplicates
            to_delete = sorted_deps[:-1]
            for dep in to_delete:
                model_info = dep.get("model_info") or {}
                model_id = model_info.get("id")
                if not model_id or not model_info.get("db_model"):
                    continue
                try:
                    resp = await client.post(
                        f"{self.litellm_url}/model/delete",
                        headers=self.headers,
                        json={"id": model_id},
                        timeout=10.0,
                    )
                    if resp.status_code == 200:
                        pruned += 1
                        logger.info(f"Pruned duplicate deployment {model_id} for '{model_name}'")
                    else:
                        logger.warning(
                            f"Failed to delete duplicate {model_id} for '{model_name}': HTTP {resp.status_code}"
                        )
                except Exception as e:
                    logger.warning(f"Exception deleting duplicate {model_id} for '{model_name}': {e}")
        return pruned

    async def remove_stale_models(
        self,
        grouped_models: dict[str, list[dict[str, Any]]],
        stale_names: list[str] | None = None,
    ) -> int:
        """Remove deprecated / non-standard models from LiteLLM DB."""
        if stale_names is None:
            stale_names = DEPRECATED_MODEL_NAMES

        client = await self._get_client()
        removed = 0
        for name in stale_names:
            deployments = grouped_models.get(name, [])
            for dep in deployments:
                model_info = dep.get("model_info") or {}
                model_id = model_info.get("id")
                if not model_id or not model_info.get("db_model"):
                    continue
                try:
                    resp = await client.post(
                        f"{self.litellm_url}/model/delete",
                        headers=self.headers,
                        json={"id": model_id},
                        timeout=10.0,
                    )
                    if resp.status_code == 200:
                        removed += 1
                        logger.info(f"Removed deprecated model '{name}' (id: {model_id})")
                except Exception as e:
                    logger.warning(f"Exception deleting deprecated model '{name}': {e}")
        return removed

    async def discover_agy_latest_flash(self) -> str:
        """Query agy-daemon /models to discover the latest available Gemini Flash model."""
        client = await self._get_client()
        fallback_model = "gemini-3.8-flash"
        try:
            resp = await client.get(f"{self.agy_daemon_url}/models", timeout=5.0)
            if resp.status_code != 200:
                return fallback_model
            data = resp.json()
            models = data.get("models", [])
            flash_versions: list[tuple[tuple[int, ...], str]] = []
            for m in models:
                mid = m.get("id", "")
                match = re.search(r"gemini-(\d+(?:\.\d+)+)-flash", mid)
                if match:
                    ver_tuple = tuple(int(x) for x in match.group(1).split("."))
                    flash_versions.append((ver_tuple, f"gemini-{match.group(1)}-flash"))
            if flash_versions:
                flash_versions.sort(key=lambda x: x[0], reverse=True)
                latest = flash_versions[0][1]
                logger.info(f"Discovered latest Gemini Flash version from agy daemon: {latest}")
                return latest
        except Exception as e:
            logger.debug(f"Unable to discover agy models (using fallback {fallback_model}): {e}")
        return fallback_model

    def build_locallama_models(self) -> list[dict[str, Any]]:
        """Build model definitions for local host backends (llama-server & whisper-server)."""
        llama_base = (
            self.llama_server_url
            if self.llama_server_url.endswith("/v1")
            else f"{self.llama_server_url}/v1"
        )
        whisper_base = (
            self.whisper_server_url
            if self.whisper_server_url.endswith("/v1")
            else f"{self.whisper_server_url}/v1"
        )
        return [
            {
                "model_name": "locallama-qwen",
                "litellm_params": {
                    "model": "openai/locallama-qwen",
                    "api_base": llama_base,
                    "api_key": "local-token",
                    "request_timeout": 600,
                    "extra_body": {
                        "chat_template_kwargs": {"preserve_thinking": True}
                    },
                },
                "model_info": {
                    "mode": "chat",
                    "max_tokens": 240896,
                    "max_input_tokens": 240896,
                    "supports_vision": True,
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                    "is_public_model_group": True,
                },
            },
            {
                "model_name": "locallama-qwen-hass",
                "litellm_params": {
                    "model": "openai/locallama-qwen-hass",
                    "api_base": llama_base,
                    "api_key": "local-token",
                    "request_timeout": 600,
                    "extra_body": {
                        "chat_template_kwargs": {"enable_thinking": False}
                    },
                },
                "model_info": {
                    "mode": "chat",
                    "max_tokens": 240896,
                    "max_input_tokens": 240896,
                    "supports_vision": True,
                    "supports_reasoning": False,
                    "supports_function_calling": True,
                    "is_public_model_group": True,
                },
            },
            {
                "model_name": "locallama-qwen-routing",
                "litellm_params": {
                    "model": "openai/locallama-qwen-routing",
                    "api_base": self.classifier_url,
                    "api_key": "local-token",
                    "request_timeout": 60,
                },
                "model_info": {
                    "mode": "chat",
                    "max_tokens": 8192,
                    "max_input_tokens": 8192,
                    "supports_vision": False,
                    "supports_reasoning": False,
                    "supports_function_calling": False,
                    "is_public_model_group": True,
                },
            },
            {
                "model_name": "locallama-whisper",
                "litellm_params": {
                    "model": "openai/locallama-whisper",
                    "api_base": whisper_base,
                    "api_key": "local-token",
                    "request_timeout": 60,
                },
                "model_info": {
                    "mode": "audio_transcription",
                    "is_public_model_group": True,
                },
            },
            {
                "model_name": "locallama-nomic-embed",
                "litellm_params": {
                    "model": "openai/locallama-nomic-embed",
                    "api_base": llama_base,
                    "api_key": "local-token",
                    "request_timeout": 30,
                },
                "model_info": {
                    "mode": "embedding",
                    "is_public_model_group": True,
                },
            },
        ]

    def build_agy_models(self, latest_flash: str = "gemini-3.8-flash") -> list[dict[str, Any]]:
        """Build model definitions for agy daemon, updating Flash to latest discovered version."""
        agy_base = (
            self.agy_daemon_url
            if self.agy_daemon_url.endswith("/v1")
            else f"{self.agy_daemon_url}/v1"
        )
        return [
            {
                "model_name": "agy-gemini",
                "litellm_params": {
                    "model": f"openai/{latest_flash}",
                    "api_base": agy_base,
                    "api_key": "dummy",
                    "request_timeout": 600,
                },
                "model_info": {
                    "mode": "chat",
                    "max_tokens": 65536,
                    "max_input_tokens": 1048576,
                    "supports_vision": False,
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                    "is_public_model_group": True,
                },
            },
            {
                "model_name": "agy-gemini-sse",
                "litellm_params": {
                    "model": f"openai/{latest_flash}-sse",
                    "api_base": agy_base,
                    "api_key": "dummy",
                    "request_timeout": 600,
                },
                "model_info": {
                    "mode": "chat",
                    "max_tokens": 65536,
                    "max_input_tokens": 1048576,
                    "supports_vision": False,
                    "supports_reasoning": True,
                    "supports_function_calling": False,
                    "is_public_model_group": True,
                },
            },
            {
                "model_name": "agy-opus",
                "litellm_params": {
                    "model": "openai/claude-opus-4.6",
                    "api_base": agy_base,
                    "api_key": "dummy",
                    "request_timeout": 600,
                },
                "model_info": {
                    "mode": "chat",
                    "max_tokens": 65536,
                    "max_input_tokens": 200000,
                    "supports_vision": False,
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                    "is_public_model_group": True,
                },
            },
            {
                "model_name": "agy-opus-sse",
                "litellm_params": {
                    "model": "openai/claude-opus-4.6-sse",
                    "api_base": agy_base,
                    "api_key": "dummy",
                    "request_timeout": 600,
                },
                "model_info": {
                    "mode": "chat",
                    "max_tokens": 65536,
                    "max_input_tokens": 200000,
                    "supports_vision": False,
                    "supports_reasoning": True,
                    "supports_function_calling": False,
                    "is_public_model_group": True,
                },
            },
            {
                "model_name": "agy-sonnet",
                "litellm_params": {
                    "model": "openai/claude-sonnet-4.6",
                    "api_base": agy_base,
                    "api_key": "dummy",
                    "request_timeout": 600,
                },
                "model_info": {
                    "mode": "chat",
                    "max_tokens": 65536,
                    "max_input_tokens": 200000,
                    "supports_vision": False,
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                    "is_public_model_group": True,
                },
            },
            {
                "model_name": "agy-sonnet-sse",
                "litellm_params": {
                    "model": "openai/claude-sonnet-4.6-sse",
                    "api_base": agy_base,
                    "api_key": "dummy",
                    "request_timeout": 600,
                },
                "model_info": {
                    "mode": "chat",
                    "max_tokens": 65536,
                    "max_input_tokens": 200000,
                    "supports_vision": False,
                    "supports_reasoning": True,
                    "supports_function_calling": False,
                    "is_public_model_group": True,
                },
            },
            {
                "model_name": "agy-gptoss",
                "litellm_params": {
                    "model": "openai/gpt-oss-120b-medium",
                    "api_base": agy_base,
                    "api_key": "dummy",
                    "request_timeout": 600,
                },
                "model_info": {
                    "mode": "chat",
                    "max_tokens": 65536,
                    "max_input_tokens": 131072,
                    "supports_vision": False,
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                    "is_public_model_group": True,
                },
            },
            {
                "model_name": "agy-gptoss-sse",
                "litellm_params": {
                    "model": "openai/gpt-oss-120b-medium-sse",
                    "api_base": agy_base,
                    "api_key": "dummy",
                    "request_timeout": 600,
                },
                "model_info": {
                    "mode": "chat",
                    "max_tokens": 65536,
                    "max_input_tokens": 131072,
                    "supports_vision": False,
                    "supports_reasoning": True,
                    "supports_function_calling": False,
                    "is_public_model_group": True,
                },
            },
        ]

    def build_ollama_models(self) -> list[dict[str, Any]]:
        """Build model definitions for Ollama Cloud models."""
        return [
            {
                "model_name": "ollama-deepseek-v4-pro",
                "litellm_params": {
                    "model": "ollama_chat/deepseek-v4-pro",
                    "api_base": self.ollama_api_base,
                    "api_key": "os.environ/OLLAMA_API_KEY",
                    "request_timeout": 120,
                },
                "model_info": {
                    "mode": "chat",
                    "max_tokens": 524288,
                    "max_input_tokens": 524288,
                    "input_cost_per_token": 0.00000174,
                    "output_cost_per_token": 0.00000348,
                    "supports_vision": True,
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                    "is_public_model_group": True,
                },
            },
            {
                "model_name": "ollama-deepseek-v4-flash",
                "litellm_params": {
                    "model": "ollama_chat/deepseek-v4-flash",
                    "api_base": self.ollama_api_base,
                    "api_key": "os.environ/OLLAMA_API_KEY",
                    "request_timeout": 120,
                },
                "model_info": {
                    "mode": "chat",
                    "max_tokens": 524288,
                    "max_input_tokens": 524288,
                    "input_cost_per_token": 0.00000014,
                    "output_cost_per_token": 0.00000028,
                    "supports_vision": True,
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                    "is_public_model_group": True,
                },
            },
        ]

    def build_openrouter_models(self) -> list[dict[str, Any]]:
        """Build model definitions for OpenRouter models."""
        return [
            {
                "model_name": "openrouter-auto",
                "litellm_params": {
                    "model": "openrouter/openrouter/auto",
                    "request_timeout": 120,
                },
                "model_info": {
                    "mode": "chat",
                    "max_tokens": 2000000,
                    "max_input_tokens": 2000000,
                    "supports_vision": True,
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                    "is_public_model_group": True,
                },
            },
            {
                "model_name": "openrouter-gpt-5.6-luna",
                "litellm_params": {
                    "model": "openrouter/openai/gpt-5.6-luna",
                    "api_key": "os.environ/OPENROUTER_API_KEY",
                    "reasoning_effort": "max",
                    "request_timeout": 120,
                },
                "model_info": {
                    "mode": "chat",
                    "max_tokens": 1050000,
                    "max_input_tokens": 1050000,
                    "input_cost_per_token": 0.0000002,
                    "output_cost_per_token": 0.0000012,
                    "supports_vision": True,
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                    "is_public_model_group": True,
                },
            },
            {
                "model_name": "openrouter-gpt-5.6-luna-max",
                "litellm_params": {
                    "model": "openrouter/openai/gpt-5.6-luna",
                    "api_key": "os.environ/OPENROUTER_API_KEY",
                    "reasoning_effort": "max",
                    "request_timeout": 120,
                },
                "model_info": {
                    "mode": "chat",
                    "max_tokens": 1050000,
                    "max_input_tokens": 1050000,
                    "input_cost_per_token": 0.0000002,
                    "output_cost_per_token": 0.0000012,
                    "supports_vision": True,
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                    "is_public_model_group": True,
                },
            },
        ]

    def build_legacy_aliases(self, latest_flash: str = "gemini-3.8-flash") -> list[dict[str, Any]]:
        """Legacy aliases removed: all clients use canonical provider-model names."""
        return []

    async def upsert_model(
        self,
        payload: dict[str, Any],
        existing_grouped: dict[str, list[dict[str, Any]]],
    ) -> tuple[str, bool]:
        """Idempotently register or update a model in LiteLLM DB."""
        client = await self._get_client()
        name = payload["model_name"]
        deployments = existing_grouped.get(name, [])

        if not deployments:
            try:
                resp = await client.post(
                    f"{self.litellm_url}/model/new",
                    headers=self.headers,
                    json=payload,
                    timeout=10.0,
                )
                if resp.status_code in (200, 201):
                    logger.info(f"Registered new DB model: {name}")
                    return ("created", True)
                logger.warning(
                    f"Failed to register model '{name}': HTTP {resp.status_code} - {resp.text[:200]}"
                )
                return ("failed", False)
            except Exception as e:
                logger.warning(f"Exception registering model '{name}': {e}")
                return ("error", False)

        # Model already exists. Compare config to determine if update is necessary.
        existing_dep = deployments[-1]
        model_info = existing_dep.get("model_info") or {}
        model_id = model_info.get("id")

        if not model_id or not model_info.get("db_model"):
            # Static yaml model or missing ID - register DB override if needed
            return ("unchanged", False)

        current_params = existing_dep.get("litellm_params") or {}
        new_params = payload.get("litellm_params") or {}
        current_info = existing_dep.get("model_info") or {}
        new_info = payload.get("model_info") or {}

        # Compare all configured litellm_params
        params_drift = any(current_params.get(k) != new_params.get(k) for k in new_params)

        # Compare critical capabilities and limits in model_info
        info_keys = (
            "supports_vision",
            "supports_reasoning",
            "supports_function_calling",
            "max_tokens",
            "max_input_tokens",
            "mode",
        )
        info_drift = any(current_info.get(k) != new_info.get(k) for k in info_keys if k in new_info)

        if not params_drift and not info_drift:
            return ("unchanged", False)

        update_payload = {
            "litellm_params": new_params,
            "model_info": {
                **new_info,
                "id": model_id,
            },
        }

        try:
            resp = await client.post(
                f"{self.litellm_url}/model/update",
                headers=self.headers,
                json=update_payload,
                timeout=10.0,
            )
            if resp.status_code == 200:
                logger.info(f"Updated DB model '{name}' (id: {model_id}) with new parameters")
                return ("updated", True)
            logger.warning(
                f"Failed to update model '{name}': HTTP {resp.status_code} - {resp.text[:200]}"
            )
            return ("failed", False)
        except Exception as e:
            logger.warning(f"Exception updating model '{name}': {e}")
            return ("error", False)

    async def sync_all_models(self) -> dict[str, int]:
        """Run full synchronization pass: deduplicate, clean stale, discover, and upsert.

        Fails closed: if existing models cannot be fetched, aborts immediately.
        """
        results = {
            "pruned_duplicates": 0,
            "removed_stale": 0,
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "failed": 0,
        }

        try:
            existing = await self.get_existing_models()
        except ModelInfoFetchError as e:
            logger.error(f"Aborting model sync: unable to retrieve existing models ({e})")
            results["failed"] += 1
            return results

        # Step 1: Assemble target models to know our managed set
        latest_flash = await self.discover_agy_latest_flash()
        all_targets: list[dict[str, Any]] = []
        all_targets.extend(self.build_locallama_models())
        all_targets.extend(self.build_agy_models(latest_flash=latest_flash))
        all_targets.extend(self.build_ollama_models())
        all_targets.extend(self.build_openrouter_models())

        managed_names = {t["model_name"] for t in all_targets}

        # Step 2: Prune duplicates only for managed models
        results["pruned_duplicates"] = await self.prune_duplicates(existing, managed_names=managed_names)

        # Refresh existing list after pruning
        try:
            existing = await self.get_existing_models()
        except ModelInfoFetchError:
            # Use previously retrieved map if refresh fails
            pass

        # Step 3: Remove deprecated models
        results["removed_stale"] = await self.remove_stale_models(existing)

        # Refresh existing list after stale removal
        try:
            existing = await self.get_existing_models()
        except ModelInfoFetchError:
            pass

        # Step 4: Upsert each target model
        for target in all_targets:
            action, success = await self.upsert_model(target, existing)
            if action in results:
                results[action] += 1
            elif not success:
                results["failed"] += 1

        logger.info(f"Sync complete: {results}")
        return results
