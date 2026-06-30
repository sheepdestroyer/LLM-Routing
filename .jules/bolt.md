## 2026-06-24 - [Security Fix] Remove Hardcoded Cryptographic Secrets
**Learning:** Hardcoded cryptographic variables like salts or keys pose a severe security vulnerability. Storing them in plaintext within source control means anyone with read access to the repository could decrypt sensitive authentication tokens.
**Action:** Next time, make sure to generate cryptographic variables dynamically at runtime using secure random generation tools (e.g., `openssl rand`) and inject them via environment variables rather than embedding them as raw string values directly in configuration.
