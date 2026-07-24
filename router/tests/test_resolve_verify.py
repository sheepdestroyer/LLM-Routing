import pytest
from unittest.mock import patch
from router.main import _resolve_verify

@pytest.mark.parametrize(
    "env_val, expected",
    [
        (None, False),
        ("", False),
        ("  ", False),
        ("false", False),
        ("0", False),
        ("off", False),
        ("no", False),
        ("none", False),
        ("null", False),
        ("disabled", False),
        ("FALSE", False),
        ("Off", False),
        ("true", True),
        ("1", True),
        ("on", True),
        ("yes", True),
        ("TRUE", True),
        ("On", True),
        ("/etc/ssl/certs/ca-bundle.crt", "/etc/ssl/certs/ca-bundle.crt"),
        ("  /path/to/cert.pem  ", "/path/to/cert.pem"),
    ]
)
def test_resolve_verify(env_val, expected):
    with patch("os.getenv", return_value=env_val):
        assert _resolve_verify("TEST_ENV_VAR") == expected
