## 2025-02-28 - CRLF Injection in API Proxy Paths
**Vulnerability:** The `proxy_memory` and `proxy_audio` endpoints dynamically construct proxy URLs using unescaped and unsanitized request paths.
**Learning:** URL construction via string concatenation (`f"{base_url}{path}"`) without properly validating against carriage return (`\r`) and line feed (`\n`) characters allows CRLF injection. This can lead to HTTP Request Smuggling/Splitting in downstream `httpx` clients.
**Prevention:** Always sanitize dynamically constructed paths by blocking `\r` and `\n` characters before appending them to base URLs, or use strict URL parsing libraries that enforce valid characters in the path portion of a URI.

## 2025-02-20 - Hardcoded authorization keys removal in responses API
**Vulnerability:** The responses API proxy `POST /v1/responses` allowed the use of hardcoded testing tokens in production, which bypassed the intended authentication mechanism (`valid_keys` list contained entries like "gateway-pass", "local-token", "test-key", etc. for `client_token` validation).
**Learning:** This existed likely because these keys were used for running the automated tests inside pytest and the developer forgot to conditionally restrict them to only test environments.
**Prevention:** Using environment-based configuration for secrets, avoiding adding test-specific credentials into production source code, and strictly conditionally injecting them (e.g., `if "pytest" in sys.modules`) if absolutely necessary.

## 2025-02-18 - Missing Authentication on Chat Completions Endpoint
**Vulnerability:** The `/v1/chat/completions` endpoint was publicly accessible without verifying the client Authorization header against configured server secrets (`ROUTER_API_KEY`, `LITELLM_MASTER_KEY`, `GATEWAY_KEY`).
**Learning:** All proxy/completion ingress endpoints must enforce client authentication with fail-closed semantics and case-insensitive Bearer token validation to avoid unauthorized access when exposed to external networks.
**Prevention:** Centralize client token authentication across all proxy endpoints with a dedicated helper that fails closed if no valid keys are configured.

