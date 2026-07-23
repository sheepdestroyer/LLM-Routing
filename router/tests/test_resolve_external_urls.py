import pytest
import os
from unittest.mock import MagicMock

from router import main
from router.main import resolve_external_urls


class MockRequest:
    def __init__(self, base_host="localhost", base_netloc="localhost", url_scheme="http", url_netloc="localhost"):
        self.base_url = MagicMock()
        self.base_url.hostname = base_host
        self.base_url.netloc = base_netloc
        self.url = MagicMock()
        self.url.scheme = url_scheme
        self.url.netloc = url_netloc


@pytest.fixture(autouse=True)
def clean_env():
    # Store initial env variables
    initial_env = {
        "PUBLIC_BASE_URL": os.getenv("PUBLIC_BASE_URL"),
        "BASEURL": os.getenv("BASEURL"),
        "BASE_URL": os.getenv("BASE_URL"),
        "ROUTING_DOMAIN": os.getenv("ROUTING_DOMAIN"),
    }
    # Clear env
    for k in initial_env:
        if k in os.environ:
            del os.environ[k]
    yield
    # Restore env
    for k, v in initial_env.items():
        if v is not None:
            os.environ[k] = v
        elif k in os.environ:
            del os.environ[k]


def test_resolve_with_public_base_url_vaild_domain():
    os.environ["PUBLIC_BASE_URL"] = "https://app.vendeuvre.lan"
    os.environ["ROUTING_DOMAIN"] = "vendeuvre.lan"
    
    req = MockRequest()
    lf, ll, lm = resolve_external_urls(req)
    
    assert lf == "https://langfuse.app.vendeuvre.lan"
    assert ll == "https://litellm.app.vendeuvre.lan/ui/"
    assert lm == "https://llama.app.vendeuvre.lan/"


def test_resolve_with_valid_base_request():
    os.environ["ROUTING_DOMAIN"] = "vendeuvre.lan"
    os.environ["PUBLIC_BASE_URL"] = "http://[::1]:5000"
    
    # Request hostname is valid base
    req = MockRequest(
        base_host="sub.vendeuvre.lan",
        base_netloc="sub.vendeuvre.lan:8000",
        url_scheme="https",
        url_netloc="[::1]:8000"
    )
    
    lf, ll, lm = resolve_external_urls(req)
    
    # Valid request host is converted to service subdomains while preserving
    # its explicit port, rather than leaking the configured IPv6 fallback.
    assert lf == "http://langfuse.sub.vendeuvre.lan:8000"
    assert ll == "http://litellm.sub.vendeuvre.lan:8000/ui/"
    assert lm == "http://llama.sub.vendeuvre.lan:8000/"


def test_resolve_with_valid_base_request_without_port():
    os.environ["ROUTING_DOMAIN"] = "vendeuvre.lan"
    os.environ["PUBLIC_BASE_URL"] = "http://[::1]:5000"

    req = MockRequest(
        base_host="sub.vendeuvre.lan",
        base_netloc="sub.vendeuvre.lan",
        url_scheme="https",
        url_netloc="[::1]:8000",
    )

    lf, ll, lm = resolve_external_urls(req)

    assert lf == "http://langfuse.sub.vendeuvre.lan"
    assert ll == "http://litellm.sub.vendeuvre.lan/ui/"
    assert lm == "http://llama.sub.vendeuvre.lan/"


def test_local_fallback_ipv6():
    # Force fallback to local development logic (domain check fails)
    os.environ["ROUTING_DOMAIN"] = "vendeuvre.lan"
    
    # external_host will be derived from request.base_url.hostname
    req = MockRequest(
        base_host="::1",
        base_netloc="[::1]:8000",
        url_scheme="http",
        url_netloc="[::1]:8000"
    )
    
    # Mock global URL constants in main module
    main.LANGFUSE_HOST = "https://127.0.0.1:3001/"
    main.LITELLM_URL = "http://127.0.0.1:4000/api"
    main.LLAMA_SERVER_URL = "http://127.0.0.1:8080"
    
    lf, ll, lm = resolve_external_urls(req)
    
    # Should wrap ::1 in brackets, preserve scheme from targets (https for langfuse)
    assert lf == "https://[::1]:3001/"
    assert ll == "http://[::1]:4000/api/ui"
    assert lm == "http://[::1]:8080"


def test_local_fallback_ipv4():
    os.environ["ROUTING_DOMAIN"] = "vendeuvre.lan"
    
    req = MockRequest(
        base_host="127.0.0.1",
        base_netloc="127.0.0.1:8000",
        url_scheme="http",
        url_netloc="127.0.0.1:8000"
    )
    
    main.LANGFUSE_HOST = "http://localhost:3001"
    main.LITELLM_URL = "http://localhost:4000"
    main.LLAMA_SERVER_URL = "http://localhost:8080"
    
    lf, ll, lm = resolve_external_urls(req)
    
    # Should not wrap 127.0.0.1 in brackets
    assert lf == "http://127.0.0.1:3001"
    assert ll == "http://127.0.0.1:4000/ui"
    assert lm == "http://127.0.0.1:8080"
