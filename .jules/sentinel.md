## 2025-02-28 - CRLF Injection in API Proxy Paths
**Vulnerability:** The `proxy_memory` and `proxy_audio` endpoints dynamically construct proxy URLs using unescaped and unsanitized request paths.
**Learning:** URL construction via string concatenation (`f"{base_url}{path}"`) without properly validating against carriage return (`\r`) and line feed (`\n`) characters allows CRLF injection. This can lead to HTTP Request Smuggling/Splitting in downstream `httpx` clients.
**Prevention:** Always sanitize dynamically constructed paths by blocking `\r` and `\n` characters before appending them to base URLs, or use strict URL parsing libraries that enforce valid characters in the path portion of a URI.
