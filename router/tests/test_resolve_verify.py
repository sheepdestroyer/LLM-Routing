import pytest
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
    ],
)
def test_resolve_verify(monkeypatch, env_val, expected):
    env_name = "TEST_CA_BUNDLE_ENV"
    if env_val is None:
        monkeypatch.delenv(env_name, raising=False)
    else:
        monkeypatch.setenv(env_name, env_val)

    result = _resolve_verify(env_name)
    assert result == expected
    assert type(result) is type(expected)
