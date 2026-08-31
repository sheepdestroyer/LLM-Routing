import json
import os
import pytest
from unittest.mock import patch, AsyncMock
from router import main
import host_agy_daemon

def test_parse_oauth_token_info_nested():
    data = {
        "auth_method": "consumer",
        "token": {
            "access_token": "ya29.test_token_123",
            "refresh_token": "1//test_refresh_456",
            "token_type": "Bearer",
            "expiry": "2026-08-14T15:45:24.092546+02:00"
        }
    }
    token, expiry_ms = main._parse_oauth_token_info(data)
    assert token == "ya29.test_token_123"
    assert expiry_ms > 0

def test_parse_oauth_token_info_flat():
    data = {
        "access_token": "ya29.flat_token",
        "expiry_date": 1786715124000
    }
    token, expiry_ms = main._parse_oauth_token_info(data)
    assert token == "ya29.flat_token"
    assert expiry_ms == 1786715124000

def test_parse_oauth_token_info_iso_utc():
    data = {
        "access_token": "ya29.utc_token",
        "expiry": "2026-08-14T13:45:24Z"
    }
    token, expiry_ms = main._parse_oauth_token_info(data)
    assert token == "ya29.utc_token"
    assert expiry_ms > 0

def test_parse_oauth_token_info_iso_negative_tz():
    data = {
        "access_token": "ya29.neg_tz_token",
        "expiry": "2026-08-14T08:45:24.123456-05:00"
    }
    token, expiry_ms = main._parse_oauth_token_info(data)
    assert token == "ya29.neg_tz_token"
    assert expiry_ms > 0

def test_parse_oauth_token_info_numeric_seconds():
    data = {
        "access_token": "ya29.sec_token",
        "expiry_date": 1786715124  # Seconds, < 10B
    }
    token, expiry_ms = main._parse_oauth_token_info(data)
    assert token == "ya29.sec_token"
    assert expiry_ms == 1786715124000

def test_parse_oauth_token_info_invalid_non_dict():
    token, expiry_ms = main._parse_oauth_token_info(None)
    assert token is None
    assert expiry_ms == 0

    token, expiry_ms = main._parse_oauth_token_info("invalid_string")
    assert token is None
    assert expiry_ms == 0

def test_parse_oauth_token_info_missing_token():
    data = {"expiry": "2026-08-14T15:45:24Z"}
    token, expiry_ms = main._parse_oauth_token_info(data)
    assert token is None

def test_parse_oauth_token_info_invalid_date_str():
    data = {
        "access_token": "ya29.invalid_date",
        "expiry": "not-a-valid-date"
    }
    token, expiry_ms = main._parse_oauth_token_info(data)
    assert token == "ya29.invalid_date"
    assert expiry_ms == 0

def test_host_agy_daemon_auth_status_valid(tmp_path, monkeypatch):
    token_file = tmp_path / "antigravity-oauth-token"
    token_file.write_text(json.dumps({
        "auth_method": "consumer",
        "token": {
            "access_token": "mock_tok_live",
            "refresh_token": "mock_ref_live",
            "expiry": "2026-08-14T18:00:00+02:00"
        }
    }))
    monkeypatch.setattr(host_agy_daemon, "CLI_TOKEN_PATH", str(token_file))
    with patch("time.time", return_value=1786715000.0):
        status = host_agy_daemon.get_auth_status()
        assert status["authenticated"] is True
        assert status["source"] == "cli_token"

def test_host_agy_daemon_auth_status_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(host_agy_daemon, "CLI_TOKEN_PATH", str(tmp_path / "nonexistent"))
    status = host_agy_daemon.get_auth_status()
    assert status["authenticated"] is False
    assert status["status"] == "missing"

def test_host_agy_daemon_auth_status_invalid_json(tmp_path, monkeypatch):
    token_file = tmp_path / "antigravity-oauth-token"
    token_file.write_text("{broken json")
    monkeypatch.setattr(host_agy_daemon, "CLI_TOKEN_PATH", str(token_file))
    status = host_agy_daemon.get_auth_status()
    assert status["authenticated"] is False
    assert status["status"] == "error"

def test_host_agy_daemon_auth_status_empty_json(tmp_path, monkeypatch):
    token_file = tmp_path / "antigravity-oauth-token"
    token_file.write_text("{}")
    monkeypatch.setattr(host_agy_daemon, "CLI_TOKEN_PATH", str(token_file))
    status = host_agy_daemon.get_auth_status()
    assert status["authenticated"] is False
    assert status["status"] == "missing"
